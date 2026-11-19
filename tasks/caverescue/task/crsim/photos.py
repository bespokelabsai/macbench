"""Render the drone photo library for one scenario.

    python -m crsim.photos SCENARIO.json OUTDIR [--procs N] [--only STATION,...]

One photo per (station, neighbouring station): the view a drone sees standing at the station and
looking down the leg. Views where flood water can reach get one variant per water level (0.25 m steps).
Writes OUTDIR/img/<view>[~<level>].jpg and OUTDIR/views.json with what every photo shows (ground truth).
"""
import json, math, os, sys, time
from multiprocessing import Pool
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cave"))
import render as R
from .world import Cave

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "..", "assets", "fonts")
LEVEL_STEP = 0.5

# ---------- object geometry ----------
def _frame(rc, st, toward):
    p = rc.pos[st].copy(); q = rc.pos[toward].copy() if toward is not None else p + np.array([1.0, 0, 0])
    f = q - p; f[2] = 0; n = np.linalg.norm(f)
    f = f / n if n > 1e-6 else np.array([1.0, 0, 0])
    right = np.array([f[1], -f[0], 0.0])
    return p, f, right

def make_obj(rc, spec, toward, rnd):
    st = spec["station"]
    p, f, right = _frame(rc, st, toward)
    w = rc.w[st]; floor = rc.floor[st]
    side = spec.get("side", rnd.choice([-1, 1]))
    lat = side * max(0.0, w / 2 - 0.45)
    base = p + right * lat; base[2] = floor
    k = spec["kind"]
    P = []
    if k == "person_lying":
        suit = spec["suit"]; helm = spec["helmet"]
        a = base + f * -0.7 + np.array([0, 0, 0.2]); b = base + f * 0.55 + np.array([0, 0, 0.2])
        head = base + f * 0.82 + np.array([0, 0, 0.22])
        P = [(lambda Q, a=a, b=b: R.sd_capsule(Q, a, b, 0.19), suit),
             (lambda Q, c=head: R.sd_sphere(Q, c, 0.12), (0.78, 0.6, 0.5)),
             (lambda Q, c=head + np.array([0, 0, 0.05]) + f * 0.03: R.sd_sphere(Q, c, 0.135), helm),
             (lambda Q, a=a, c=a - f * 0.25 + right * 0.1: R.sd_capsule(Q, a, c, 0.09), (0.12, 0.12, 0.13)),
             (lambda Q, a=a, c=a - f * 0.25 - right * 0.12: R.sd_capsule(Q, a, c, 0.09), (0.12, 0.12, 0.13))]
    elif k == "person_sitting":
        suit = spec["suit"]; helm = spec["helmet"]
        back = base + right * (0.15 * side)
        hip = back + np.array([0, 0, 0.22]); sh = back + np.array([0, 0, 0.72]) + right * (0.06 * side)
        knee = hip - right * (0.45 * side) + np.array([0, 0, 0.18]); foot = knee - right * (0.15 * side) - np.array([0, 0, 0.35])
        head = sh + np.array([0, 0, 0.24])
        P = [(lambda Q, a=hip, b=sh: R.sd_capsule(Q, a, b, 0.2), suit),
             (lambda Q, a=hip + f * 0.12, b=knee + f * 0.12: R.sd_capsule(Q, a, b, 0.1), suit),
             (lambda Q, a=hip - f * 0.12, b=knee - f * 0.12: R.sd_capsule(Q, a, b, 0.1), suit),
             (lambda Q, a=knee, b=foot: R.sd_capsule(Q, a, b, 0.09), (0.12, 0.12, 0.13)),
             (lambda Q, c=head: R.sd_sphere(Q, c, 0.12), (0.78, 0.6, 0.5)),
             (lambda Q, c=head + np.array([0, 0, 0.05]): R.sd_sphere(Q, c, 0.135), helm)]
    elif k == "survival_bag":
        c = base + f * 0.1 + np.array([0, 0, 0.12]) - right * (0.3 * side)
        P = [(lambda Q, c=c: R.sd_box(Q, c, (0.55, 0.3, 0.1), math.atan2(f[1], f[0]), 0.06), (0.95, 0.42, 0.05))]
    elif k == "tackle_bag":
        c = base + np.array([0, 0, 0.0])
        col = rnd.choice([(0.95, 0.75, 0.05), (0.9, 0.35, 0.05), (0.8, 0.1, 0.1)])
        P = [(lambda Q, a=c + np.array([0, 0, 0.15]), b=c + np.array([0, 0, 0.45]): R.sd_capsule(Q, a, b, 0.15), col)]
    elif k == "glove":
        c = base + np.array([0, 0, 0.03]) + f * 0.4
        P = [(lambda Q, c=c: R.sd_box(Q, c, (0.12, 0.07, 0.025), rnd.uniform(0, 3), 0.015), rnd.choice([(0.9, 0.55, 0.1), (0.2, 0.3, 0.6), (0.85, 0.85, 0.2)]))]
    elif k == "food_wrapper":
        c = base + np.array([0, 0, 0.015]) + f * 0.3
        P = [(lambda Q, c=c: R.sd_box(Q, c, (0.09, 0.05, 0.01), rnd.uniform(0, 3), 0.005), (0.85, 0.85, 0.9))]
    elif k == "old_bag":
        c = base
        col = rnd.choice([(0.55, 0.42, 0.25), (0.35, 0.38, 0.45), (0.62, 0.45, 0.2), (0.7, 0.55, 0.15)])
        P = [(lambda Q, a=c + np.array([0, 0, 0.14]), b=c + np.array([0, 0, 0.4]): R.sd_capsule(Q, a, b, 0.14), col)]
    elif k == "rope_coil":
        c = base + np.array([0, 0, 0.05])
        col = rnd.choice([(0.9, 0.9, 0.85), (0.75, 0.2, 0.15), (0.2, 0.4, 0.75), (0.9, 0.6, 0.1)])
        P = [(lambda Q, c=c: R.sd_torus(Q, c, 0.2, 0.05), col)]
    elif k == "barrel":
        c = base
        P = [(lambda Q, a=c + np.array([0, 0, 0.2]), b=c + np.array([0, 0, 0.55]): R.sd_capsule(Q, a, b, 0.2), (0.15, 0.3, 0.7))]
    elif k == "survey_tape":
        c = base + np.array([0, 0, 0.6]) + right * (0.3 * side)
        P = [(lambda Q, c=c: R.sd_box(Q, c, (0.02, 0.012, 0.15), 0, 0.004), (0.9, 0.15, 0.2))]
    elif k == "tarp":
        c = base + np.array([0, 0, 0.04])
        P = [(lambda Q, c=c: R.sd_box(Q, c, (0.7, 0.5, 0.04), rnd.uniform(0, 3), 0.03), (0.2, 0.35, 0.65))]
    else:
        raise ValueError(k)
    return R.Obj(P), base

