"""The game host on the hub: one Game, one clock, every seat's calls.

- The clock: game time = start_minute + (real seconds since the world started) * 1440 / real_sec_per_day. A thread
  advances the world four times a second. Nobody can pause it except freeze() (keepalive.py calls it at the end).
- The world starts at the first call from any seat, or `autostart_sec` after the host starts. A world restored from
  state.pkl (a run resumed from a checkpoint) waits the same way, so the minutes the agents take to come back cost no
  game time.
- Each seat has a token bucket (rate_per_sec, rate_burst); a look costs look_cost. A refused call is HTTP 429.
- State lives in STATE_DIR (default /var/lib/holdout, passed to the harness as hub_state_dirs so a checkpoint
  keeps it): score.json (the grading state, rewritten every 2 s, atomically), replay.jsonl (header + a frame every
  few game minutes, for the viewer), state.pkl (the whole game every 60 s, so a restarted hub resumes),
  perf.jsonl (how well the loop keeps real time, plus one record per stranger event and per threat),
  messages.jsonl (every message in full) and strangers.jsonl (one line per stranger: type, who talked, decision, outcome).
"""
import json
import os
import pickle
import threading
import time

from . import config as C
from .engine import BAD_ARGS, Game, GameError

MAX_BODY_BYTES = 64 * 1024
STATE_DIR = os.environ.get("HOLDOUT_STATE_DIR", "/var/lib/holdout")


