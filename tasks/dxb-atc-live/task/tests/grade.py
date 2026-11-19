"""Grade the live day from the hub's record. Reads only /var/lib/dxb-atc/score.json, which must carry the live server's
signature (a record without it grades 0). No team file is read, imported or run, and MULTI_AGENT_HARNESS_GRADE_APP is
ignored on purpose: the grade is of the day, not a commit.

Service S = mean credit over the flights scheduled from the start of the day up to 30 minutes before the grading
time (a flight scheduled later is still in progress, so it is left to the next grading):
  a flight completed (an arrival landed at OMDB, a departure airborne) d minutes after its scheduled time earns
  1 if d <= 15, else max(0, 1 - (d - 15) / 240), so a flight 4 h 15 min late or more earns 0;
  a flight not yet completed at the grading time earns 0 there (it earns its credit at a later grading once it is
  completed);
  a diverted arrival earns the credit of its return flight once that has landed, else 0;
  a cancelled or crashed flight earns 0.
Safety M = 0.5 for each accident (A), times, per 100 flights counted, 0.9 for each near collision (A-), 0.97 for
  each serious incident (B) and 0.995 for each loss of separation or spacing below the minimum (C). "Per 100 flights"
  means those factors are raised to the power 100 / max(N, 100), where N is the number of flights counted in S, so a
  grade over fewer than 100 flights counts every event in full.
Grade = day score D = S * M, from 0 to 1. Nobody else works the day: a sector no seat has open has no controller.

The reference solution's day is checked, not scored. The live day is open-ended: no controller can make every flight
on time with no safety event, so the reference controller's day score is never 1. Its record (marked "reference" by
the server, which only the root-only entrypoint starts with the built-in controller, and signed like every record)
grades 1 when the task works end to end: the day ran for at least an hour of airport time with no simulator error (no failed step or stage),
flights landed and took off, and nobody crashed. Otherwise it grades 0. Its day score is still reported in details.json.
A team's record never carries the mark, so a team is always graded by D.
details.json also gives the plain counts: on time, delays, diversions, cancellations and every safety event.
"""
import hashlib
import hmac
import json
import os

STATE = os.environ.get("ATC_STATE_DIR", "/var/lib/dxb-atc")
LAG_S = 1800
START_SLACK_S = 900  # the day starts at the team's first call, at most 10 minutes into the run, plus clock lag
SEVERITY = {"A": 0.5, "A-": 0.9, "B": 0.97, "C": 0.995}
# The live server signs every record it writes with this key (environment/hub/server/server.py). A record without a
# valid signature was not written by the simulator, so it grades 0.
SIGN_KEY = bytes.fromhex("15272bdd733541611393929c523079765c1f6ad3c06d30f641831088c39ddd9a")


def signed(rec):
    sig = rec.get("sig")
    body = json.dumps({k: v for k, v in rec.items() if k != "sig"}, sort_keys=True, separators=(",", ":"))
    return isinstance(sig, str) and hmac.compare_digest(sig, hmac.new(SIGN_KEY, body.encode(), hashlib.sha256).hexdigest())


def credit(delay_min):
    if delay_min <= 15.0:
        return 1.0
    return max(0.0, 1.0 - (delay_min - 15.0) / 240.0)


def _as_of(f, now):
    """The flight's outcome as it stood at `now`, and its projected completion if it was still pending. A record saved
    after `now` knows later outcomes: a flight that landed or took off after `now` was pending then, and its real
    completion time is the best projection of it."""
    t = f.get("out_t")
    if f["outcome"] == "crashed" and t is not None and t > now and f.get("t_done") is not None and f["t_done"] <= now:
        # Landed or took off before `now` and crashed later (hit on its landing roll, or a mid-air collision after
        # take-off): at `now` it was done.
        return ("landed" if f["kind"] == "ARR" else "departed"), f["t_done"], None
    if f["outcome"] is not None and t is not None and t > now:
        return None, None, (f["t_done"] if f["outcome"] in ("landed", "departed") else None)
    return f["outcome"], f["t_done"], f.get("proj_t")


