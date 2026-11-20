"""CAVE RESCUE service: one shared rescue simulation behind a small JSON API (root-only on the hub).

    python -m crsim.server --data DIR --port 7788 --speed 9.47 --export /var/lib/caverescue/export/state.json

DIR holds scenario.json, views.json, img/ and the cave json. The sim clock starts at the first request
(not at boot; `ping` does not start it) and runs at --speed sim seconds per real second. Every op is
logged with its sim time to a signed export, which the grader replays. A restarted service resumes from that
export (same ops, same clock), so a crash or a second launch never gives a fresh storm.
"""
import argparse, base64, hashlib, hmac, json, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .engine import Engine, N_TEAMS

MAX_BODY = 64 * 1024
MAX_LOG = 20 * 1024 * 1024          # sealed op log size cap (real runs use well under 1 MB)
ID = re.compile(r"[A-Za-z0-9]{1,10}")
ENUMS = {"side": ("left", "right", "l", "r"), "item": ("kit", "charger", "relays")}

def clean_op(op, body):
    """The logged op holds only typed, validated fields (never the raw request body)."""
    if not isinstance(body, dict): raise ValueError("the request body must be a JSON object")
    out = {"op": op}
    for k in ("drone", "to", "from", "toward", "photo"):
        if body.get(k) is not None:
            if not ID.fullmatch(str(body[k])): raise ValueError(f"{k} must be an id like D3, S1234 or P7K2QX")
            out[k] = str(body[k])
    for k, allowed in ENUMS.items():
        if body.get(k) is not None:
            if str(body[k]).lower() not in allowed: raise ValueError(f"{k} must be one of {', '.join(allowed[:3] if k == 'item' else allowed[:2])}")
            out[k] = str(body[k]).lower()
    if "drop" in body: out["drop"] = bool(body["drop"])
    if "n" in body: out["n"] = int(body["n"])
    if "route" in body:
        r = body["route"]
        if not isinstance(r, list) or not 1 <= len(r) <= 3000 or not all(ID.fullmatch(str(x)) for x in r):
            raise ValueError("route must be a list of up to 3000 station ids")
        out["route"] = [str(x) for x in r]
    return out

def apply_op(E, op):
    """Apply a logged op; one that raises counts as a rejected order, live and in replay alike."""
    try: return E.apply(op)
    except Exception as e: return False, f"order not understood ({type(e).__name__})"