def make_decal(rc, spec, rnd):
    """Arrows and short marks sit at the mouth of the passage they point into, big enough to read in a
    passage photo. Notes sit on the wall at the station and are only legible in a close-up wall photo."""
    st, toward = spec["station"], spec["toward"]
    p, f, right = _frame(rc, st, toward)
    w, h, floor = rc.w[st], rc.h[st], rc.floor[st]
    side = spec.get("side") or (1 if (int(spec["id"][1:]) % 2) else -1)   # v5 trail marks carry a checked side
    lines = spec["lines"]
    if spec["kind"] == "party_note":
        c = p + right * side * (w / 2); c[2] = floor + min(1.1, max(0.4, h * 0.45))
        normal = -right * side
        px_m = 0.0021
    else:
        leg = np.linalg.norm(rc.pos[toward] - rc.pos[st])
        ahead = spec.get("ahead", min(1.6, 0.5 * leg)); inset = spec.get("inset", 1.0)
        c = p + f * ahead + right * side * (w / 2) * inset; c[2] = floor + min(1.0, max(0.35, h * 0.4))
        normal = -right * side - f * 0.7
        normal /= np.linalg.norm(normal)
        px_m = 0.0062
    up = np.array([0, 0, 1.0])
    if "at" in spec:        # v5 trail marks: placed on a rendered rock surface (scratch/v5_trailcheck.py)
        c = np.array(spec["at"]); normal = np.array(spec["nrm"]); up = np.array(spec["upv"]); px_m = spec["scale"]
        if np.cross(up, normal) @ f < 0:
            lines = [s.replace("\u2192", "\u0000").replace("\u2190", "\u2192").replace("\u0000", "\u2190") for s in lines]
    elif spec.get("floor"):   # v5: in big chambers the mark is chalked on the floor ahead, reading down the passage
        c = p + f * spec.get("ahead", 2.0); c[2] = floor + 0.03
        normal = np.array([0, 0, 1.0]); up = -right; px_m = spec.get("scale", 0.012)
    rt = np.cross(np.array([0, 0, 1.0]), normal)
    if "at" not in spec and not spec.get("floor") and rt @ f < 0:   # keep arrows pointing down the passage they mark
        lines = [s.replace("\u2192", "\u0000").replace("\u2190", "\u2192").replace("\u0000", "\u2190") for s in lines]
    font = ImageFont.truetype(os.path.join(FONTS, spec["font"]), 48)
    W_ = max(int(font.getlength(s)) for s in lines) + 24; H_ = 58 * len(lines) + 16
    im = Image.new("L", (W_, H_), 0); d = ImageDraw.Draw(im)
    for i, s in enumerate(lines): d.text((12, 6 + i * 58), s, fill=255, font=font)
    im = im.rotate(rnd.uniform(-5, 5), resample=Image.BILINEAR, expand=False)
    arr = np.array(im)
    size = (W_ * px_m, H_ * px_m)
    # the party writes in black marker; old expedition marks are a mix of carbide soot and chalk
    if spec["party"]: colour = (0.06, 0.06, 0.07)
    else: colour = (0.1, 0.09, 0.08) if rnd.random() < 0.6 else (0.93, 0.92, 0.88)
    return dict(center=c, normal=normal, up=up, size=size, image=arr, colour=colour, alpha=0.85)

