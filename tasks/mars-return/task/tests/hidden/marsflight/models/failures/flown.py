"""Flown failures rebuilt as failure modes, on the in-flight mechanism of Each mode is a real loss with a published cause (data/failures/catalog.json has the sources). Two kinds:

- **Latent flaws**: a build, integration or software error that a new vehicle carries with a prior chance
  (data/failures/rates.json ``latent_flaw_prior``: the FAA first-flight loss rate times the cause's share of
  launch failures in Chang 1980-1999). A program office test finds the flaw with the test's coverage; each test
  done multiplies the chance by (1 - coverage). Whether this vehicle has the flaw is a hidden draw from
  ``ctx.rng(self.name + "/" + mode)``.
- **Characteristics**: how real hardware behaves, which the flight software has to handle (the Mars Polar Lander
  touchdown sensor transient). Always on for a design that has the part; the details are hidden draws.

Modes and what each does in the flight:

| mode | real case | in the flight | countered by |
|---|---|---|---|
| ``reversed_polarity`` | Proton-M 2013, Vega VV17 2020 | the stage's steering goes the wrong way while it burns | ``phasing_test`` |
| ``heritage_overflow`` | Ariane 5 501, 1996 | above a horizontal speed the inertial software overflows on both units; nav freezes and the nozzles go hard over | ``trajectory_requalification`` |
| ``weak_strut`` | Falcon 9 CRS-7, 2015 | a pressurant bottle strut in an upper stage breaks at a fraction of its rating as the axial g rises; the stage is lost | ``part_lot_screening`` |
| ``inverted_gswitch`` | Genesis, 2004 | the capsule sequencer never senses entry, so its parachutes never fire | ``centrifuge_test`` |
| ``imu_saturation_hold`` | Schiaparelli, 2016 | the IMU saturates about 1 s at parachute inflation; the attitude error makes the radar height read negative | ``edl_hil_test`` |
| ``units_mismatch`` | Mars Climate Orbiter, 1999 | small-force impulses in lbf s read as N s; the navigated position near Mars is biased high | ``nav_end_to_end`` |
| ``see_reset`` | Phobos-Grunt, 2011 | heavy-ion upsets in non-rad-hard memory reboot both strings in cruise; safe mode until ``restart:<id>`` | ``radiation_test`` |
| ``touchdown_transient`` | Mars Polar Lander, 1999 | leg sensors give a spurious touchdown signal when the legs deploy at 1,500 m | ``touchdown_sensor_test`` reveals it; the flight software must ignore it |

Recovery commands: ``restart:<id>`` after a ``see_reset``. Telemetry: ``obs["faults"]`` (shared with the in-flight fault model),
``obs["touchdown_sensors"]`` on a Mars lander after its legs deploy, ``obs["imu_saturated"]`` while saturated,
``obs["nav_valid"]`` false after an overflow.
"""
from __future__ import annotations

import math

import numpy as np

from ...core.model import Model
from . import rates as R

G0 = 9.80665
# mode -> (prior class in rates.json, program office test that finds it, its coverage per test)
LATENT = {
    "reversed_polarity": ("integration", "phasing_test", 0.95),
    "heritage_overflow": ("software", "trajectory_requalification", 0.9),
    "weak_strut": ("structural_part", "part_lot_screening", 0.95),
    "inverted_gswitch": ("integration", "centrifuge_test", 0.95),
    "imu_saturation_hold": ("software", "edl_hil_test", 0.8),
    "units_mismatch": ("software", "nav_end_to_end", 0.9),
    "see_reset": ("electrical_parts", "radiation_test", 0.9),
}
CHARACTERISTIC = {"touchdown_transient": "touchdown_sensor_test"}
# Numbers of the rebuilt cases (catalog.json has the sources)
CRS7_RATING_G = 16.0          # a strut rated 10,000 lbf for about 2,000 lbf at 3.2 g: 5 x 3.2 g (SpaceX, Jul 20 2015)
CRS7_STRENGTH = (0.15, 0.4)   # share of its rating a bad strut really holds (CRS-7: about 0.2)
ARIANE501_HARDOVER_DEG = 20.0 # the angle of attack the full nozzle deflection gave (Lions board: above 20 deg)
ARIANE501_VH = (300.0, 1200.0)  # m/s Earth-relative horizontal speed at which the heritage range ends (game range)
SCHIAPARELLI_ERR_DEG = 165.0  # attitude error after the saturation (ESA inquiry, 2017)
SCHIAPARELLI_SAT_S = 1.0      # saturation lasted about 1 s (ESA, Nov 23 2016)
MCO_BIAS_KM = (30.0, 120.0)   # navigated altitude bias near Mars (game range around MCO's error)
MPL_LEG_DEPLOY_M = 1500.0     # MPL deployed its legs at about 1,500 m (JPL D-18709)


