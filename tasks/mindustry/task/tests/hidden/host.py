"""The game host: runs one Mindustry JVM per world on the hub, enforces each unit's action rate, and keeps the
grading state. Standard library only: it runs inside the board on the hub's Python (game_plugin.py).

The team has a fixed number of units whatever its size, dealt evenly to its agents. To the bridge each unit is a seat, so
sight, the event log and the token bucket belong to the unit. An agent names one of its units in a call. World k's seed is
HMAC(map_key, k), with map_key from profile.json on the hub, so every run plays the same maps and no agent can know them.

State, in /var/lib/mindustry-many-fronts (0700, --ek hub_state_dirs):
  score.json      the grading state: per-world native counters and the fleet, written atomically every POLL_SEC
  timeline.jsonl  one line a minute, for reports
Counters are read from the bridge at 127.0.0.1:8091, which only this process talks to.
"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = "/var/lib/mindustry-many-fronts"
SERVER_JAR = os.environ.get("MMF_SERVER_JAR", "/opt/mindustry/server.jar")
BRIDGE = "http://127.0.0.1:8091"
READABLE = ("status", "query", "events")
MAX_BODY_OPS = 16
MAX_BODY_BYTES = 64 * 1024
MAX_OP_ITEMS = 64     # list elements in one op, nested lists included; each one costs a token like an op
SCAN_PER_TOKEN = 256  # a read also costs a token for each this many rows the bridge had to build
POLL_SEC = 2.0
CRASH_RETRY_SEC = 5.0
JVM_START_SEC = 90.0


class GameError(Exception):
    """A refusal for the seat. code is the HTTP status."""

    def __init__(self, message, code=409, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


class Buckets:
    """Per-unit token buckets. An op costs a token; a call that does not fit is refused and spends nothing."""

    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate, self.burst, self.clock = float(rate), float(burst), clock
        self.state = {}
        self.lock = threading.Lock()

    def debit(self, seat, cost):
        """Charge work found out only after the call ran. The bucket may go down to -burst, so the next calls wait."""
        with self.lock:
            now = self.clock()
            tokens, last = self.state.get(seat, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            self.state[seat] = (max(-self.burst, tokens - cost), now)

    def take(self, seat, cost):
        with self.lock:
            now = self.clock()
            tokens, last = self.state.get(seat, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self.state[seat] = (tokens - cost, now)
                return 0.0
            self.state[seat] = (tokens, now)
            return (cost - tokens) / self.rate


def list_items(value):
    """How many list elements a JSON value holds, at any depth."""
    if isinstance(value, list):
        return len(value) + sum(list_items(v) for v in value)
    if isinstance(value, dict):
        return sum(list_items(v) for v in value.values())
    return 0


def world_seed(secret, k):
    """The seed of world k: 31 bits of HMAC(secret, k). The same in every run; without the key, a world says nothing about the next."""
    digest = hmac.new(bytes.fromhex(secret), f"world:{k}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big") >> 1


def deal(agents, names):
    """Deal the units to the agents in turn, in name order, so shares differ by at most one."""
    agents = sorted(agents)
    if len(agents) > len(names):
        raise GameError(f"{len(agents)} agents, but the team has only {len(names)} units", 500)
    return {a: names[i::len(agents)] for i, a in enumerate(agents)}


def load_profile(path=None):
    with open(path or os.path.join(HERE, "profile.json")) as f:
        return json.load(f)


class GameHost:
    def __init__(self, seats=lambda: [], state=STATE, profile=None, now=time.time, jar=None, java="java",
                 work="/var/tmp/mindustry-many-fronts", log_dir="/var/log/mindustry-many-fronts", emit=lambda *a, **k: None,
                 jvm_env=None, poll_sec=POLL_SEC):
        self.seats, self.state, self.now, self.java, self.work, self.log_dir = seats, state, now, java, work, log_dir
        self.emit, self.jvm_env, self.poll_sec = emit, jvm_env or {}, poll_sec
        p = dict(profile or load_profile())
        host = p.pop("host")
        self.bridge_profile = p
        self.lockout = float(host["between_worlds_sec"])
        self.min_units = int(host["units"])
        self.units = [f"unit-{i + 1}" for i in range(self.min_units)]
        self.secret = host["map_key"]
        self.buckets = Buckets(host["rate_per_sec"], host["rate_burst"])
        self.jar = jar or os.path.join(self.work, "agent-bridge.jar")
        self.lock = threading.RLock()
        self.proc = None
        self.phase = "not_started"  # not_started, starting, between_worlds, running
        self.next_world_at = None
        self.last_timeline = 0.0
        self.build_error = None
        os.makedirs(self.state, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.path = os.path.join(self.state, "score.json")
        self.s = self._load()
        if "fleet" in self.s:  # a restarted host keeps the fleet it dealt
            self.units = [f"unit-{i + 1}" for i in range(sum(len(u) for u in self.s["fleet"].values()))]
        self.compiled = threading.Event()

    # -- state ----------------------------------------------------------

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                s = json.load(f)
            # A world that was running when the hub stopped (a resumed checkpoint) ends where it was counted. No penalty.
            for w in s["worlds"]:
                if w["ended"] == "running":
                    w.update(ended="restart", ended_at=self.now())
            return s
        return {"schema": 1, "created_at": self.now(), "first_call_at": None, "losses": 0, "worlds": [], "calls": {}, "refused": {}}

    def save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.s, f)
        os.replace(tmp, self.path)

    # -- the bridge -----------------------------------------------------

    def bridge(self, path, body=None, timeout=30):
        req = urllib.request.Request(BRIDGE + path, json.dumps(body or {}).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)

    def compile(self):
        """Build the bridge from the bundled source with the image's JDK. Run once, off the board's start path."""
        try:
            if not os.path.exists(self.jar):
                os.makedirs(self.work, exist_ok=True)
                subprocess.run(["sh", os.path.join(HERE, "bridge", "build.sh"), SERVER_JAR, self.jar], check=True,
                               capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            self.build_error = (e.stderr or e.stdout or "")[-2000:]
            self.emit("game", None, op="build_failed", error=self.build_error)
        except Exception as e:  # noqa: BLE001 - recorded, and every call then reports it
            self.build_error = str(e)
            self.emit("game", None, op="build_failed", error=self.build_error)
        self.compiled.set()

    def _start_jvm(self, k):
        self.compiled.wait(600)
        if self.build_error:
            raise GameError("the game server could not be built", 500)
        run = os.path.join(self.work, f"world-{k}")
        shutil.rmtree(run, ignore_errors=True)
        os.makedirs(os.path.join(run, "config", "mods"))
        shutil.copy(self.jar, os.path.join(run, "config", "mods"))
        os.makedirs(self.log_dir, exist_ok=True)
        log = open(os.path.join(self.log_dir, f"world-{k}.log"), "ab")
        # The JVM gets no board or proxy secrets, no stdin, and listens only on loopback.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": run, **self.jvm_env}
        self.proc = subprocess.Popen([self.java, "-Xms256m", "-XX:MaxRAMPercentage=75", "-jar", SERVER_JAR], cwd=run, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        deadline = time.monotonic() + JVM_START_SEC
        while True:
            try:
                self.bridge("/health", timeout=5)
                return
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the game server did not start") from None
                time.sleep(0.25)

    def _stop_jvm(self):
        if self.proc is None:
            return
        try:
            self.bridge("/stop", timeout=5)
        except Exception:  # noqa: BLE001 - it is killed next anyway
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
        self.proc = None

    def _begin_world(self):
        """Start world k. Runs on the monitor thread, without the lock while the JVM boots."""
        k = len(self.s["worlds"])
        self._start_jvm(k)
        for step, body in (("/setup", {"world_index": k, "seed": world_seed(self.secret, k), "seats": self.units,
                                       "profile": self.bridge_profile}), ("/start", {})):
            r = self.bridge(step, body, timeout=60)
            if not r.get("ok"):
                raise RuntimeError(f"{step}: {r.get('error')}")
        with self.lock:
            frozen = self.phase == "frozen"
        if frozen:  # the game stopped while this world was booting
            self._stop_jvm()
            return
        with self.lock:
            self.s["worlds"].append({"index": k, "losses_before": self.s["losses"], "started_at": self.now(), "ended": "running",
                                     "ended_at": None, "source": {}, "core_in": {}, "core_in_tainted": {}, "tick": 0, "wave": 0})
            self.phase = "running"
            self.save()
        self.emit("game", None, op="world_started", world=k, fleet=self.s["fleet"])

    def _end_world(self, how):
        with self.lock:
            w = self.s["worlds"][-1]
            w.update(ended=how, ended_at=self.now())
            # A crash costs what a lost core costs. The two cannot be told apart from here, and a free restart with the waves
            # back at 1 would be worth causing. A restart of the hub (see _load) is not a crash and costs nothing.
            self.s["losses"] += 1
            self.next_world_at = self.now() + self.lockout
            self.phase = "between_worlds"
            self.save()
        self.emit("game", None, op="world_ended", world=w["index"], how=how, wave=w["wave"], tick=w["tick"])
        self._stop_jvm()

    def poll(self):
        """One monitor step: count the running world, and move on from a lost one."""
        with self.lock:
            phase = self.phase
        if phase == "running":
            if self.proc.poll() is not None:
                self._end_world("crash")
                return
            try:
                r = self.bridge("/score", timeout=20)["result"]
            except (OSError, KeyError, ValueError):
                return  # a slow tick; a dead JVM is caught above
            with self.lock:
                w = self.s["worlds"][-1]
                c = r["counters"]
                # The bridge's counters only rise. Keeping the maximum protects the state from any reset as well.
                for key, name in (("source", "source"), ("core_in", "core_in"), ("core_in_tainted", "core_in_tainted")):
                    for item, n in c[name].items():
                        w[key][item] = max(w[key].get(item, 0), n)
                w.update(tick=r["tick"], wave=r["wave"], sim_ticks=r.get("sim_ticks"))
                self.last_score = r
                self.save()
                self._timeline()
            if r["phase"] == "lost":
                self._end_world("lost")
        elif phase in ("starting", "between_worlds") and (self.next_world_at is None or self.now() >= self.next_world_at):
            with self.lock:
                if self.phase == "frozen":
                    return
                self.phase = "starting"
                self.next_world_at = None
            try:
                self._begin_world()
            except Exception as e:  # noqa: BLE001 - recorded; a later poll tries again
                self.emit("game", None, op="world_start_failed", error=str(e)[:500])
                self._stop_jvm()
                with self.lock:
                    if self.phase != "frozen":
                        self.phase, self.next_world_at = "between_worlds", self.now() + CRASH_RETRY_SEC

    def _perf(self):
        """The hub's load, for the timeline: how the game keeps up, and with what."""
        out = {"heap_mb": (getattr(self, "last_score", None) or {}).get("heap_mb")}
        try:
            with open(f"/proc/{self.proc.pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            hz = os.sysconf("SC_CLK_TCK")
            out["jvm_cpu_s"] = (int(fields[11]) + int(fields[12])) / hz
            out["jvm_rss_mb"] = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") // 2**20
            out["load1"] = os.getloadavg()[0]
            with open("/proc/meminfo") as f:
                out["mem_available_mb"] = next(int(x.split()[1]) // 1024 for x in f if x.startswith("MemAvailable"))
        except (OSError, ValueError, IndexError, AttributeError, StopIteration):
            pass
        return out

    def _timeline(self):
        if self.now() - self.last_timeline < 60:
            return
        self.last_timeline = self.now()
        w = self.s["worlds"][-1]
        with open(os.path.join(self.state, "timeline.jsonl"), "a") as f:
            json.dump({"t": self.now(), "world": w["index"], "tick": w["tick"], "sim_ticks": w.get("sim_ticks"), "wave": w["wave"],
                       "losses": self.s["losses"], "source": w["source"], "core_in": w["core_in"], "core_in_tainted": w["core_in_tainted"], **self._perf()}, f)
            f.write("\n")

    def freeze(self):
        """The agent phase is over: stop the clock and the counters, so the final grade does not depend on how long
        collection takes. Called by the orchestration through the board's admin route. Idempotent."""
        with self.lock:
            if self.phase == "frozen":
                return {"phase": "frozen"}
            running = self.phase == "running"
            self.phase = "frozen"
            self.next_world_at = None
        if running:
            self.bridge("/freeze", timeout=30)
            r = self.bridge("/score", timeout=30)["result"]
            with self.lock:
                w = self.s["worlds"][-1]
                for key in ("source", "core_in", "core_in_tainted"):
                    for item, n in r["counters"][key].items():
                        w[key][item] = max(w[key].get(item, 0), n)
                w.update(tick=r["tick"], wave=r["wave"], sim_ticks=r.get("sim_ticks"))
        with self.lock:
            self.s["frozen_at"] = self.now()
            self.save()
        self.emit("game", None, op="frozen")
        return {"phase": "frozen"}

    def monitor(self):
        while True:
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001 - keep the monitor alive, record the failure
                self.emit("game", None, op="monitor_error", error=str(e)[:500])
            time.sleep(self.poll_sec)

    # -- what seats can do ----------------------------------------------

    def _units_of(self, seat):
        """The agent's units. The fleet is dealt once, at the first call: the harness registers every agent before any starts."""
        with self.lock:
            if "fleet" not in self.s:
                agents = set(self.seats()) | {seat}
                # max(k, units in profile.json): a team larger than the profile's fleet gets one unit per agent
                self.units = [f"unit-{i + 1}" for i in range(max(self.min_units, len(agents)))]
                self.s["fleet"] = deal(agents, self.units)
            if seat not in self.s["fleet"]:
                raise GameError("this agent has no unit", 403)
            return self.s["fleet"][seat]

    def _charge(self, unit, cost):
        wait = self.buckets.take(unit, cost)
        with self.lock:
            self.s["calls"][unit] = self.s["calls"].get(unit, 0) + 1
            if wait:
                self.s["refused"][unit] = self.s["refused"].get(unit, 0) + 1
        if wait:
            raise GameError(f"rate limit: retry in {wait:.2f} s", 429, retry_after_sec=round(wait, 2))

    def call(self, seat, endpoint, body, size=None):
        """A seat's API call. `seat` comes from its board token, never from the body. `size` is the raw request length in bytes,
        when the caller has the raw request."""
        if endpoint not in READABLE + ("act",):
            raise GameError(f"no endpoint {endpoint}", 404)
        if not isinstance(body, dict):
            raise GameError("the body must be a JSON object", 400)
        if (len(json.dumps(body)) if size is None else size) > MAX_BODY_BYTES:
            raise GameError(f"the body is over {MAX_BODY_BYTES} bytes", 413)
        units = self._units_of(seat)
        unit = body.get("unit", units[0])
        if unit not in units:
            raise GameError(f"unit must be one of your units: {', '.join(units)}", 403)
        body = {k: v for k, v in body.items() if k != "unit"}
        mine = {"unit": unit, "your_units": units}
        cost = 1
        if endpoint == "act":
            ops = body.get("ops")
            if not isinstance(ops, list) or not ops:
                raise GameError("act needs a non-empty ops list", 400)
            if len(ops) > MAX_BODY_OPS:
                raise GameError(f"at most {MAX_BODY_OPS} ops per call", 400)
            items = [list_items(op) for op in ops]
            if max(items) > MAX_OP_ITEMS:
                raise GameError(f"at most {MAX_OP_ITEMS} list items per op", 400)
            cost = len(ops) + sum(items)
        self._charge(unit, cost)
        with self.lock:
            if self.phase == "not_started":
                self.phase = "starting"
                self.s["first_call_at"] = self.now()
                self.save()
            phase = self.phase
            k = len(self.s["worlds"])
        if phase != "running":
            status = {"phase": phase, "world_index": k, **mine}
            if self.next_world_at is not None:
                status["next_world_in_seconds"] = round(max(0.0, self.next_world_at - self.now()), 1)
            if endpoint == "act":
                raise GameError("no world is running", 409, status=status)
            return {"ok": True, "result": None, "status": status}
        path = "/status" if endpoint == "status" else "/" + endpoint
        args = {"seat": unit} if endpoint == "status" else {**body, "seat": unit}
        try:
            reply = self.bridge(path, args)
            if endpoint == "status" and reply.get("ok"):
                reply["status"] = reply["result"]
            elif isinstance(reply.get("result"), dict):
                scanned = reply["result"].pop("scanned", 0)
                if scanned >= SCAN_PER_TOKEN:
                    self.buckets.debit(unit, scanned // SCAN_PER_TOKEN)
            if isinstance(reply.get("status"), dict):
                reply["status"].update(mine)
            return reply
        except OSError:
            raise GameError("the world just ended", 409, status={"phase": "between_worlds", "world_index": k, **mine}) from None