# ---------- views ----------
def view_list(cave):
    out = []
    for i in range(cave.n):
        for j, L, pi in cave.adj[i]:
            out.append((i, j))
        out += [(i, "L"), (i, "R")]           # close-ups of the left and right wall at the station
    return out

def _view_target(rc, cave, i, j):
    # look ~6 m down the passage: walk on past j if the leg is short
    prev, cur, dist = i, j, np.linalg.norm(rc.pos[j] - rc.pos[i])
    while dist < 5 and len(cave.adj[cur]) == 2:
        nxt = [v for v, _, _ in cave.adj[cur] if v != prev][0]
        dist += np.linalg.norm(rc.pos[nxt] - rc.pos[cur]); prev, cur = cur, nxt
    return rc.pos[cur]

G = {}
def _init(sc_path):
    sc = json.load(open(sc_path)); G["sc"] = sc
    g = json.load(open(sc["cave"])); G["rc"] = R.Cave(g); G["cave"] = Cave(sc["cave"])

def _levels_for(sc, cave, rc, i, j):
    """Distinct water levels (rounded) that can show in view i->j over the episode."""
    bas = sc["basin"]; out = set()
    sts = {i, j}
    lo = min(rc.floor[s] for s in sts)
    eye_z = rc.floor[i] + min(max(rc.h[i] * 0.55, 0.25), 1.3)
    for t, lv in sc["flood"]:
        if t < 0: continue
        m = max((lv.get(str(bas[s]), lv.get(bas[s])) or -1e9) for s in sts)
        if m > lo + 0.05:
            q = round(math.floor(m / LEVEL_STEP) * LEVEL_STEP, 2)
            if q > lo and q < eye_z - 0.05: out.add(q)    # higher water and the drone would be under it
    return sorted(out)