def p_flaw(mode, record):
    """Chance this design still has a latent flaw of this mode, after the program tests in ``record``."""
    cls, test, cov = LATENT[mode]
    n = int(((record or {}).get("failure_tests") or {}).get(test, 0))
    return R.prior(cls) * (1.0 - cov) ** n


def applies(mode, design, vehicle):
    """Whether the design has the parts a mode needs."""
    els = vehicle.elements
    stages = [e for e in els if e.is_stage and e.engines]
    if mode == "reversed_polarity":
        return any(any(en.gimbal_deg > 0 for en, _ in e.engines) for e in stages)
    if mode == "heritage_overflow":
        return bool(stages)
    if mode == "weak_strut":
        return any(any(en.cycle != "pressure_fed" for en, _ in e.engines) for e in stages[1:])
    if mode == "inverted_gswitch":
        return any(e.kind == "capsule" and e.landing == "parachute" for e in els)
    if mode == "imu_saturation_hold":
        return any(e.kind == "parachute" for e in els) and any(e.legs_touchdown_speed for e in els)
    if mode == "units_mismatch":
        return True
    if mode == "see_reset":
        return any(e.mass_breakdown.get("avionics") for e in els)
    if mode == "touchdown_transient":
        return any(e.legs_touchdown_speed for e in els)
    return False


