"""Propulsion failure modes the failures atlas found missing (gates F3 to F7), as latent flaws.

Switch: ``MARSFLIGHT_PROPULSION_FAILURES`` is on by default (unset or empty). Set to 0, ``configure`` draws nothing and every
hook returns its input unchanged, so a flight is the same as without this file.

| mode | real case (atlas id in data/failures/propulsion_gaps.json) | in the flight | found by |
|---|---|---|---|
| ``feed_line_leak`` | Falcon 9 Starlink 9-3, 2024 | a pump-fed upper stage leaks oxidizer during its first burn; obs shows the tank pressure low; the restart fails | ``feed_leak_test`` |
| ``regulator_stuck_open`` | Peregrine, 2024 | in cruise a pressurant valve does not reseal; obs shows the tank pressure rising; unless the flight software sends ``isolate_pressurant:<id>`` within the window, the oxidizer tank ruptures and most oxidizer is lost | ``propulsion_checkout`` |
| ``check_valve_migration`` | Mars Observer, 1993 | after a long cruise, the first start of a pressure-fed stage pressurizes the tanks and the line ruptures; the stage is lost. A design with ``pyro_isolation_valves`` on the element survives | ``long_duration_prop_test`` |
| ``check_valve_stuck`` | Akatsuki, 2010 | the first burn of a pressure-fed stage at Mars ends early at a hidden time; the engine cannot fire again for a day | ``valve_qualification`` |
| ``srm_nozzle_erosion`` | Vega C VV22, 2022 | a solid motor's throat insert erodes through partway into the burn; the stage is lost | ``srm_lot_static_fire`` |
| ``srm_case_burst`` | Titan 34D-9, 1986 | a solid motor's case burns through seconds after ignition; the stage is lost | ``srm_lot_static_fire`` |

Rates: prior = FAA first-flight loss rate x (atlas rows of the mode's subcategory / atlas rows with a known cause)
x the mode's split; see ``prior``. A done test multiplies the chance by (1 - coverage), as in flown.p_flaw.
The program office tests are ``TEST_KINDS``, with ``quote`` and ``run_test`` in the shape of program_tests.py.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import random

from ...core.model import Model

SWITCH = "MARSFLIGHT_PROPULSION_FAILURES"
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "failures")
_CACHE = {}
SOLID_CYCLES = ("solid",)


def enabled():
    v = (os.environ.get(SWITCH) or "").strip().lower()
    if v in ("0", "false", "off", "no"):
        return False
    if v in ("", "1", "true", "on", "yes"):
        return True
    raise ValueError(f"{SWITCH}={v!r}: use 1 or 0")


def table():
    if "t" not in _CACHE:
        with open(os.path.join(_DATA, "propulsion_gaps.json")) as f:
            _CACHE["t"] = json.load(f)
    return _CACHE["t"]


def atlas_counts():
    """{subcategory: rows} over atlas rows with a known cause, and the number of those rows."""
    if "c" not in _CACHE:
        cnt, n = {}, 0
        with open(os.path.join(_DATA, "history.csv")) as f:
            for r in csv.DictReader(f):
                if r["category"] == "unknown":
                    continue
                n += 1
                cnt[r["subcategory"]] = cnt.get(r["subcategory"], 0) + 1
        _CACHE["c"] = (cnt, n)
    return _CACHE["c"]


MODES = tuple(table()["modes"])
TEST_KINDS = {k: dict(v, modes=[m for m, d in table()["modes"].items() if d["test"] == k])
              for k, v in table()["tests"].items()}


def prior(mode):
    d = table()["modes"][mode]
    cnt, n = atlas_counts()
    return float(table()["first_flight_loss_rate"]) * cnt.get(d["subcategory"], 0) / n * float(d["split"])


def p_flaw(mode, record):
    d = table()["modes"][mode]
    k = int(((record or {}).get("failure_tests") or {}).get(d["test"], 0))
    return prior(mode) * (1.0 - float(d["coverage"])) ** k


def _stages(vehicle):
    return [e for e in vehicle.elements if e.is_stage and e.engines]


def candidates(mode, vehicle):
    """Element ids a mode can sit in."""
    st = _stages(vehicle)
    solid = [e.id for e in st if any(en.cycle in SOLID_CYCLES for en, _ in e.engines)]
    pf = [e.id for e in st if any(en.cycle == "pressure_fed" for en, _ in e.engines)]
    if mode == "feed_line_leak":
        return [e.id for e in st[1:] if any(en.cycle not in ("pressure_fed",) + SOLID_CYCLES for en, _ in e.engines)]
    if mode in ("regulator_stuck_open", "check_valve_migration", "check_valve_stuck"):
        return pf
    if mode in ("srm_nozzle_erosion", "srm_case_burst"):
        return solid
    return []


def _design_flag(design, eid, key):
    for el in (design or {}).get("elements") or []:
        if isinstance(el, dict) and el.get("id") == eid:
            return bool(el.get(key))
    return False


def _span(rng_u, lo_hi):
    lo, hi = lo_hi
    return lo + rng_u * (hi - lo)


class PropulsionGapFailures(Model):
    name = "failures_propulsion_gaps"
    area = "failures"
    order = 22

    def __init__(self, inject=None, enabled_override=None):
        """inject: test-only, {mode: {details}} forces a mode on. enabled_override: test-only, ignores the switch."""
        self.inject = dict(inject or {})
        self.enabled_override = enabled_override
        self.on, self.seen, self.state = {}, [], {}

    def is_on(self):
        return enabled() if self.enabled_override is None else bool(self.enabled_override)

    def configure(self, ctx):
        self.ctx = ctx
        self.on, self.seen, self.state = {}, [], {}
        if not self.is_on():
            return
        rec = getattr(ctx, "record", None) or (ctx.truth or {}).get("failure_record") or {}
        M = table()["modes"]
        for mode in MODES:
            g = ctx.rng(self.name + "/" + mode)
            u, u2, u3 = g.uniform(size=3)            # fixed number of draws per mode
            cand = candidates(mode, ctx.vehicle)
            if mode not in self.inject and (not cand or u >= p_flaw(mode, rec)):
                continue
            d = dict(self.inject.get(mode) or {})
            if "element" not in d:
                if not cand:
                    continue
                d["element"] = cand[int(u2 * len(cand)) % len(cand)]
            m = M[mode]
            if mode == "feed_line_leak":
                d.setdefault("leak_frac", _span(u3, m["leak_frac_of_ox"]))
            elif mode == "regulator_stuck_open":
                d.setdefault("t_rel", u3 * float(m["cruise_days"]) * 86400.0)
                d.setdefault("window_s", _span(u2, m["window_s"]))
                d.setdefault("ox_left", _span(u3, m["ox_left_frac"]))
            elif mode == "check_valve_migration":
                d.setdefault("min_cruise_s", float(m["min_cruise_days"]) * 86400.0)
                d.setdefault("isolated", _design_flag(ctx.design, d["element"], "pyro_isolation_valves"))
            elif mode == "check_valve_stuck":
                d.setdefault("cut_s", _span(u3, m["cut_s"]))
                d.setdefault("lockout_s", float(m["lockout_s"]))
            elif mode == "srm_nozzle_erosion":
                d.setdefault("fail_burn_s", 100.0 * _span(u3, m["burn_frac"]))
            elif mode == "srm_case_burst":
                d.setdefault("fail_burn_s", _span(u3, m["t_s"]))
            self.on[mode] = d

    # ------------------------------------------------------------------ helpers
    def _report(self, m, kind, element=None, **kw):
        item = {"element": element, "kind": kind, "met_s": round(m.t - m.t0, 1)}
        item.update(kw)
        self.seen.append(item)
        m.event("fault", fault=kind, element=element, **kw)

    @staticmethod
    def _burn_s(st):
        return float(sum((st.burn_s or {}).values()))

    @staticmethod
    def _fail(msg):
        from ...core.common import FlightFailure
        raise FlightFailure(msg)

    # ------------------------------------------------------------------ core hooks (as flown.py)
    def before_obs(self, m):
        if not self.on:
            return
        on, st = self.on, self.state
        r = on.get("regulator_stuck_open")
        if r and m.central == "Sun" and "reg_t" not in st and m.find(r["element"]) is not None:
            st.setdefault("cruise_t0", m.t)
            if m.t - st["cruise_t0"] >= r["t_rel"]:
                st["reg_t"] = m.t
                self._report(m, "tank_pressure_rising", r["element"])
        if r and "reg_t" in st and not st.get("reg_done"):
            el = m.find(r["element"])
            if el is not None and m.t - st["reg_t"] >= r["window_s"]:
                st["reg_done"] = True
                el.ox *= r["ox_left"]
                self._report(m, "oxidizer_tank_ruptured", r["element"])
        c = on.get("check_valve_migration")
        if c and m.central == "Sun":
            st.setdefault("mig_cruise_t0", m.t)

    def command(self, cmd, m):
        if not self.on:
            return cmd
        on, st = self.on, self.state
        acts = []
        for a in cmd.get("actions") or []:
            verb, _, eid = a.partition(":") if isinstance(a, str) else ("", "", "")
            if verb == "isolate_pressurant":
                r = on.get("regulator_stuck_open")
                if r and r["element"] == eid and "reg_t" in st and not st.get("reg_done"):
                    st["reg_done"] = True
                    m.event("pressurant_isolated", element=eid)
                continue
            acts.append(a)
        cmd = dict(cmd, actions=acts)
        eng = dict(cmd.get("engines") or {})
        blocked = set()
        f = on.get("feed_line_leak")
        if f and st.get("leak_burn_done"):
            el = m.find(f["element"])
            if el is not None and not el.lit and eng.get(f["element"]):
                blocked.add(f["element"])
                if not st.get("restart_failed"):
                    st["restart_failed"] = True
                    self._report(m, "engine_restart_failed", f["element"])
        k = on.get("check_valve_stuck")
        if k and st.get("cv_cut_t") is not None and m.t - st["cv_cut_t"] < k["lockout_s"] and eng.get(k["element"]):
            blocked.add(k["element"])
        if blocked:
            cmd["engines"] = {e: v for e, v in eng.items() if e not in blocked}
        return cmd

    def after_step(self, m, t_before, sensed=None):
        if not self.on:
            return
        on, st = self.on, self.state
        dt = max(float(m.t - t_before), 0.0)
        f = on.get("feed_line_leak")
        if f:
            el = m.find(f["element"])
            if el is not None and el.lit:
                st.setdefault("leak_ox0", el.ox)
                st["leak_on"] = True
                el.ox = max(el.ox - f["leak_frac"] * st["leak_ox0"] * dt / 300.0, 0.0)
                if not st.get("leak_seen"):
                    st["leak_seen"] = True
                    self._report(m, "ox_tank_pressure_low", f["element"])
            elif el is not None and st.get("leak_on"):
                st["leak_burn_done"] = True
        c = on.get("check_valve_migration")
        if c and not c["isolated"] and not st.get("mig_done"):
            el = m.find(c["element"])
            if el is not None and el.lit:
                st["mig_done"] = True
                cruise = m.t - st.get("mig_cruise_t0", m.t)
                if cruise >= c["min_cruise_s"]:
                    self._report(m, "pressurization_line_ruptured", c["element"])
                    self._fail(f"oxidizer vapor that crept through the check valves of {c['element']} during cruise "
                               f"reacted in the pressurization line when the tanks were pressurized; the line "
                               f"ruptured and {c['element']} was lost")
        k = on.get("check_valve_stuck")
        if k and m.central == "Mars" and st.get("cv_cut_t") is None:
            el = m.find(k["element"])
            if el is not None and el.lit:
                st.setdefault("cv_b0", self._burn_s(el))
                if self._burn_s(el) - st["cv_b0"] >= k["cut_s"]:
                    st["cv_cut_t"] = m.t
                    el.lit = {}
                    self._report(m, "engine_shutdown_early", k["element"], burn_s=round(k["cut_s"], 1))
        for mode, what in (("srm_case_burst", "the motor case burned through and burst"),
                           ("srm_nozzle_erosion", "the nozzle throat insert eroded through and the chamber "
                                                  "pressure collapsed")):
            s = on.get(mode)
            if s:
                el = m.find(s["element"])
                if el is not None and self._burn_s(el) >= s["fail_burn_s"]:
                    self._report(m, mode, s["element"])
                    self._fail(f"{s['element']}: {what} {self._burn_s(el):.0f} s into its burn; the stage was lost")

    def sense(self, o, m):
        if not self.on:
            return o
        if self.seen:
            o["faults"] = list(o.get("faults") or []) + [f for f in self.seen if f not in (o.get("faults") or [])]
        return o

    def summary(self):
        return {"modes": {k: dict(v) for k, v in self.on.items()}, "faults": list(self.seen)}


# ---------------------------------------------------------------------- program office tests
def _u(secret, key):
    h = hmac.new(str(secret).encode(), key.encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(h[:8], "little"))


def quote(kind, spec, params):
    if kind not in TEST_KINDS:
        raise ValueError(f"unknown test kind {kind!r}")
    k = TEST_KINDS[kind]
    key = f"failtest:{kind}:{(spec or {}).get('element', 'vehicle')}"
    return {"kind": kind, "key": key, "facility": k["facility"], "cost_usd": float(k["cost_usd"]),
            "minutes": float(k["minutes"])}


def run_test(kind, spec, params, test_id, ledger, secret, record=None):
    """One test. ``record`` is program_tests.failure_record(ledger) (tests done before this one).
    For each mode the test covers: a flaw was there with chance p_flaw and the test caught it with its coverage."""
    q = quote(kind, spec, params)
    rng = _u(secret, "failtest:" + str(test_id))
    if record is None:
        from .program_tests import failure_record
        record = failure_record([t for t in (ledger or []) if t.get("kind") not in TEST_KINDS])
        for t in ledger or []:
            if t.get("status") == "done" and t.get("kind") in TEST_KINDS:
                record["failure_tests"][t["kind"]] = record["failure_tests"].get(t["kind"], 0) + 1
    out = {"kind": kind, "key": q["key"], "measured": {}, "found": []}
    rem = {}
    for mode in TEST_KINDS[kind]["modes"]:
        p = p_flaw(mode, record)
        cov = float(table()["modes"][mode]["coverage"])
        if rng.random() < p * cov:
            out["found"].append(mode)
        rem[mode] = round(p * (1 - cov), 6)
    out["measured"] = {"flaw_found": bool(out["found"]), "remaining_chance": rem}
    lesson = TEST_KINDS[kind]["lesson"]
    out["summary"] = (f"Found and fixed: {lesson}." if out["found"] else "No flaw found.") + \
        f" The chance that one is still there is now {100 * sum(rem.values()):.3f}%."
    return out


MODELS = [PropulsionGapFailures]