def render_view(args):
    i, j, outdir = args[:3]
    gone_only = args[3] if len(args) > 3 else None
    sc, rc, cave = G["sc"], G["rc"], G["cave"]
    rnd = __import__("random").Random(hash((sc["seed"], i, j)) & 0xffffffff)
    eye = rc.pos[i].copy(); eye[2] = rc.floor[i] + min(max(rc.h[i] * 0.55, 0.25), 1.3)
    if j in ("L", "R"):
        nb = cave.adj[i][0][0]
        _, f, right = _frame(rc, i, nb)
        s = -1 if j == "L" else 1
        eye = eye - right * s * min(0.6, rc.w[i] * 0.3)
        tgt = eye + right * s * 2.0
    else:
        tgt = _view_target(rc, cave, i, j).copy(); tgt[2] = rc.floor[j] + min(max(rc.h[j] * 0.5, 0.25), 1.3)
    near_obj = [o for o in sc["objects"] if np.linalg.norm(rc.pos[o["station"]] - eye) < 30]
    near_dec = [d for d in sc["decals"] if np.linalg.norm(rc.pos[d["station"]] - eye) < 25]
    objs, meta_o = [], []
    for o in near_obj:
        nb = cave.adj[o["station"]][0][0] if cave.adj[o["station"]] else None
        ob, _ = make_obj(rc, o, nb, __import__("random").Random(hash(o["id"]) & 0xffff))
        objs.append(ob); meta_o.append(o)
    decals = [make_decal(rc, d, __import__("random").Random(hash(d["id"]) & 0xffff)) for d in near_dec]
    res = {"view": f"{i}-{j}", "from": i, "to": j, "variants": []}
    if gone_only:
        # the same view after a group has left (rescued): their people and kit are no longer there
        keep = [k for k, o in enumerate(meta_o) if o.get("group") != gone_only]
        objs = [objs[k] for k in keep]; meta_o = [meta_o[k] for k in keep]
    for lvl in [None] + (_levels_for(sc, cave, rc, i, i) if j in ("L", "R") else _levels_for(sc, cave, rc, i, j)):
        stats = {}
        im = R.render(rc, eye, tgt, objs=objs, decals=decals, water_z=lvl, seed=sc["seed"] * 1000 + i, stats=stats)
        name = f"{i}-{j}" + ("" if lvl is None else f"~{lvl:.2f}") + (f"-no{gone_only}" if gone_only else "")
        im.save(os.path.join(outdir, "img", name + ".jpg"), quality=82)
        shows = {}
        for k, o in enumerate(meta_o):
            px = stats["obj_pixels"].get(k, 0)
            if px >= 40: shows[o["id"]] = px
        dshow = {near_dec[k]["id"]: px for k, px in enumerate(stats["decal_pixels"]) if px >= 60}
        res["variants"].append({"level": lvl, "file": name + ".jpg", "objects": shows, "decals": dshow, **({"gone": gone_only} if gone_only else {})})
    return res

def add_gone(sc_path, outdir, procs):
    """Extra variants, for every view that shows a group, with that group gone (after a rescue)."""
    _init(sc_path)
    vp = os.path.join(outdir, "views.json"); V = json.load(open(vp))
    grp = {o["id"]: o["group"] for o in G["sc"]["objects"] if o.get("group")}
    jobs = []
    for v in V:
        v["variants"] = [x for x in v["variants"] if not x.get("gone")]
        gs = {grp[o] for x in v["variants"] for o in x["objects"] if o in grp}
        jobs += [(v["from"], v["to"], outdir, g) for g in sorted(gs)]
    with Pool(procs, initializer=_init, initargs=(sc_path,)) as pool:
        res = pool.map(render_view, jobs)
    by = {v["view"]: v for v in V}
    for r in res: by[r["view"]]["variants"] += r["variants"]
    json.dump(V, open(vp, "w")); print("gone variants", sum(len(r["variants"]) for r in res), "for", len(jobs), "view/group pairs")

def main():
    sc_path, outdir = sys.argv[1], sys.argv[2]
    if "--gone" in sys.argv:
        return add_gone(sc_path, outdir, int(sys.argv[sys.argv.index("--procs") + 1]) if "--procs" in sys.argv else 8)
    procs = int(sys.argv[sys.argv.index("--procs") + 1]) if "--procs" in sys.argv else 8
    only = set(int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    os.makedirs(os.path.join(outdir, "img"), exist_ok=True)
    _init(sc_path)
    views = [(i, j, outdir) for i, j in view_list(G["cave"]) if only is None or i in only]
    t0 = time.time(); out = []
    with Pool(procs, initializer=_init, initargs=(sc_path,)) as pool:
        for k, r in enumerate(pool.imap_unordered(render_view, views, chunksize=4)):
            out.append(r)
            if k % 200 == 0: print(f"{k}/{len(views)} {time.time() - t0:.0f}s", flush=True)
    json.dump(sorted(out, key=lambda r: (r["from"], str(r["to"]))), open(os.path.join(outdir, "views.json"), "w"))
    print("done", len(out), f"{time.time() - t0:.0f}s")

if __name__ == "__main__":
    main()