def _atomic(path, data, mode="w"):
    tmp = path + ".tmp"
    with open(tmp, mode) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class GameHost:
    def __init__(self, seats=lambda: [], emit=lambda *a, **k: None, state_dir=STATE_DIR, cfg=None, clock=time.monotonic,
                 start_thread=True):
        self.cfg = cfg or C.load(os.environ.get("HOLDOUT_PROFILE") or os.path.join(os.path.dirname(__file__), "..", "profile.json"))
        self.seed = self.cfg.get("seed", "holdout-v1")
        self.seats_fn, self.emit = seats, emit
        self.dir = state_dir
        os.makedirs(self.dir, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.clock = clock
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.game = None
        self.t_start = None            # clock() at which the world's real time is 0
        self.wall0 = 0.0               # real seconds already played before a resume
        self.frozen = False
        self.created = clock()
        self.buckets = {}
        self.refused = {}
        self.last_score = 0.0
        self.last_save = 0.0
        self._resume()
        self._write_score()            # a grade exists from the start: a run nobody played grades 0, not "failed"
        if start_thread:
            threading.Thread(target=self._loop, daemon=True, name="holdout-clock").start()

    # ------------------------------------------------------------------ persistence
    def _p(self, name):
        return os.path.join(self.dir, name)

    def _resume(self):
        try:
            with open(self._p("state.pkl"), "rb") as f:
                saved = pickle.load(f)
        except (OSError, EOFError, pickle.UnpicklingError):
            return
        self.game = saved["game"]
        self.wall0 = saved["wall"]
        self.frozen = saved.get("frozen", False)
        self.t_start = None            # held at wall0 until _begin()
        self.emit("holdout_resume", None, wall=self.wall0, t=self.game.t)

    def _save(self):
        g = self.game
        _atomic(self._p("state.pkl"), pickle.dumps({"game": g, "wall": g.wall, "frozen": self.frozen}), "wb")
        self._write_strangers()
        self.last_save = self.clock()

    def _write_score(self):
        g = self.game
        st = g.grade_state() if g else {"grade": 0.0, "started": False}
        st.update({"started": g is not None, "frozen_by_host": self.frozen, "refused": dict(self.refused),
                   "written_at": time.time()})
        _atomic(self._p("score.json"), json.dumps(st))
        self.last_score = self.clock()

    def _flush_frames(self):
        g = self.game
        for name, buf in (("replay.jsonl", g.frames), ("messages.jsonl", g.msg_log), ("perf.jsonl", g.perf_log)):
            if buf:
                with open(self._p(name), "a") as f:
                    for rec in buf:
                        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                buf.clear()

    def _write_strangers(self):
        recs = self.game.stranger_records()
        _atomic(self._p("strangers.jsonl"), "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))

    # ------------------------------------------------------------------ the clock
    def _start(self):
        if self.game is not None:
            return
        seats = sorted(self.seats_fn())
        self.game = Game(self.cfg, self.seed, seats)
        self.t_start = self.clock()
        self.game.started = True
        with open(self._p("replay.jsonl"), "w") as f:
            f.write(json.dumps(self.game.header(), separators=(",", ":")) + "\n")
        open(self._p("messages.jsonl"), "a").close()     # always there: a run with no messages shows an empty file, not a lost one
        self.emit("holdout_start", None, seats=seats)
        self._write_score()
        self._save()

    def _begin(self):
        """Start the world, or a restored world's clock: at the first call, or autostart_sec after the host starts."""
        if self.game is None:
            self._start()
        elif self.t_start is None:
            self.t_start = self.clock()
            self.emit("holdout_clock_resumed", None, wall=round(self.wall0, 1), t=self.game.t)

    def elapsed(self):
        if self.t_start is None:
            return self.wall0
        return self.wall0 + (self.clock() - self.t_start)

    def _loop(self):
        last_perf = self.clock()
        while True:
            time.sleep(0.25)
            try:
                with self.cond:
                    if self.t_start is None and self.clock() - self.created >= self.cfg["autostart_sec"]:
                        self._begin()
                    if self.t_start is None:
                        continue
                    if not self.frozen:
                        t0 = time.perf_counter()
                        wall = self.elapsed()
                        target = self.cfg["start_minute"] + wall * 1440.0 / self.cfg["real_sec_per_day"]
                        self.game.advance_to(target, wall=wall)
                        busy = time.perf_counter() - t0
                        self._flush_frames()
                        if self.game.frozen:
                            self.frozen = True
                        self.cond.notify_all()
                        if self.clock() - last_perf >= 60:
                            last_perf = self.clock()
                            lag = target - self.game.t
                            with open(self._p("perf.jsonl"), "a") as f:
                                f.write(json.dumps({"kind": "loop", "wall": round(wall, 1), "t": self.game.t, "lag_min": round(lag, 2),
                                                    "tick_busy_s": round(busy, 4), "zombies": len(self.game.zombies)}) + "\n")
                    if self.clock() - self.last_score >= 2:
                        self._write_score()
                    if self.clock() - self.last_save >= 60:
                        self._save()
            except Exception as e:  # noqa: BLE001 - the clock must keep running
                self.emit("holdout_error", None, error=f"{type(e).__name__}: {e}")

    def freeze(self):
        with self.cond:
            if self.game is None:
                self._start()
            if not self.frozen:
                wall = self.elapsed()
                target = self.cfg["start_minute"] + wall * 1440.0 / self.cfg["real_sec_per_day"]
                self.game.advance_to(target, wall=wall)
                self._flush_frames()
                self.frozen = True
                self.game.frozen = True
                self.game._frame(force=True)
                for s in self.game.surv.values():
                    if s["threat"]:
                        self.game._close_threat(s, "run_end")
                self._flush_frames()
            self._write_score()
            self._save()
            return {"ok": True, "frozen_at_wall": round(self.game.wall, 1), "grade": round(self.game.best, 6)}

    # ------------------------------------------------------------------ calls
    def _charge(self, seat, cost):
        rate, burst = self.cfg["rate_per_sec"], self.cfg["rate_burst"]
        now = self.clock()
        tokens, last = self.buckets.get(seat, (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        if tokens < cost:
            self.buckets[seat] = (tokens, now)
            self.refused[seat] = self.refused.get(seat, 0) + 1
            raise GameError("rate limit: slow down", code=429, retry_after_sec=round((cost - tokens) / rate, 2))
        self.buckets[seat] = (tokens - cost, now)

    def call(self, seat, endpoint, body):
        try:
            return self._call(seat, endpoint, body)
        except BAD_ARGS as e:
            raise GameError(f"bad arguments: {e}") from None

    def _call(self, seat, endpoint, body):
        if not isinstance(body, dict):
            raise GameError("the body must be a JSON object")
        cost = self.cfg["look_cost"] if endpoint == "look" else 1
        with self.cond:
            self._charge(seat, cost)
            self._begin()
            if self.frozen and endpoint == "act":
                raise GameError("the game is over: the world is frozen", code=409)
            if endpoint == "events" and body.get("wait_seconds"):
                wait = max(0.0, min(30.0, float(body["wait_seconds"])))
                after = int(body.get("after", 0))
                deadline = self.clock() + wait
                s = self.game.join(seat)
                while s["ev_seq"] <= after and self.clock() < deadline and not self.frozen:
                    self.cond.wait(timeout=min(1.0, deadline - self.clock()))
            out = self.game.api(seat, endpoint, body)
            if endpoint == "status":
                out["time"]["real_seconds_since_start"] = round(self.elapsed(), 1)
            return out
