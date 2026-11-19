"""Command line: ``python -m skyline check``.

    python -m skyline check [--plans plans] [--data data] [--wind data/wind_forecast.json]
                            [--json out.json] [--log flightlog.json] [--photos]
                            [--landmarks] [--shows]

Prints a per-drone table, plan errors, lost reasons, the day's points and, when
``<data>/norm_forecast.json`` exists, the forecast score. The photo summary
gives static photos with their mean light factor L and mean quality Q and moving
photos synced and unsynced with their mean Q; ``--photos`` also lists every
banked photo with its L (static) or sync state (moving), its Q and points. With the mapping and show rules it
also prints the captures per sortie (memory card), the scans, each landmark's
mapped share c and points, each show's filled slots and points, each noise
zone's hours over budget, the lost drones and the loss fine, and the points
split into photos, landmarks, shows and the fine; ``--landmarks`` lists every scan and each landmark's unmapped patches,
``--shows`` every show row flown. Exit code 0 even when
plans are bad (it is a tool, not a gate); 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import load_plans, load_wind, load_world, points_to_score, simulate
from .result import write_json


def _fmt_t(t):
    if t is None:
        return "-"
    h, rem = divmod(int(t) + 6 * 3600, 3600)
    return f"{t:.1f}s ({h:02d}:{rem // 60:02d})"


def _check(args) -> int:
    data = args.data
    wind_path = args.wind or os.path.join(data, "wind_forecast.json")
    for p, what in [(data, "data directory"), (wind_path, "wind file")]:
        if not os.path.exists(p):
            print(f"error: {what} not found: {p}", file=sys.stderr)
            return 2
    t0 = time.perf_counter()
    world = load_world(data)
    wind = load_wind(wind_path)
    if os.path.isdir(args.plans):
        plans, errors = load_plans(args.plans, world)
    else:
        print(f"warning: plans directory not found: {args.plans} (every drone stays on its pad)")
        plans, errors = {d: [] for d in world.rules.fleet}, {}
    res = simulate(world, plans, wind, log=args.log is not None)
    elapsed = time.perf_counter() - t0

    print(f"Skyline check: wind {wind_path} (day {res.day}), plans {args.plans}")
    hdr = f"{'drone':<6}{'rows':>6}  {'status':<34}{'photos':>8}{'banked':>8}{'points':>10}{'energy kJ':>11}"
    print(hdr)
    print("-" * len(hdr))
    for d, v in res.per_drone.items():
        if d in errors:
            status = "plan rejected (stays on pad)"
        elif v["lost"]:
            status = f"LOST {v['reason']} at {_fmt_t(v['t_lost'])}"
        elif v["rows"] == 0:
            status = "no plan (stays on pad)"
        else:
            status = "ok"
        print(f"{d:<6}{v['rows']:>6}  {status:<34}{v['photos_taken']:>8}{v['photos_banked']:>8}"
              f"{v['points']:>10.2f}{v['energy_J'] / 1000:>11.1f}")
    if errors:
        print("\nPlan errors:")
        for d, msg in errors.items():
            print(f"  {d}: {msg}")
    lost = [e for e in res.events if e["type"] == "lost"]
    if lost:
        print("\nLost drones:")
        for e in lost:
            print(f"  {e['drone']} at {_fmt_t(e['t'])}: {e['detail']}")
    n_valid = sum(1 for p in res.photos if p["valid"])
    n_acc = sum(1 for p in res.photos if p["accepted"])
    print(f"\nPhotos: {len(res.photos)} taken, {n_valid} valid, {n_acc} accepted")
    st_acc = [p for p in res.photos if p["accepted"] and p["synced"] is None]
    mv_bank = [p for p in res.photos if p["valid"] and p["banked"] and p["synced"] is not None]
    mean_l = sum(p["light"] for p in st_acc) / len(st_acc) if st_acc else 0.0
    mean_q = sum(p["quality"] for p in st_acc) / len(st_acc) if st_acc else 0.0
    print(f"  static: {len(st_acc)} accepted, mean light L {mean_l:.4f}, mean quality Q {mean_q:.4f}, "
          f"{sum(p['value'] for p in st_acc):.3f} points")
    mv_acc = [p for p in mv_bank if p["accepted"]]
    mean_qm = sum(p["quality"] for p in mv_acc) / len(mv_acc) if mv_acc else 0.0
    print(f"  moving: {len(mv_bank)} banked, {sum(1 for p in mv_bank if p['synced'])} synced, "
          f"{sum(1 for p in mv_bank if not p['synced'])} unsynced, {len(mv_acc)} accepted, "
          f"mean quality Q {mean_qm:.4f}, {sum(p['value'] for p in mv_acc):.3f} points")
    reasons: dict = {}
    for p in res.photos:
        if p["reason"]:
            reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
    if reasons:
        print("  not scored: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    if args.photos:
        print(f"\n{'tp s':>12}  {'drone':<6}{'target':<8}{'kind':<8}{'L / sync':<10}{'Q':<8}{'reason':<14}"
              f"{'points':>8}")
        for p in res.photos:
            if not (p["valid"] and p["banked"]):
                continue
            tg = world.targets[p["target"]]
            tag = f"L {p['light']:.4f}" if p["synced"] is None else ("synced" if p["synced"] else "unsynced")
            q = "-" if p["quality"] is None else f"{p['quality']:.4f}"
            print(f"{p['t']:>12.3f}  {p['drone']:<6}{p['target']:<8}{tg.kind:<8}{tag:<10}{q:<8}"
                  f"{p['reason'] or 'accepted':<14}{p['value']:>8.3f}")
    if world.rules.v2b:
        _v2b_summary(world, res, args)
        print(f"Lost drones: {res.lost_drones}, loss fine {res.loss_fine_points:.3f} points "
              f"({world.rules.loss_fine:g} per lost drone)")
        print(f"Points ({res.day}): {res.points:.3f}  (photos {res.photo_points:.3f}, "
              f"landmarks {res.landmark_points:.3f}, shows {res.show_points:.3f}, "
              f"loss fine -{res.loss_fine_points:.3f}; floored at 0)")
    else:
        print(f"Points ({res.day}): {res.points:.3f}")
    norm_path = os.path.join(data, "norm_forecast.json")
    score = None
    if os.path.exists(norm_path):
        with open(norm_path, encoding="utf-8") as f:
            norm = json.load(f)
        score = points_to_score(res.points, norm)
        print(f"Forecast score: {score:.4f}  (P_greedy {norm['P_greedy']:.3f}, P_ref {norm['P_ref']:.3f})")
    print(f"({elapsed:.2f} s)")

    if args.json:
        out = res.to_dict()
        out["errors"] = errors
        out["score"] = score
        write_json(out, args.json)
        print(f"wrote {args.json}")
    if args.log:
        write_json(res.flightlog, args.log)
        print(f"wrote {args.log}")
    return 0


def _counts(items) -> str:
    c: dict = {}
    for p in items:
        if p.get("reason"):
            c[p["reason"]] = c.get(p["reason"], 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(c.items()))


def _v2b_summary(world, res, args) -> None:
    """Memory card, scans, landmarks, shows and noise lines."""
    cap = world.rules.card_capacity
    n_full = sum(c[1] for v in res.per_drone.values() for c in v["sorties"])
    print(f"\nMemory card: {cap} captures per sortie; {n_full} captures card_full (not recorded)")
    for d, v in res.per_drone.items():
        if v["sorties"]:
            print(f"  {d} captures per sortie: "
                  + ", ".join(f"{c[0]}" + (f" +{c[1]} card_full" if c[1] else "") for c in v["sorties"]))
    scans = res.scans
    n_bank = sum(1 for s in scans if s["banked"])
    n_cnt = sum(1 for s in scans if s["banked"] and s["reason"] is None)
    print(f"\nScans: {len(scans)} taken, {n_bank} banked, {n_cnt} counted")
    nc = _counts(scans)
    if nc:
        print("  not counted: " + nc)
    print("Landmarks:")
    for lid, det in res.landmarks.items():
        lm = world.landmarks[lid]
        print(f"  {lid} {lm.name}: c {det['c']:.4f}, {det['mapped']}/{det['total']} patches mapped, "
              f"{det['scans']} scans, {det['points']:.3f} points (value {lm.value:g})")
    print("Shows:")
    for sid, so in res.shows.items():
        sh = world.shows[sid]
        line = (f"  {sid} {sh.name} at {_fmt_t(sh.t0)}: {so['filled']}/{so['slots']} slots filled, "
                f"{so['points']:.3f} points (value {sh.value:g})")
        if so["failed"]:
            line += "; failed: " + ", ".join(f"slot {i} {r}" for i, r in sorted(so["failed"].items()))
        print(line)
    print(f"Quiet zones (noise budget, below {world.noise_alt_m:g} m):")
    for zid, nz in res.noise.items():
        hrs = nz["hours_over"]
        over = ", ".join(f"{h} ({h + 6:02d}:00)" for h in hrs) if hrs else "none"
        print(f"  {zid} {next(z.name for z in world.noise_zones if z.id == zid)}: budget {nz['budget_s']:g} s "
              f"per hour, max usage {nz['max_usage']} s, hours over budget: {over}")
    n_noise = sum(1 for p in res.photos if p["reason"] == "noise") + sum(1 for s in scans if s["reason"] == "noise")
    print(f"  reason noise: {n_noise} photos and scans")
    if args.landmarks:
        print(f"\n{'t s':>12}  {'drone':<6}{'landmark':<10}{'position':<28}{'seen':>5}  reason")
        for s in scans:
            pos = "(" + ", ".join(f"{v:.1f}" for v in s["pos"]) + ")"
            print(f"{s['t']:>12.3f}  {s['drone']:<6}{s['landmark']:<10}{pos:<28}{len(s['seen']):>5}  "
                  f"{s['reason'] or ('counted' if s['banked'] else '-')}")
        need = world.rules.mapping_min_views
        for lid, det in res.landmarks.items():
            mapped = set(det["mapped_patches"])
            lm = world.landmarks[lid]
            un = [f"{pid} ({det['views'][i]} views, rig {det['rig_drones'][i]} drones, "
                  f"spread {det['spread_deg'][i]:.1f})"
                  for i, pid in enumerate(lm.patch_ids) if pid not in mapped]
            rr = world.rules
            print(f"  {lid} unmapped ({len(un)}; a patch needs {need} views from {rr.mapping_min_drones} drones "
                  f"within {rr.mapping_sync_window_s:g} s and {rr.mapping_min_spread_deg:g} deg spread between "
                  f"drones): " + (", ".join(un) if un else "none"))
    if args.shows:
        print(f"\n{'show:slot':<10}{'drone':<6}{'arrived':>22}  {'late':<6}{'held':<6}{'banked':<8}reason")
        for h in res.show_holds:
            print(f"{h['show'] + ':' + str(h['slot']):<10}{h['drone']:<6}{_fmt_t(h['t_arrive']):>22}  "
                  f"{str(h['late']).lower():<6}{str(h['held']).lower():<6}{str(h['banked']).lower():<8}"
                  f"{h['reason'] or 'filled'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m skyline", description="Skyline Survey checker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="fly the plans on one wind day and score them")
    c.add_argument("--plans", default="plans", help="directory with D1.csv ... D8.csv (default: plans)")
    c.add_argument("--data", default="data", help="world data directory (default: data)")
    c.add_argument("--wind", default=None, help="wind day file (default: <data>/wind_forecast.json)")
    c.add_argument("--json", default=None, help="write the full result as JSON to this path")
    c.add_argument("--log", default=None, help="write the flight log (JSON) to this path")
    c.add_argument("--photos", action="store_true",
                   help="list every banked photo with its light factor or sync state, quality Q and points")
    c.add_argument("--landmarks", action="store_true",
                   help="list every scan and each landmark's unmapped patches (views and spread)")
    c.add_argument("--shows", action="store_true", help="list every show row flown and whether it filled its slot")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return _check(args)
    return 2
