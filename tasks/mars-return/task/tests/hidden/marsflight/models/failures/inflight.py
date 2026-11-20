"""Random part failures during the flight, and redundancy against them.

Before this model, failures never happened in a flight: program.hazards turned engine starts, burn seconds,
stage ignitions, parachute deployments and separations into a survival factor on the score, and avionics,
RCS, power, the landing radar, pyros and ISRU never failed at all. Here a part fails at a time drawn from the
world's seed, the flight software sees it in telemetry, and the design's redundancy and the flight software's
response decide what happens next.

Part classes and their laws (rates from data/failures/rates.json, each with its source):

- **Avionics string** (computer, inertial unit, radio): Weibull per unit, Castet and Saleh (2009). A string
  fails at its first unit's failure. Design ``redundancy.avionics``:
  ``single`` (one string; losing it ends the flight, the element can no longer be commanded),
  ``dual`` (a cold spare string, +60 kg; the element goes to safe mode, its engines stop and it ignores
  commands until the flight software sends ``switch_string:<id>``; a second string loss ends the flight),
  ``cross_strapped`` (dual plus cross-strapping, +6 kg more; a unit failure only removes that unit, so the
  element is lost only when the same unit fails on both sides; the switch is still the flight software's).
- **RCS leak** (a thruster valve fails open): Weibull, thruster/fuel. RCS propellant drains until the flight
  software sends ``isolate_rcs:<id>``. ``redundancy.rcs_branches`` 2 keeps half the RCS thrust after the
  isolation; with 1 branch the element has no RCS left.
- **Battery** (a cell or string short): Weibull, battery/cell plus electrical distribution. A string's share of
  the capacity is lost. ``redundancy.battery_strings`` (1 to 4).
- **ISRU plant**: Weibull, payload/instrument, from the sol the plant starts. ``redundancy.isru_units`` 2 keeps
  half the plant.
- **Landing radar**: per landing, at power-on in Mars descent (below 20 km); the rate is fitted on every radar or
  lidar powered landing with a sensor record (data/failures/landing_sensors.json). With no working radar the height
  in telemetry comes from the inertial navigation. ``redundancy.landing_radars`` 2.
- **Separation** (pyro events): per event. The initiation share is covered by ``redundancy.pyro_initiators`` 2;
  the mechanism share is not. A failed separation leaves the element attached.

Draws come from ``ctx.rng(self.name + "/" + element id)``, one stream per element, so adding an element or
changing one element's redundancy never moves another element's draws. A part's failure time is fixed by the
world, so two designs with the same parts see the same failures, and a redundant design survives the worlds
where a single string dies.

A burn-in test (``program_tests.avionics_burn_in``) ages the units before flight, which matters because every
Castet and Saleh shape below 1 means infant mortality: the hazard falls with age.

Telemetry: ``obs["faults"]`` lists each fault the vehicle's fault detection has seen (element, kind, which string
or branch, mission time). Each element in ``obs["elements"]`` gets ``avionics`` (strings, active, healthy,
safe_mode). Hidden: the drawn failure times, which never reach obs.
"""
from __future__ import annotations

import math

from ...core.model import MassItem, Model
from . import rates as R

LEVELS = ("single", "dual", "cross_strapped")
STRINGS = ("A", "B")


def redundancy(el: dict) -> dict:
    """The redundancy an element (design dict) asks for, with defaults (all single)."""
    r = el.get("redundancy") if isinstance(el.get("redundancy"), dict) else {}
    av = r.get("avionics", "single")
    if av not in LEVELS:
        av = "single"

    def n(key, lo, hi):
        try:
            return int(min(max(int(r.get(key, 1)), lo), hi))
        except (TypeError, ValueError):
            return 1
    return {"avionics": av, "rcs_branches": n("rcs_branches", 1, 2), "landing_radars": n("landing_radars", 1, 2),
            "pyro_initiators": n("pyro_initiators", 1, 2), "battery_strings": n("battery_strings", 1, 4),
            "isru_units": n("isru_units", 1, 2)}