def service(rec, now, lag_s=LAG_S):
    flights = {f["id"]: f for f in rec["flights"]}
    total, n = 0.0, 0
    rows = {}
    for f in flights.values():
        if f.get("return_of"):
            continue
        if f["sched"] < 0 or f["sched"] > now - lag_s:
            continue
        n += 1
        out, done, proj = _as_of(f, now)
        c, row = 0.0, out or "pending"
        if out in ("landed", "departed") and done is not None and done <= now:
            c = credit((done - f["sched"]) / 60.0)
        elif out == "diverted":
            # The return flight, or the return of a return flight that diverted again, as of the grading time.
            r = flights.get(f["id"] + "R")
            r_out, r_done, r_proj = _as_of(r, now) if r is not None else (None, None, None)
            while r is not None and r_out == "diverted" and flights.get(r["id"] + "R") is not None:
                r = flights[r["id"] + "R"]
                r_out, r_done, r_proj = _as_of(r, now)
            if r is not None and r_out == "landed" and r_done is not None and r_done <= now:
                c, row = credit((r_done - f["sched"]) / 60.0), "diverted_returned"
            elif r is not None and r_out is None:
                row = "diverted_returning"
        total += c
        rows[row] = rows.get(row, 0) + 1
    return (total / n if n else 0.0), n, rows


def safety(rec, now, n):
    """Accidents count in full; every other event counts per 100 of the n flights counted in the service score."""
    accidents, rest, counts = 1.0, 1.0, {}
    for s in rec.get("safety", []):
        if s["t"] <= now and not (s.get("replaced_t") is not None and s["replaced_t"] <= now):
            if s["severity"] == "A":
                accidents *= SEVERITY["A"]
            else:
                rest *= SEVERITY.get(s["severity"], 1.0)
            counts[s["severity"]] = counts.get(s["severity"], 0) + 1
    return accidents * rest ** (100.0 / max(n, 100)), counts


def day_score(rec, now):
    s, n, rows = service(rec, now)
    m, counts = safety(rec, now, n)
    return {"service": s, "safety": m, "day_score": s * m, "flights_counted": n, "rows": rows, "safety_counts": counts}


def counts(rec, now):
    """Plain numbers for the people reading the result. Not part of the grade."""
    due = [f for f in rec["flights"] if not f.get("return_of") and 0 <= f["sched"] <= now - LAG_S]
    done = [f for f in due if f["outcome"] in ("landed", "departed") and f["t_done"] is not None and f["t_done"] <= now]
    delays = sorted((f["t_done"] - f["sched"]) / 60.0 for f in done)
    kinds = {}
    for e in rec.get("safety", []):
        if e["t"] <= now and not (e.get("replaced_t") is not None and e["replaced_t"] <= now):
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    out = {}
    for o in ("diverted", "cancelled", "crashed"):
        out[o] = sum(1 for f in due if f["outcome"] == o and (f.get("out_t") is None or f["out_t"] <= now))
    return {"flights_due": len(due), "completed": len(done), "on_time": sum(1 for d in delays if d <= 15.0),
            "on_time_share": round(sum(1 for d in delays if d <= 15.0) / len(due), 4) if due else None,
            "mean_delay_min": round(sum(max(0.0, d) for d in delays) / len(delays), 1) if delays else None,
            "late_over_60_min": sum(1 for d in delays if d > 60.0), **out, "safety_by_kind": kinds}


def mark_seconds(mark):
    """Run time of a mark label: s090 -> 90, m30 -> 1800, h04 -> 14400. None for any other label (e.g., end)."""
    import re
    m = re.fullmatch(r"([smh])(\d+(?:\.\d+)?)", mark or "")
    return None if m is None else float(m.group(2)) * {"s": 1, "m": 60, "h": 3600}[m.group(1)]