class FlownFailures(Model):
    name = "failures_flown"
    area = "failures"
    order = 21

    def __init__(self, inject=None, enabled=True):
        """inject: test-only, {mode: {details}} forces a mode on with the given details."""
        self.inject = dict(inject or {})
        self.enabled = enabled

    def configure(self, ctx):
        self.ctx = ctx
        rec = getattr(ctx, "record", None) or (ctx.truth or {}).get("failure_record") or {}
        v = ctx.vehicle
        self.on = {}
        for mode in list(LATENT) + list(CHARACTERISTIC):
            g = ctx.rng(self.name + "/" + mode)
            u, u2, u3 = g.uniform(size=3)        # fixed number of draws per mode
            if not applies(mode, ctx.design, v) and mode not in self.inject:
                continue
            present = mode in CHARACTERISTIC or u < p_flaw(mode, rec)
            if mode in self.inject:
                present = True
            if not present:
                continue
            d = dict(self.inject.get(mode) or {})
            stages = [e for e in v.elements if e.is_stage and e.engines]
            if mode == "reversed_polarity":
                steer = [e.id for e in stages if any(en.gimbal_deg > 0 for en, _ in e.engines)]
                if "element" not in d:
                    d["element"] = steer[int(u2 * len(steer)) % len(steer)]
            elif mode == "heritage_overflow":
                d.setdefault("vh_limit", ARIANE501_VH[0] + u2 * (ARIANE501_VH[1] - ARIANE501_VH[0]))
            elif mode == "weak_strut":
                ups = [e.id for e in stages[1:] if any(en.cycle != "pressure_fed" for en, _ in e.engines)]
                if "element" not in d:
                    d["element"] = ups[int(u2 * len(ups)) % len(ups)]
                d.setdefault("strength_g", CRS7_RATING_G * (CRS7_STRENGTH[0] + u3 * (CRS7_STRENGTH[1] - CRS7_STRENGTH[0])))
            elif mode == "inverted_gswitch":
                if "element" not in d:
                    d["element"] = next(e.id for e in v.elements if e.kind == "capsule" and e.landing == "parachute")
            elif mode == "units_mismatch":
                d.setdefault("bias_m", 1e3 * (MCO_BIAS_KM[0] + u2 * (MCO_BIAS_KM[1] - MCO_BIAS_KM[0])))
            elif mode == "see_reset":
                av = [e.id for e in v.elements if e.mass_breakdown.get("avionics")]
                if "element" not in d:
                    d["element"] = av[int(u2 * len(av)) % len(av)]
                d.setdefault("cruise_frac", u3)       # when in the first cruise
            elif mode == "touchdown_transient":
                legs = [bool(x) for x in (g.uniform(size=3) < 0.5)]
                if not any(legs):
                    legs[int(u2 * 3) % 3] = True
                d.setdefault("legs", legs)
            self.on[mode] = d
        self.seen, self.state = [], {}

    # ------------------------------------------------------------------ helpers
    def _report(self, m, kind, element=None, **kw):
        item = {"element": element, "kind": kind, "met_s": round(m.t - m.t0, 1)}
        item.update(kw)
        self.seen.append(item)
        m.event("fault", fault=kind, element=element, **kw)

    def _lit(self, m, eid):
        st = m.find(eid)
        return st is not None and bool(st.lit)

    def _vh(self, m):
        r, v = m.x[:3], m.x[3:]
        vr = v - m.omega_cross(r)
        up = r / np.linalg.norm(r)
        return float(np.linalg.norm(vr - np.dot(vr, up) * up))

    # ------------------------------------------------------------------ core hooks (see standin.py)
    def before_obs(self, m):
        if not self.enabled or not self.on:
            return
        on, st = self.on, self.state
        if "heritage_overflow" in on and "overflow_t" not in st and m.x is not None and m.central == "Earth" \
                and "earth_orbit" not in m.milestones and any(s.lit for s in m.present()):
            if self._vh(m) > on["heritage_overflow"]["vh_limit"]:
                st["overflow_t"] = m.t
                st["frozen"] = (list(m.x[:3]), list(m.x[3:]))
                self._report(m, "inertial_reference_overflow")
        if "see_reset" in on and "see_t" not in st and m.central == "Sun":
            st.setdefault("cruise_t0", m.t)
            if m.t - st["cruise_t0"] >= on["see_reset"]["cruise_frac"] * 150 * 86400.0 or on["see_reset"].get("now"):
                st["see_t"] = m.t
                st["safe"] = on["see_reset"]["element"]
                self._report(m, "avionics_reset", st["safe"])
        if "see_reset" in on and on["see_reset"].get("t_rel") is not None and "see_t" not in st:
            st.setdefault("t_first", m.t)
            if m.t - st["t_first"] >= on["see_reset"]["t_rel"]:
                st["see_t"] = m.t
                st["safe"] = on["see_reset"]["element"]
                self._report(m, "avionics_reset", st["safe"])
        if "imu_saturation_hold" in on and "sat_t" not in st:
            ev = next((e for e in m.events if e["type"] == "parachute_opened" and m.central == "Mars"), None)
            if ev is not None:
                st["sat_t"] = ev["t"]
                self._report(m, "imu_saturated")

    def command(self, cmd, m):
        if not self.enabled or not self.on:
            return cmd
        on, st = self.on, self.state
        acts = []
        for a in cmd.get("actions") or []:
            verb, _, eid = a.partition(":") if isinstance(a, str) else ("", "", "")
            if verb == "restart":
                if st.get("safe") == eid:
                    st["safe"] = None
                    m.event("avionics_restarted", element=eid)
                else:
                    m.cmd_errors.append(f"{a}: {eid} is not waiting for a restart")
                continue
            if verb == "deploy" and "inverted_gswitch" in on:
                el = m.find(eid)
                cap = on["inverted_gswitch"]["element"]
                if el is not None and el.el.kind == "parachute" and getattr(el.el, "carrier", None) in (cap, None) \
                        and m.find(cap) is not None:
                    if not st.get("gswitch_reported"):
                        st["gswitch_reported"] = True
                        m.event("parachute_not_fired", element=eid, reason="sequencer never armed")
                    continue
            acts.append(a)
        cmd = dict(cmd, actions=acts)
        if st.get("safe"):
            cmd["engines"] = {k: v for k, v in (cmd.get("engines") or {}).items() if k != st["safe"]}
            if (cmd.get("rcs") or {}).get("element") == st["safe"]:
                cmd.pop("rcs")
        want = cmd.get("dir")
        if want is not None and m.dir is not None and m.x is not None:
            w = np.array(want, float)
            cur = np.array(m.dir, float)
            if "reversed_polarity" in on and self._lit(m, on["reversed_polarity"]["element"]):
                # the rate sensors (or actuator cables) are reversed: the controller turns away from the command
                flip = 2.0 * cur - w / max(np.linalg.norm(w), 1e-9)
                cmd["dir"] = (flip / max(np.linalg.norm(flip), 1e-9)).tolist()
                if not st.get("polarity_seen"):
                    st["polarity_seen"] = True
            if "overflow_t" in st and any(s.lit for s in m.present()):
                # Ariane 501: the OBC reads the diagnostic word as attitude data and drives the nozzles hard over:
                # each call the vehicle turns a further ARIANE501_HARDOVER_DEG away from its flight path
                vr = m.x[3:] - m.omega_cross(m.x[:3])
                vh = vr / max(np.linalg.norm(vr), 1e-9)
                perp = cur - np.dot(cur, vh) * vh
                if np.linalg.norm(perp) < 1e-6:
                    perp = np.cross(vh, m.x[:3])
                perp /= max(np.linalg.norm(perp), 1e-9)
                a = math.radians(ARIANE501_HARDOVER_DEG)
                ang = math.acos(float(np.clip(np.dot(cur, vh), -1.0, 1.0))) + a
                cmd["dir"] = (math.cos(ang) * vh + math.sin(ang) * perp).tolist()
        return cmd

    def after_step(self, m, t_before, sensed=None):
        on, st = self.on, self.state
        if "weak_strut" in on and sensed is not None:
            eid = on["weak_strut"]["element"]
            g = float(np.linalg.norm(sensed)) / G0
            if m.find(eid) is not None and g >= on["weak_strut"]["strength_g"]:
                from ...core.common import FlightFailure
                self._report(m, "strut_failure", eid, g=round(g, 2))
                raise FlightFailure(f"a strut holding a helium bottle in {eid}'s LOX tank broke at {g:.1f} g: the bottle "
                                    f"broke free, the tank overpressured and {eid} was lost")

    def sense(self, o, m):
        if not self.enabled or not self.on:
            return o
        on, st = self.on, self.state
        if self.seen:
            o["faults"] = list(o.get("faults") or []) + [f for f in self.seen if f not in (o.get("faults") or [])]
        if "overflow_t" in st:
            o["r"], o["v"] = st["frozen"]
            o["nav_valid"] = False
        if st.get("safe"):
            for d in o.get("elements") or []:
                if d["id"] == st["safe"]:
                    d.setdefault("avionics", {})["safe_mode"] = True
                    d["avionics"]["rebooted"] = True
        if "units_mismatch" in on and m.central == "Mars" and not m.mars_lifted and m.x is not None \
                and m.mode == "flight" and "mars_entry" not in m.milestones and o.get("r") is not None:
            r = np.array(o["r"], float)
            o["r"] = (r + on["units_mismatch"]["bias_m"] * r / np.linalg.norm(r)).tolist()
            if "alt" in o:
                o["alt"] = o["alt"] + on["units_mismatch"]["bias_m"]
        if "imu_saturation_hold" in on and "sat_t" in st and m.x is not None and not m.mars_lifted:
            o["imu_saturated"] = m.t - st["sat_t"] <= SCHIAPARELLI_SAT_S
            if "height_above_surface" in o:
                o["height_above_surface"] = o["height_above_surface"] * math.cos(math.radians(SCHIAPARELLI_ERR_DEG))
        if "touchdown_transient" in on and m.x is not None and m.central == "Mars" and not m.mars_lifted:
            h = m.altitude(m.x[:3], m.t) - m.ground_alt(m.x[:3], m.t)
            heat = any(s.el.kind == "heatshield" for s in m.present())
            if not heat and h <= MPL_LEG_DEPLOY_M:
                if "legs_t" not in st:
                    st["legs_t"] = m.t
                    m.event("legs_deployed", height_m=round(h, 1))
                    o["touchdown_sensors"] = list(on["touchdown_transient"]["legs"])   # the transient: one call only
                else:
                    o["touchdown_sensors"] = [False, False, False]
        return o

    def summary(self):
        return {"modes": {k: {kk: vv for kk, vv in v.items()} for k, v in self.on.items()}, "faults": list(self.seen)}


MODELS = [FlownFailures]