def check_design(design: dict) -> list:
    """Errors in the redundancy fields, for the compiler."""
    errs = []
    for el in design.get("elements") or []:
        r = el.get("redundancy") if isinstance(el, dict) else None
        if r is None:
            continue
        if not isinstance(r, dict):
            errs.append(f"{el.get('id')}: redundancy must be an object")
            continue
        for k, v in r.items():
            if k == "avionics":
                if v not in LEVELS:
                    errs.append(f"{el.get('id')}: redundancy.avionics must be one of {LEVELS}")
            elif k in ("rcs_branches", "landing_radars", "pyro_initiators", "isru_units"):
                if v not in (1, 2):
                    errs.append(f"{el.get('id')}: redundancy.{k} must be 1 or 2")
            elif k == "battery_strings":
                if v not in (1, 2, 3, 4):
                    errs.append(f"{el.get('id')}: redundancy.battery_strings must be 1 to 4")
            else:
                errs.append(f"{el.get('id')}: unknown redundancy field {k}")
    return errs


class InFlightFailures(Model):
    name = "failures_inflight"
    area = "failures"
    order = 20

    def __init__(self, inject=None, enabled=True):
        """inject: test-only faults, each {"kind", "element", "t_rel" (s after the flight's first call),
        and "string" or "unit"}; they replace the drawn time of that part."""
        self.inject = list(inject or [])
        self.enabled = enabled

    # ------------------------------------------------------------------ setup
    def design_fields(self):
        return ["elements[].redundancy.avionics", "elements[].redundancy.rcs_branches",
                "elements[].redundancy.landing_radars", "elements[].redundancy.pyro_initiators",
                "elements[].redundancy.battery_strings", "elements[].redundancy.isru_units"]

    def mass_items(self, vehicle, design=None):
        """The mass of the redundancy each element asks for."""
        design = design if design is not None else getattr(self, "design", {})
        by_id = {str(e.get("id")): e for e in design.get("elements") or [] if isinstance(e, dict)}
        out = []
        for el in vehicle.elements:
            r = redundancy(by_id.get(el.id, {}))
            av = el.mass_breakdown.get("avionics", 0.0)
            if av and r["avionics"] != "single":
                out.append(MassItem(el.id, "avionics_string_B", av * R.redundancy("second_string_mass_fraction")))
                if r["avionics"] == "cross_strapped":
                    out.append(MassItem(el.id, "avionics_cross_strap", R.redundancy("cross_strap_mass_kg")))
            if r["rcs_branches"] == 2 and (el.rcs_prop or el.rcs_thrust):
                out.append(MassItem(el.id, "rcs_second_branch", R.redundancy("rcs_branch_mass_kg")))
            if r["landing_radars"] == 2 and el.legs_touchdown_speed:
                out.append(MassItem(el.id, "landing_radar_B", R.redundancy("landing_radar_mass_kg")))
            if r["pyro_initiators"] == 2:
                out.append(MassItem(el.id, "pyro_initiator_B", R.redundancy("pyro_initiator_mass_kg")))
            if r["battery_strings"] > 1 and el.mass_breakdown.get("battery"):
                out.append(MassItem(el.id, "battery_string_switches",
                                    el.mass_breakdown["battery"] * R.redundancy("battery_string_overhead")))
            if r["isru_units"] == 2 and el.mass_breakdown.get("isru_plant"):
                out.append(MassItem(el.id, "isru_unit_B", el.mass_breakdown["isru_plant"]))
        return out

    def configure(self, ctx):
        self.ctx = ctx
        self.design = ctx.design
        rec = getattr(ctx, "record", None) or (ctx.truth or {}).get("failure_record") or {}
        burn_in = rec.get("burn_in_h") or {}
        by_id = {str(e.get("id")): e for e in ctx.design.get("elements") or [] if isinstance(e, dict)}
        av_units = R.part("avionics_string")["units"]
        self.red, self.plan, self.radar_u, self.sep_u, self.isru_dt = {}, [], {}, {}, {}
        self.status = {}
        for el in ctx.vehicle.elements:
            eid = el.id
            r = self.red[eid] = redundancy(by_id.get(eid, {}))
            g = ctx.rng(self.name + "/" + eid)
            # A fixed number of draws per element, in a fixed order, whatever its redundancy.
            u_av = g.uniform(size=(2, len(av_units)))
            u_rcs, u_bat = g.uniform(size=2), g.uniform(size=4)
            u_isru, u_radar, u_sep = g.uniform(size=2), g.uniform(size=2), g.uniform(size=(8, 3))
            age = float(burn_in.get(eid, 0.0)) / 8766.0
            has_av = bool(el.mass_breakdown.get("avionics"))
            if has_av:
                n_str = 1 if r["avionics"] == "single" else 2
                self.status[eid] = {"strings": n_str, "level": r["avionics"], "active": "A", "safe_mode": False,
                                    "down": {s: [] for s in STRINGS[:n_str]}}
                for i, s in enumerate(STRINGS[:n_str]):
                    for j, unit in enumerate(av_units):
                        self.plan.append({"t": R.failure_time_s(unit, u_av[i, j], age), "kind": "avionics_string",
                                          "element": eid, "string": s, "unit": unit})
            if el.rcs_prop > 0:
                self.plan.append({"t": R.failure_time_s("thruster_fuel", u_rcs[0]), "kind": "rcs_leak", "element": eid,
                                  "branch": 1})
            if el.battery_Wh > 0:
                for k in range(r["battery_strings"]):
                    t = min(R.failure_time_s(u, x) for u, x in zip(R.part("battery")["units"], (u_bat[k], u_bat[3 - k])))
                    self.plan.append({"t": t, "kind": "battery_string", "element": eid, "string": k + 1})
            if el.isru_kW > 0:
                self.isru_dt[eid] = [R.failure_time_s("payload_instrument", u_isru[k]) for k in range(r["isru_units"])]
            if el.legs_touchdown_speed:
                self.radar_u[eid] = list(u_radar[:r["landing_radars"]])
            self.sep_u[eid] = u_sep
        t0 = getattr(ctx, "t_launch", 0.0)
        for f in self.plan:
            f["t"] += t0
        self.plan.sort(key=lambda f: f["t"])
        self.seen, self.leaks, self.radar_ok, self.isolated = [], {}, {}, set()
        self.n_sep = {}
        self.t_first = None

    # ------------------------------------------------------------------ helpers for the core
    def _now_faults(self, m):
        if self.t_first is None:
            self.t_first = m.t
            for inj in self.inject:   # test-only faults replace the drawn time of that part
                f = dict(inj)
                f["t"] = m.t + float(inj.get("t_rel", 0.0))
                f.pop("t_rel", None)
                if f["kind"] == "avionics_string":
                    f.setdefault("string", "A")
                    f.setdefault("unit", "control_processor")
                self.plan = [p for p in self.plan if not (p["kind"] == f["kind"] and p["element"] == f["element"]
                                                          and p.get("string") == f.get("string"))]
                self.plan.append(f)
            # faults drawn before a scenario's start are in its past: the flight got here without them
            self.plan = sorted((p for p in self.plan if p["t"] >= m.t), key=lambda p: p["t"])
        due = [p for p in self.plan if p["t"] <= m.t]
        self.plan = [p for p in self.plan if p["t"] > m.t]
        return due

    def _report(self, m, kind, element, **kw):
        item = {"element": element, "kind": kind, "met_s": round(m.t - m.t0, 1)}
        item.update(kw)
        self.seen.append(item)
        m.event("fault", fault=kind, element=element, **kw)

    def before_obs(self, m):
        """Apply the faults that are due. Called by the core before each obs."""
        if not self.enabled:
            return
        present = {s.el.id: s for s in m.present()}
        for f in self._now_faults(m):
            eid = f["element"]
            st = present.get(eid)
            if st is None:
                continue
            k = f["kind"]
            if k == "avionics_string":
                self._string_fault(m, eid, f["string"], f["unit"])
            elif k == "rcs_leak" and eid not in self.isolated and st.rcs > 0:
                self.leaks[eid] = (m.t, R.part("rcs_leak")["leak_kg_per_s_fraction"] * st.el.rcs_prop)
                self._report(m, "rcs_leak", eid, branch=f.get("branch", 1))
            elif k == "battery_string":
                n = self.red[eid]["battery_strings"]
                lost = st.el.battery_Wh / max(n - len([x for x in self.seen if x["kind"] == "battery_string"
                                                         and x["element"] == eid]), 1)
                st.el.battery_Wh = max(0.0, st.el.battery_Wh - lost)
                st.battery_Wh = min(st.battery_Wh, st.el.battery_Wh)
                self._report(m, "battery_string", eid, string=f["string"], capacity_Wh=round(st.el.battery_Wh, 1))
            elif k == "isru_unit":
                n = self.red[eid]["isru_units"]
                gone = len([x for x in self.seen if x["kind"] == "isru_unit" and x["element"] == eid])
                st.el.isru_kW = max(0.0, st.el.isru_kW - st.el.isru_kW / max(n - gone, 1))
                self._report(m, "isru_unit", eid, isru_kW=round(st.el.isru_kW, 2))
        # ISRU: the plant's clock starts on landing
        if m.landed_t is not None and self.isru_dt:
            for eid, dts in self.isru_dt.items():
                for i, dt in enumerate(dts):
                    self.plan.append({"t": m.landed_t + dt, "kind": "isru_unit", "element": eid, "unit_index": i})
            self.plan.sort(key=lambda p: p["t"])
            self.isru_dt = {}

    def _usable(self, s):
        units = R.part("avionics_string")["units"]
        if s["level"] == "cross_strapped":
            return all(any(u not in d for d in s["down"].values()) for u in units)
        return any(not d for d in s["down"].values())

    def _string_fault(self, m, eid, string, unit):
        s = self.status.get(eid)
        if s is None or string not in s["down"] or unit in s["down"][string]:
            return
        s["down"][string].append(unit)
        self._report(m, "avionics_string", eid, string=string)
        if s["level"] == "cross_strapped" or string == s["active"]:
            s["safe_mode"] = True
            st = m.find(eid)
            for name in list(st.lit if st is not None else []):   # the valves close with the string
                del st.lit[name]
                m.event("shutdown", element=eid, engine=name, reason="avionics string lost")
        if not self._usable(s):
            from ...core.common import FlightFailure
            raise FlightFailure(f"avionics of {eid} lost ({unit} failed on string {string}) and no working string is "
                                f"left ({s['level']} avionics): {eid} can no longer be commanded")

    def healthy(self, eid):
        """The strings with no failed unit."""
        s = self.status.get(eid)
        return [] if s is None else [x for x, d in s["down"].items() if not d]

    # ------------------------------------------------------------------ commands
    def command(self, cmd, m):
        """Handle this model's actions and block commands to elements in safe mode. Returns the command the
        vehicle acts on."""
        if not self.enabled:
            return cmd
        acts = []
        for a in cmd.get("actions") or []:
            verb, _, eid = a.partition(":") if isinstance(a, str) else ("", "", "")
            if verb == "switch_string":
                s = self.status.get(eid)
                ok = s is not None and s["strings"] == 2 and s["safe_mode"] and self._usable(s)
                if ok and s["level"] == "cross_strapped":
                    s["active"], s["safe_mode"] = "cross", False   # healthy units from both sides
                elif ok:
                    new = next(x for x in s["down"] if x != s["active"])
                    s["active"], s["safe_mode"] = new, False
                if ok:
                    m.event("string_switched", element=eid, active=s["active"])
                else:
                    m.cmd_errors.append(f"{a}: nothing to switch to")
            elif verb == "isolate_rcs":
                st = m.find(eid)
                if st is None or not st.el.rcs_prop:
                    m.cmd_errors.append(f"{a}: no RCS on {eid}")
                    continue
                self.leaks.pop(eid, None)
                self.isolated.add(eid)
                st.el.rcs_thrust *= 0.5 if self.red[eid]["rcs_branches"] == 2 else 0.0
                m.event("rcs_isolated", element=eid, rcs_thrust_N=st.el.rcs_thrust)
            elif verb == "separate" and m.find(eid) is not None and self._separation_fails(m, eid):
                continue
            else:
                acts.append(a)
        cmd = dict(cmd, actions=acts)
        safe = {eid for eid, s in self.status.items() if s["safe_mode"]}
        if safe:
            eng = {k: v for k, v in (cmd.get("engines") or {}).items() if k not in safe}
            cmd["engines"] = eng
            if (cmd.get("rcs") or {}).get("element") in safe:
                cmd.pop("rcs")
            steer = [s.el.id for s in m.present() if s.el.id in self.status]
            if steer and all(e in safe for e in steer):
                cmd.pop("dir", None)
                cmd.pop("settle", None)
        return cmd

    def _separation_fails(self, m, eid):
        k = self.n_sep.get(eid, 0)
        self.n_sep[eid] = k + 1
        if k >= 8:
            return False
        sep = R.part("separation")
        p, share = float(sep["p_event"]), float(sep["initiation_share"])
        u = self.sep_u[eid][k]
        n_init = self.red[eid]["pyro_initiators"]
        mech = u[0] < p * (1.0 - share)
        init = all(u[1 + i] < p * share for i in range(n_init))
        if mech or init:
            m.event("separation_failed", element=eid, cause="mechanism" if mech else "initiator")
            self._report(m, "separation_failed", eid)
            self.sep_u[eid][k:] = 1.0   # a failed separation stays failed
            return True
        return False

    # ------------------------------------------------------------------ after each arc
    def after_step(self, m, t_before):
        for eid, (t_on, rate) in list(self.leaks.items()):
            st = m.find(eid)
            if st is None:
                self.leaks.pop(eid)
                continue
            st.rcs = max(0.0, st.rcs - rate * max(m.t - max(t_before, t_on), 0.0))

    # ------------------------------------------------------------------ telemetry
    def sense(self, o, m):
        if not self.enabled:
            return o
        o["faults"] = list(self.seen)
        for d in o.get("elements") or []:
            s = self.status.get(d["id"])
            if s is not None:
                d["avionics"] = {"strings": s["strings"], "cross_strapped": s["level"] == "cross_strapped",
                                 "active": s["active"], "healthy": self.healthy(d["id"]), "safe_mode": s["safe_mode"]}
        # landing radar: powered on at the first call below 20 km in Mars descent
        if m.x is not None and m.central == "Mars" and not m.mars_lifted and "height_above_surface" in o:
            h_true = m.altitude(m.x[:3], m.t) - m.ground_alt(m.x[:3], m.t)
            lander = next((s.el.id for s in m.present() if s.el.id in self.radar_u), None)
            if lander and h_true < 20000.0:
                if lander not in self.radar_ok:
                    p = R.landing_radar_p()
                    ok = [u >= p for u in self.radar_u[lander]]
                    self.radar_ok[lander] = any(ok)
                    for i, good in enumerate(ok):
                        if not good:
                            self._report(m, "landing_radar", lander, radar=i + 1)
                    o["faults"] = list(self.seen)
                if not self.radar_ok[lander]:
                    import numpy as np
                    r = np.array(o["r"], float)
                    a_nav = m.altitude(r, m.t)
                    o["alt"] = a_nav
                    o["height_above_surface"] = a_nav - m.ground_alt(r, m.t)
                    o["radar_valid"] = False
                else:
                    o["radar_valid"] = True
        return o

    def summary(self):
        """For the flight record (verifier side only)."""
        return {"faults": list(self.seen), "redundancy": self.red}


MODELS = [InFlightFailures]