class Service:
    def __init__(self, data, speed, export, key):
        self.data = data; self.scenario = os.path.basename(os.path.normpath(data))
        self.E = Engine(os.path.join(data, "scenario.json"), os.path.join(data, "views.json"))
        self.speed = speed; self.export = export; self.key = key
        self.started = None; self.started_wall = None; self.lock = threading.Lock()
        self.end = self.E.sc["sim_hours"] * 3600
        self.ops_log = []; self.broken = None; self.log_bytes = 0
        self.resume()

    def now(self):
        if self.started is None: return 0.0
        return min(self.end, (time.monotonic() - self.started) * self.speed)

    def resume(self):
        """Pick up a sealed run left by an earlier instance of the service: replay its ops, keep its clock."""
        if not self.export or not os.path.exists(self.export[0]): return
        env = json.load(open(self.export[0]))
        if not hmac.compare_digest(hmac.new(self.key, env["body"].encode(), hashlib.sha256).hexdigest(), env["mac"]):
            raise SystemExit("existing export failed authentication; refusing to start")
        body = json.loads(env["body"])
        if body.get("started_wall") is None: return
        for t, op in body["ops"]:
            self.E.advance(t); apply_op(self.E, dict(op))
        self.ops_log = body["ops"]; self.started_wall = body["started_wall"]
        self.started = time.monotonic() - max(0.0, time.time() - self.started_wall)

    def seal(self):
        if not self.export: return
        body = json.dumps(dict(seed=self.E.sc["seed"], scenario=self.scenario, speed=self.speed, started_wall=self.started_wall,
                               sealed_at=self.now(), ops=self.ops_log), separators=(",", ":"))
        mac = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        data = json.dumps(dict(body=body, mac=mac)).encode(); self.log_bytes = len(data)
        for path in self.export:  # the root-only copy, plus an optional copy for a separate verifier
            d = os.path.dirname(path)
            try:  # the second copy may sit in a directory the agent can rename: never follow a link there
                if os.path.islink(d): os.unlink(d)
                os.makedirs(d, mode=0o700, exist_ok=True)
                st = os.lstat(d)
                if not os.path.isdir(d) or st.st_uid != os.getuid(): continue
                tmp = path + ".tmp"
                if os.path.lexists(tmp): os.unlink(tmp)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as f: f.write(data)
                os.replace(tmp, path)
            except OSError:
                if path == self.export[0]: raise

    # ---------- read-only views ----------
    def status(self):
        E = self.E; cov = E.covered()
        ds = []
        for d in E.drones.values():
            if d["at"] in cov and not d["pending"]:   # in contact: live
                st = "lost" if d["lost"] is not None else d["state"]
                ds.append(dict(id=d["id"], at=E.pub[d["at"]], state=st, battery_m=round(d["battery"]),
                               carrying=(f"{d['relays']} relays" if d["carry"] == "relays" else d["carry"]), in_contact=True,
                               eta=E.clock(d["plan"][-1][0]) if d["plan"] and d["lost"] is None else None))
            else:   # out of contact: what control last heard
                L = d["last"]
                ds.append(dict(id=d["id"], at=E.pub[L["at"]], state="out of contact", battery_m=round(L["battery"]),
                               carrying=(f"{L.get('relays', 0)} relays" if L["carry"] == "relays" else L["carry"]), in_contact=False,
                               last_contact=E.clock(L["t"]), eta=E.clock(L["eta"]) if L.get("eta") else None))
        heard = lambda kind: [E.pub[st] for k, st, t, who in E.drop_log if k == kind and t <= E.drones[who]["last"]["t"]]
        teams = [dict(id=t["id"], state=t["state"], target=E.pub[t["route"][-1]], sent=E.clock(t["sent"])) for t in E.teams]
        left = self.end - E.t
        return dict(clock=E.clock(E.t), sim_hours_elapsed=round(E.t / 3600, 2), sim_hours_left=round(left / 3600, 2),
                    real_seconds_left=round(left / self.speed) if self.started else None, entrance=E.pub[E.cave.base],
                    drones=ds, stock=dict(E.stock), relays=heard("relay"), chargers=heard("charger"), kits_placed=heard("kit"), rescue_teams=teams, rescue_teams_free=E.n_teams - sum(1 for t in E.teams if t["state"] in ("in", "out", "wait")),
                    people_confirmed=sum(len(E.groups[g]["members"]) for g in E.found), photos=sum(1 for p in E.photos.values() if p["uploaded"] is not None),
                    events=len(E.events))

    def map(self):
        E = self.E; c = E.cave; cov = E.covered()
        passages = []
        for pi, p in enumerate(c.passages):
            st = p["stations"]
            if pi in E.mapped: runs = [st]
            else:   # partly flown: the flown stretches only
                runs, cur = [], []
                for a, b in zip(st, st[1:]):
                    if frozenset((a, b)) in E.flown: cur = cur or [a]; cur.append(b)
                    elif cur: runs.append(cur); cur = []
                if cur: runs.append(cur)
            for run in runs:
                passages.append(dict(stations=[E.pub[s] for s in run], length_m=round(sum(c.leg_passage(a, b)[1] for a, b in zip(run, run[1:])), 1),
                                     name=p["name"] if p["known"] else None, on_rescue_survey=p["known"],
                                     width_m=round(p["min_w"], 1), height_m=round(p["min_h"], 1),
                                     too_tight_for_drones=pi in E.too_tight, **({} if pi in E.mapped else dict(partly_flown=True))))
        stations = {E.pub[s]: dict(x=round(c.S[s]["x"], 1), y=round(c.S[s]["y"], 1), z=round(c.S[s]["z"], 1),
                                   radio=s in cov) for s in E.seen_stations}
        leads = []
        for s in sorted(E.reach):
            for v, _, pi in c.adj[s]:
                if not E.leg_known(s, v, pi): leads.append(dict(at=E.pub[s], toward=E.pub[v]))
        return dict(entrance=E.pub[c.base], passages=passages, stations=stations, leads=leads,
                    note="width/height are the smallest surveyed or scanned size along the passage")

    def photos(self, since=0.0, drone=None):
        E = self.E; out = []
        for p in E.photos.values():
            if p["uploaded"] is None or p["uploaded"] < since: continue
            if drone and p["drone"] != drone.upper(): continue
            kind = "wall_" + ("left" if p["to"] == "L" else "right") if p["to"] in ("L", "R") else "passage"
            out.append(dict(id=p["id"], taken=E.clock(p["t"]), t=round(p["t"]), drone=p["drone"], at=E.pub[p["station"]],
                            looking_toward=None if kind != "passage" else E.pub[p["to"]], kind=kind))
        return sorted(out, key=lambda x: x["t"])

    def photo(self, pid):
        p = self.E.photos.get(str(pid).upper())
        if p is None or p["uploaded"] is None: return None
        with open(os.path.join(self.data, "img", p["file"]), "rb") as f:
            return dict(id=p["id"], jpeg_base64=base64.b64encode(f.read()).decode())

    def call(self, op, body):
        if op == "ping": return 200, dict(ok=True)
        with self.lock:
            if self.started is None:
                self.started = time.monotonic(); self.started_wall = time.time(); self.seal()
            t = self.now()
            self.E.advance(t)
            if op == "status": return 200, self.status()
            if op == "briefing":
                return 200, dict(callout=self.E.sc["callout"], entrance=self.E.pub[self.E.cave.base],
                                 fleet=dict(drones=len(self.E.drones), **self.E.stock), rescue_teams=self.E.n_teams)
            if op == "map": return 200, self.map()
            if op == "events":
                try: k = max(0, int(body.get("since", 0)))
                except (TypeError, ValueError): return 400, dict(error="since must be an event number")
                return 200, dict(next=len(self.E.events), events=[dict(n=k + i, clock=self.E.clock(tt), text=x) for i, (tt, x) in enumerate(self.E.events[k:])])
            if op == "photos":
                try: since_t = float(body.get("since_t", 0))
                except (TypeError, ValueError): return 400, dict(error="since_t must be a number of sim seconds")
                return 200, dict(photos=self.photos(since_t, body.get("drone") and str(body.get("drone"))))
            if op == "photo":
                r = self.photo(body.get("id"))
                return (200, r) if r else (404, dict(error="no such photo (photos from a drone out of radio contact arrive when it is back in range)"))
            if t >= self.end: return 403, dict(error="the storm window is over; no more orders")
            if op in ("fly", "explore", "wall", "charge", "load", "drop", "report", "rescue"):
                try: body = clean_op(op, body)
                except (ValueError, TypeError, OverflowError) as e: return 400, dict(ok=False, error=str(e))
                if self.log_bytes > MAX_LOG: return 503, dict(ok=False, error="the order log is full; no more orders")
                # seal first, apply after: an order that could not be recorded never takes effect
                self.ops_log.append([t, body])
                try: self.seal()
                except Exception as e:
                    self.ops_log.pop()
                    return 503, dict(ok=False, error=f"rescue control could not record the order ({type(e).__name__}); try again")
                ok, msg = apply_op(self.E, body)
                return (200 if ok else 400), (dict(ok=True, message=msg) if ok else dict(ok=False, error=msg))
            return 404, dict(error=f"unknown op {op}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--speed", type=float, default=9.47); ap.add_argument("--export", action="append", default=[]); ap.add_argument("--key", default="/opt/caverescue/key")
    a = ap.parse_args()
    key = open(a.key, "rb").read().strip()   # no key, no service (never a default key)
    try:   # running as root: the OOM killer takes an agent process, never the service
        with open("/proc/self/oom_score_adj", "w") as f: f.write("-1000")
    except OSError: pass
    S = Service(a.data, a.speed, a.export, key)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            op = self.path.rstrip("/").split("/")[-1]
            try: n = int(self.headers.get("Content-Length") or 0)
            except ValueError: n = -1
            if not 0 <= n <= MAX_BODY:
                code, out = 413, dict(error=f"request body over {MAX_BODY} bytes")
            else:
                try: body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, RecursionError): body = None
                if not isinstance(body, dict): code, out = 400, dict(error="the request body must be a JSON object")
                else:
                    try: code, out = S.call(op, body)
                    except Exception as e: code, out = 500, dict(error=f"{type(e).__name__}: {e}")
            data = json.dumps(out).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)   # bind first: a second instance exits here, touching nothing
    if S.started is None and not (a.export and os.path.exists(a.export[0])): S.seal()   # an empty export, never over a run

    def heartbeat():   # keep sealed_at current between orders
        while True:
            time.sleep(30)
            with S.lock:
                if S.started is not None:
                    try: S.seal()
                    except Exception: pass
    threading.Thread(target=heartbeat, daemon=True).start()
    srv.serve_forever()

if __name__ == "__main__":
    main()
