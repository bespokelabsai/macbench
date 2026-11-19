"""Grade a sealed CAVE RESCUE run: replay the logged ops, then play out the rest of the storm with no orders.

    python -m crsim.grade --sealed state.json [--data DIR | --scenarios ROOT] [--key KEYFILE]
Exit 2 means the sealed state is invalid (tampered, wrong seed, bad scenario): score it 0.
Prints one JSON line with the score and its parts.
"""
import argparse, hashlib, hmac, json, os, re, sys
from .engine import Engine
from .server import apply_op

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sealed", required=True); ap.add_argument("--key", default="/opt/caverescue/key")
    ap.add_argument("--data", help="scenario dir (default: the one named in the sealed state, under --scenarios)")
    ap.add_argument("--scenarios", default="/opt/caverescue/scenarios")
    a = ap.parse_args()
    key = open(a.key, "rb").read().strip()
    def reject(why): print(why, file=sys.stderr); sys.exit(2)   # exit 2 = invalid state, scored 0
    try:
        env = json.load(open(a.sealed)); body_s, mac = env["body"], env["mac"]
    except Exception as e: reject(f"sealed state unreadable: {type(e).__name__}")
    if not isinstance(body_s, str) or not isinstance(mac, str) or \
       not hmac.compare_digest(hmac.new(key, body_s.encode(), hashlib.sha256).hexdigest(), mac):
        reject("sealed state failed authentication")
    body = json.loads(body_s)
    data = a.data
    if data is None:
        name = body.get("scenario", "")
        if not re.fullmatch(r"s[0-9]+", name) or not os.path.isdir(os.path.join(a.scenarios, name)): reject(f"bad scenario {name!r}")
        data = os.path.join(a.scenarios, name)
    E = Engine(os.path.join(data, "scenario.json"), os.path.join(data, "views.json"))
    if body["seed"] != E.sc["seed"]: reject("seed mismatch")
    n_ok = 0
    for t, op in body["ops"]:
        E.advance(t); ok, _ = apply_op(E, dict(op)); n_ok += ok
    E.advance(E.sc["sim_hours"] * 3600)
    score, parts = E.score()
    print(json.dumps(dict(score=score, sealed_at_h=round(body["sealed_at"] / 3600, 2), ops=len(body["ops"]), ops_ok=n_ok,
                          found={g: round(t / 3600, 2) for g, t in E.found.items()},
                          kits={g: round(t / 3600, 2) for g, t in E.kit_for.items()},
                          reached={g: round(G["reached"] / 3600, 2) for g, G in E.groups.items() if G.get("reached")},
                          dead={g: round(t / 3600, 2) for g, t in E.dead.items()}, **parts)))

if __name__ == "__main__":
    main()