def grading_time(rec, mark=None, hours=None):
    """The airport time a grading covers. At a mark of a team run it is fixed by the mark alone: the mark's run time,
    less the start slack, at the day's speed. Every team is graded over the same stretch of the day whatever its
    clock did, so stopping or stalling the day never changes what is graded. The final grading of a team run (no
    mark) covers the planned run, like the "end" mark. Only the reference solution's day (played with the built-in
    controller, on a backend with no harness) is graded as far as it went, at a mark or without one."""
    now = rec["sim_t"]
    secs = mark_seconds(mark or "end")
    if secs is None and hours:
        secs = float(hours) * 3600
    if secs is None:
        return now
    h = min((secs - START_SLACK_S) * float(rec.get("speed") or 3.0), float(rec.get("duration_s") or now))
    return min(h, now) if rec.get("reference") else h


def grade(rec, mark=None, hours=None):
    now = grading_time(rec, mark, hours)
    team = day_score(rec, now)
    # A day whose clock stopped short of the grading time (a run ended early, a stalled clock) is not a played day:
    # it scores 0, so stopping the day at a good moment never pays.
    short = rec["sim_t"] < now
    return {"grade": 0.0 if short else max(0.0, min(1.0, team["day_score"])), "stopped_short": short,
            "team": team, "counts": counts(rec, now), "sim_t": now,
            "phase": rec.get("phase"), "start_utc": rec.get("start_utc"),
            "sectors_staffed_at_end": {k: v for k, v in rec.get("sectors", {}).items() if v}, "calls": rec.get("calls"),
            "error": rec.get("error"), "simulator_errors": simulator_errors(rec)}


def simulator_errors(rec):
    """Failed simulator steps, and failed stages inside a step (separation, pilots, runway...), over the day.
    A failed file write is not one: the next write repeats it."""
    stats = rec.get("stats") or {}
    return {"step": int(rec.get("step_errors") or 0),
            "stage": max(int(stats.get("stage_errors") or 0), len(rec.get("stage_errors") or []))}


REF_MIN_DAY_S = 3600


def reference_check(rec, details):
    """The reference solution's day: does the task work end to end? See the module docstring."""
    c = details.get("counts") or {}
    landed = sum(1 for f in rec["flights"] if f["kind"] == "ARR" and f["outcome"] == "landed")
    departed = sum(1 for f in rec["flights"] if f["kind"] == "DEP" and f["outcome"] == "departed")
    checks = {"day_ran_an_hour": rec["sim_t"] >= REF_MIN_DAY_S,
              "no_simulator_error": rec.get("error") is None and not any(simulator_errors(rec).values()),
              "flights_landed": landed > 0, "flights_departed": departed > 0,
              "no_accident": not c.get("crashed") and not any(e.get("severity") == "A" for e in rec.get("safety", []))}
    details["reference_day_score"] = details["grade"]
    details["reference_checks"] = checks
    details["grade"] = 1.0 if all(checks.values()) else 0.0
    return details


def main():
    out = os.environ.get("MULTI_AGENT_HARNESS_GRADE_OUT") or "/logs/verifier"
    os.makedirs(out, exist_ok=True)
    p = os.path.join(STATE, "score.json")
    if not os.path.exists(p):
        details = {"grade": 0.0, "note": "no day was played"}
    else:
        run = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.json")
        hours = json.load(open(run)).get("hours") if os.path.exists(run) else None
        with open(p) as f:
            rec = json.load(f)
        if not signed(rec):
            details = {"grade": 0.0, "note": "the record is not signed by the live day's simulator"}
        else:
            details = grade(rec, os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK"), hours)
            if rec.get("reference"):
                details = reference_check(rec, details)
    details["mark"] = os.environ.get("MULTI_AGENT_HARNESS_GRADE_MARK")
    with open(os.path.join(out, "details.json"), "w") as f:
        json.dump(details, f, indent=1)
    with open(os.path.join(out, "reward.txt"), "w") as f:
        f.write(f"{details['grade']:.6f}\n")
    t = details.get("team", {})
    print(f"grade {details['grade']:.4f} service {t.get('service')} safety {t.get('safety')} sim_t {details.get('sim_t')} "
          f"counts {details.get('counts')} api_calls {sum((details.get('calls') or {}).values())}")


if __name__ == "__main__":
    main()
