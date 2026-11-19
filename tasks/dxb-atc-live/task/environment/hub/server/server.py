"""The live day: the simulator on a clock, behind a small HTTP server on 127.0.0.1 that only the board plugin
(atc_plugin.py) talks to. Runs with the task image's Python, which has BlueSky.

    python3 server.py --port 8092 --scenario scenarios/live.json --state /var/lib/dxb-atc --speed 3

The day starts at the first call from any seat, or --auto-start-min real minutes after the server starts, whichever
comes first: the shift starts on time whether or not the team is ready. It runs at --speed times real time and never
pauses. A sector that no seat has open has no controller. State, in
--state (0700): secret.json (the seed for every random draw), score.json (the grading record, rewritten every
simulated minute and at the end), timeline.jsonl (one line every simulated 10 minutes).

The day cannot be restarted. If --state already holds a score.json whose day has begun (sim_t above 0), the server
does not start: a new day would begin at 18:00Z and overwrite that record. It writes the reason to stderr and exits
with code 3.
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from atcsim import api as API  # noqa: E402
from atcsim.world import World  # noqa: E402


def _round_finite(x):
    return round(x) if x is not None and math.isfinite(x) else None


# The grading record is signed with a key that only this server and tests/grade.py hold. Neither file reaches a team's
# sandbox, so a record written by anything else (a team in a verifier that shares its container) does not grade.
SIGN_KEY = bytes.fromhex("15272bdd733541611393929c523079765c1f6ad3c06d30f641831088c39ddd9a")


def sign(rec):
    body = json.dumps({k: v for k, v in rec.items() if k != "sig"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(SIGN_KEY, body.encode(), hashlib.sha256).hexdigest()


class DayInProgress(RuntimeError):
    """The state folder already holds the record of a day that has begun."""


EXIT_DAY_IN_PROGRESS = 3


def check_no_day_in_progress(state_dir):
    """Raise DayInProgress when state_dir/score.json records a day that has begun. Starting again would build a new
    day at 18:00Z and overwrite that record, and every later grading would then read the new day."""
    p = os.path.join(state_dir, "score.json")
    if not os.path.exists(p):
        return
    try:
        with open(p) as f:
            sim_t = float(json.load(f).get("sim_t") or 0)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        raise DayInProgress(f"{p} exists but cannot be read ({type(e).__name__}: {e}); refusing to start a new day "
                            f"over it") from None
    if sim_t > 0:
        raise DayInProgress(f"{p} records a day already in progress ({sim_t:.0f} s of airport time); refusing to start "
                            f"a new day, which would overwrite it. The day cannot be restarted.")


def exit_day_in_progress(e):
    print(f"atc server: {e}", file=sys.stderr, flush=True)
    sys.exit(EXIT_DAY_IN_PROGRESS)


class Live:
    def __init__(self, scenario_path, state_dir, speed, controller=None):
        # controller: a built-in controller for the sectors no seat has open. None on the live day; the reference
        # solution (standalone.py --reference) passes the rule-based one in standin.py.
        self.controller = controller
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o700)
        check_no_day_in_progress(state_dir)
        self.secret = self._secret()
        self.scenario_path = os.path.abspath(scenario_path)
        with open(scenario_path) as f:
            self.scenario = json.load(f)
        self.speed = float(speed)
        self.lock = threading.RLock()
        self.world = None
        self.phase = "not_started"
        self.started_wall = None
        self.frozen_at = None
        self.error = None
        self.api = None
        self._last_save = -1e9
        self._last_tl = -1e9
        self.calls = {}
        self.step_errors = 0
        self.write_errors = 0  # failed file writes (the record or the replay files): not simulator errors
        self.last_errors = []

    def _secret(self):
        p = os.path.join(self.state_dir, "secret.json")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"secret": secrets.token_hex(16)}, f)
        with open(p) as f:
            return json.load(f)["secret"]

    def start(self):
        with self.lock:
            if self.phase != "not_started":
                return
            self.phase = "starting"
        w = World(self.scenario, secret=self.secret, standin=self.controller, strict=False)
        with self.lock:
            self.world = w
            self.api = API.Api(w)
            self.phase = "running"
            self.started_wall = time.time()
        threading.Thread(target=self._clock, daemon=True, name="atc-clock").start()

    def auto_start(self, minutes):
        """Start the day after `minutes` of real time if no seat has called by then."""
        t = threading.Timer(minutes * 60.0, self.start)
        t.daemon = True
        t.start()

    def _clock(self):
        w = self.world
        try:
            while True:
                with self.lock:
                    if self.phase != "running":
                        break
                    target = (time.time() - self.started_wall) * self.speed
                    n = 0
                    while w.t < target and w.t < w.t_end and n < 30:
                        try:
                            w.step(1.0)
                        except Exception:
                            # One failing step must not end the day: record it and carry on with the next second.
                            self.step_errors += 1
                            self.last_errors.append({"t": w.t, "trace": traceback.format_exc()[-2000:]})
                            self.last_errors = self.last_errors[-20:]
                            if self.step_errors > 2000:
                                raise
                        n += 1
                    if w.t >= w.t_end:
                        self.phase = "ended"
                    if w.t - self._last_save >= 60:
                        try:
                            self.save()
                        except Exception:  # a failed save must not stop the clock: the next minute saves again
                            self.write_errors += 1
                            self.last_errors.append({"t": w.t, "trace": traceback.format_exc()[-2000:]})
                            self.last_errors = self.last_errors[-20:]
                    try:  # the replay files are not graded: a failed write must not stop the clock either
                        if w.t - self._last_tl >= 600:
                            self._timeline()
                        if w.t - getattr(self, "_last_track", -1e9) >= 20:
                            self._track()
                    except Exception:
                        self.write_errors += 1
                        self.last_errors.append({"t": w.t, "trace": traceback.format_exc()[-2000:]})
                        self.last_errors = self.last_errors[-20:]
                time.sleep(0.02 if n else 0.05)
        except Exception:
            self.error = traceback.format_exc()
            with self.lock:
                self.phase = "crashed"
                self.save()

    def freeze(self):
        with self.lock:
            if self.phase in ("frozen",):
                return {"phase": "frozen"}
            if self.phase in ("running", "ended", "crashed"):
                self.phase = "frozen"
                self.frozen_at = time.time()
                self.save()
            else:
                self.phase = "frozen"
                self.frozen_at = time.time()
        return {"phase": "frozen"}

    def record(self):
        """The grading record: every flight's outcome and every safety event. No grade is computed here."""
        w = self.world
        flights = []
        for f in w.flights.values():
            flights.append({"id": f.id, "kind": f.kind, "sched": f.sched, "outcome": f.outcome, "t_done": f.t_done,
                            "return_of": f.data.get("return_of"), "diverted_to": f.diverted_to, "cancel": f.cancel_reason,
                            "phase": f.phase, "pax": f.pax, "airline": f.airline, "type": f.type,
                            "out_t": f.t_done if f.outcome in ("landed", "departed") else f.data.get("out_t"),
                            "proj_t": _round_finite(w.projected_done(f)) if f.outcome is None else None})
        return {"schema": 1, "scenario": w.sc.get("name"), "reference": self.controller is not None,
                "start_utc": w.base_utc, "duration_s": w.t_end, "sim_t": w.t,
                "speed": self.speed, "phase": self.phase, "started_wall": self.started_wall, "frozen_at": self.frozen_at,
                "error": self.error, "flights": flights,
                "safety": [{k: v for k, v in s.items() if k != "key"} for s in w.safety],
                "stats": dict(w.stats), "sectors": dict(w.sectors.owner), "calls": self.calls,
                "step_errors": self.step_errors, "write_errors": self.write_errors, "last_errors": self.last_errors,
                "stage_errors": w.stage_errors[-10:]}

    def save(self):
        if self.world is None:
            return
        self._last_save = self.world.t
        p = os.path.join(self.state_dir, "score.json")
        tmp = p + ".tmp"
        rec = self.record()
        rec["sig"] = sign(rec)
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, p)

    def _timeline(self):
        w = self.world
        self._last_tl = w.t
        s = w.score(lag_s=0)
        with open(os.path.join(self.state_dir, "timeline.jsonl"), "a") as f:
            f.write(json.dumps({"wall": time.time(), "t": w.t, "clock": w.clock(), "airborne": len(w.air.state),
                                "ground": len(w.movers), "service_now": s["service"], "safety_events": s["safety_events"],
                                "sectors": dict(w.sectors.owner)}) + "\n")

    def _track(self):
        """One radar frame every 20 simulated seconds, for the replay viewer (not read by the grader)."""
        w = self.world
        self._last_track = w.t
        from atcsim.ground import pos_xy
        air = []
        for fid, st in w.air.state.items():
            s = w.air.snapshot(fid)
            if s:
                air.append([fid, round(s["lat"], 4), round(s["lon"], 4), int(s["alt_ft"]), int(s["trk"]), st.mode[:3],
                            w.flights[fid].sector or ""])
        gnd = [[fid, int(x), int(y), w.flights[fid].phase[:4]] for fid, (x, y) in
               ((fid, pos_xy(w.ground, m)) for fid, m in w.movers.items())]
        frame = {"t": w.t, "air": air, "gnd": gnd, "cells": [[c["id"], round(c["lat"], 3), round(c["lon"], 3), c["radius_nm"]]
                                                           for c in w.weather.active_cells(w.t)],
                 "metar": w.weather.current(w.t).raw, "ramp": w.ramp_closed, "rwy": [w.arr_rwy, w.dep_rwy],
                 "sectors": {k: v for k, v in w.sectors.owner.items() if v}}
        with open(os.path.join(self.state_dir, "tracks.jsonl"), "a") as f:
            f.write(json.dumps(frame, separators=(",", ":")) + "\n")

    def call(self, seat, endpoint, body):
        if self.phase == "not_started":
            self.start()
        with self.lock:
            self.calls[seat] = self.calls.get(seat, 0) + 1
            if self.phase != "running":
                st = {"phase": self.phase}
                if endpoint == "act":
                    return 409, {"ok": False, "error": f"the day is {self.phase}", "status": st}
                if self.world is None:
                    return 200, {"ok": True, "result": None, "status": st}
            return self.api.call(seat, endpoint, body, phase=self.phase)


