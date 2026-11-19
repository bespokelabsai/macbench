"""`cr`: the rescue controller's console for the CAVE RESCUE service."""
import argparse, base64, json, os, subprocess, sys, urllib.error, urllib.request

URL = os.environ.get("CAVERESCUE_URL", "http://127.0.0.1:7788/api/")
HEADERS = {"Content-Type": "application/json"}
LAUNCHER = "/usr/local/bin/caverescue-start"
PHOTO_DIR = os.environ.get("CAVERESCUE_PHOTOS", "photos")

def call(op, _retry=True, **body):
    req = urllib.request.Request(URL + op, data=json.dumps(body).encode(), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return 0, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except urllib.error.URLError:
        if _retry and os.path.exists(LAUNCHER):
            subprocess.run([LAUNCHER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return call(op, _retry=False, **body)
        raise

def main(argv=None):
    ap = argparse.ArgumentParser(prog="cr", description="CAVE RESCUE control console")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("briefing", help="the call-out sheet and what the team has")
    sp.add_parser("status", help="clock, drones, stock, rescue teams")
    m = sp.add_parser("map", help="the map so far (rescue survey + what drones have flown), as JSON"); m.add_argument("-o", "--out", default=None)
    e = sp.add_parser("events", help="messages from drones and rescue teams"); e.add_argument("--since", type=int, default=0)
    ps = sp.add_parser("photos", help="list photos received"); ps.add_argument("--since-t", type=float, default=0); ps.add_argument("--drone")
    p = sp.add_parser("photo", help="download a photo to photos/<id>.jpg"); p.add_argument("id")
    f = sp.add_parser("fly", help="fly a drone along mapped passages to a station")
    f.add_argument("drone"); f.add_argument("to"); f.add_argument("--drop", action="store_true", help="drop what it carries on arrival")
    x = sp.add_parser("explore", help="fly a drone to a station, then into the unmapped lead towards a neighbouring station")
    x.add_argument("drone"); x.add_argument("frm", metavar="from"); x.add_argument("toward"); x.add_argument("--drop", action="store_true")
    w = sp.add_parser("wall", help="close-up photo of the left or right wall where the drone is"); w.add_argument("drone"); w.add_argument("side", choices=["left", "right"])
    c = sp.add_parser("charge", help="charge a drone at the entrance or at a placed charger"); c.add_argument("drone")
    l = sp.add_parser("load", help="load a drone at the entrance"); l.add_argument("drone"); l.add_argument("item", choices=["kit", "charger", "relays"]); l.add_argument("n", type=int, nargs="?", default=1)
    d = sp.add_parser("drop", help="drop what a drone carries where it is (relays one at a time)"); d.add_argument("drone")
    r = sp.add_parser("report", help="report people seen in a photo to the incident controller"); r.add_argument("photo")
    rs = sp.add_parser("rescue", help="send a rescue team along a route of stations from the entrance")
    rs.add_argument("route", nargs="*"); rs.add_argument("--route-file", help="file with station ids, one per line or space separated")
    a = ap.parse_args(argv)
    if a.cmd == "photo":
        code, out = call("photo", id=a.id)
        if code == 0:
            os.makedirs(PHOTO_DIR, exist_ok=True)
            path = os.path.join(PHOTO_DIR, f"{out['id']}.jpg")
            open(path, "wb").write(base64.b64decode(out["jpeg_base64"]))
            print(path); return
        print(json.dumps(out, indent=1)); sys.exit(1)
    if a.cmd == "map":
        code, out = call("map")
        if a.out:
            json.dump(out, open(a.out, "w")); print(f"wrote {a.out}: {len(out.get('passages', []))} passages, {len(out.get('stations', {}))} stations, {len(out.get('leads', []))} leads")
        else: print(json.dumps(out))
        sys.exit(0 if code == 0 else 1)
    body = {}
    if a.cmd == "events": body = dict(since=a.since)
    elif a.cmd == "photos": body = {k: v for k, v in dict(since_t=a.since_t, drone=a.drone).items() if v}
    elif a.cmd == "fly": body = dict(drone=a.drone, to=a.to, drop=a.drop)
    elif a.cmd == "explore": body = {"drone": a.drone, "from": a.frm, "toward": a.toward, "drop": a.drop}
    elif a.cmd == "wall": body = dict(drone=a.drone, side=a.side)
    elif a.cmd in ("charge", "drop"): body = dict(drone=a.drone)
    elif a.cmd == "load": body = dict(drone=a.drone, item=a.item, n=a.n)
    elif a.cmd == "report": body = dict(photo=a.photo)
    elif a.cmd == "rescue":
        route = list(a.route)
        if a.route_file: route += open(a.route_file).read().split()
        body = dict(route=route)
    code, out = call(a.cmd, **body)
    print(json.dumps(out, indent=1))
    sys.exit(0 if code == 0 else 1)

if __name__ == "__main__":
    main()