def serve(live, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._reply(400, {"ok": False, "error": "bad json"})
            if self.path == "/call":
                try:
                    code, reply = live.call(body["seat"], body["endpoint"], body.get("body") or {})
                except Exception as e:  # noqa: BLE001
                    code, reply = 500, {"ok": False, "error": f"internal error: {type(e).__name__}: {e}"[:500]}
                return self._reply(code, reply)
            if self.path == "/freeze":
                return self._reply(200, live.freeze())
            if self.path == "/health":
                w_ = live.world
                return self._reply(200, {"phase": live.phase, "t": w_.t if w_ else None, "error": live.error,
                                         "step_errors": live.step_errors, "stage_errors": w_.stats.get("stage_errors", 0) if w_ else 0})
            return self._reply(404, {"ok": False, "error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--scenario", default=os.path.join(HERE, "scenarios", "live.json"))
    ap.add_argument("--state", default="/var/lib/dxb-atc")
    ap.add_argument("--speed", type=float, default=3.0)
    ap.add_argument("--start-now", action="store_true")
    ap.add_argument("--auto-start-min", type=float, default=10.0,
                    help="start the day this many real minutes after the server starts if no seat has called (0: never)")
    a = ap.parse_args()
    try:
        live = Live(a.scenario, a.state, a.speed)
    except DayInProgress as e:
        exit_day_in_progress(e)
    if a.start_now:
        live.start()
    elif a.auto_start_min > 0:
        live.auto_start(a.auto_start_min)
    srv = serve(live, a.port)
    print(f"atc server on 127.0.0.1:{a.port}, speed {a.speed}x", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
