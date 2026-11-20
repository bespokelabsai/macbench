"""Power, batteries, boiloff and freezing.

Methods of Mission, moved verbatim from sim.py. Mission inherits them from ResourcesMixin.

With ``MARSFLIGHT_POWER=1`` (core/wiring.py) the power balance uses the models instead of the v3 rules:
RTG decay (``models/power/arrays.rtg_power_W``), arrays by latitude, season, hour and dust on Mars
(``surface_solar``) and by distance, pointing and age in space (``space_array_W``), the hourly Jezero sink and tank
heaters (``models/thermal/surface``), bus survival heaters in space (``models/thermal/spacecraft.bus_heater_W``) and
the battery through the night within its depth of discharge, fade and round trip (``night_deficit_Wh``,
``models/power/eclipse``). ``MARSFLIGHT_ISRU=1`` makes oxygen with the MOXIE fit (``models/isru/moxie``). With both
off the code below runs the v3 rules unchanged.
"""
from __future__ import annotations

import math
import os
import time
import zlib
from dataclasses import dataclass, field
import numpy as np
from ... import data as D
from ... import vibro
from ... import terrain
from ...environment import EARTH_SOI, MARS_SOI, MARS_REF_RADIUS, Dispersions, World
from ...core.common import (  # noqa: F401
    MILESTONES, MILESTONE_WEIGHTS, ATM_TOP, EARTH_R_EQ, PARTIAL_APPROACH_D0, SCENARIO_PHASES, STATE_AFTER, FlightFailure, interp, unit, rotate_towards, newtonian_cd, parachute_peak_load, great_circle_km, ElementState,
)


class ResourcesMixin:
    # ------------------------------------------------------------------ resources
    def resources(self, dt, where):
        """Power, heaters, cryocoolers, boiloff and ISRU over dt seconds."""
        pres = self.present()
        if not pres:
            return
        sw = getattr(self, "sw", None) or {}
        new_power = bool(sw.get("POWER"))
        new_isru = bool(sw.get("ISRU"))
        geo = None   # the Sun on the landing site, for the power models
        if where == "mars_surface":
            sol = self.world.mars_sol(self.t)
            lat_s, lon_s = (self.surface_site[0], self.surface_site[1]) if self.surface_site else (None, None)
            tau = self.world.mars_tau(self.t, lat_s, lon_s)   # the dust.mars provider at the site
            if new_power or new_isru:
                from ..power import arrays as A
                from ...environment import mars_ls
                lat, ls = self._w_landing_lat(), mars_ls(self.t)
                dust_now = getattr(self, "array_dust", 0.0)
                out = A.surface_solar(lat, ls, self._sun_distance_au(), tau, dust_now)
                geo = {"lat": lat, "ls": ls, "mean": out["mean"], "normal": out["per_m2_normal"]}
                if new_power:
                    self.array_dust = dust_now + A.dust_settling_per_sol(tau) * dt / A.SOL_S
            sun = 1361.0 / 1.524 ** 2
            dust = max(0.3, 1 - D.SOLAR_DUST_LOSS_PER_SOL * ((self.t - (self.landed_t or self.t)) / 88775.244))
            solar_avg = sun * 0.30 * math.exp(-0.6 * tau) * dust
        else:
            r_au = self._sun_distance_au()
            solar_avg = 1361.0 / r_au ** 2 * 0.9
            dust = 1.0
        supply = 0.0
        battery = sum(s.battery_Wh for s in pres if s.battery_Wh > 0)
        nuclear = 0.0
        years = (self.t - self.t0) / (365.25 * 86400.0)
        lit, cos_sun = 1.0, 1.0
        if new_power and where != "mars_surface":
            from ..power import arrays as A
            arc = getattr(self, "w_arc", None) or {}
            lit = float(arc.get("lit", 1.0))
            cos_sun = A.pointing_cos(arc.get("thrusting", False), arc.get("dir"), self._w_sun_dir())
        for s in pres:
            e = s.el
            if not new_power:
                supply += e.solar_m2 * D.SOLAR_EFFICIENCY * solar_avg * self.disp.solar_factor
                nuclear += e.rtg_units * D.RTG["power_W"] + e.fission_units * D.FISSION["power_W"]
            elif where == "mars_surface":
                supply += e.solar_m2 * geo["mean"] * self.disp.solar_factor
                nuclear += A.rtg_power_W(e.rtg_units, years) + e.fission_units * D.FISSION["power_W"]
            else:
                supply += A.space_array_W(e.solar_m2, r_au, cos_sun, years) * lit * self.disp.solar_factor
                nuclear += A.rtg_power_W(e.rtg_units, years) + e.fission_units * D.FISSION["power_W"]
        supply += nuclear
        demand = D.BASE_POWER_W
        if "comms" in getattr(self, "w", {}):
            demand += sum(self.w["comms"].power_delta_W(s.el.id) for s in pres)
        if new_power and where != "mars_surface":
            from ...models.thermal.spacecraft import bus_heater_W
            flux = 1361.0 / r_au ** 2
            for s in pres:
                e = s.el
                if e.rtg_units or e.solar_m2 or e.battery_Wh or e.kind == "capsule":
                    demand += bus_heater_W(flux, lit, e.dry_mass)
        heaters = 0.0
        hourly = []        # (W/K, temperature to keep) of each heated tank, for the hourly sink
        boil = []
        cryo_lift_need = 0.0
        cooler_kw = sum(s.el.cryocooler_kW for s in pres)
        for s in pres:
            e = s.el
            if not e.is_stage or not e.tanks:
                continue
            for role, tk in e.tanks.items():
                prop = e.fuel if role == "fuel" else e.ox
                amount = s.fuel if role == "fuel" else s.ox
                if amount <= 0 and not (role == "ox" and self.isru_target == e.id):
                    continue
                P = D.PROPELLANTS[prop]
                if P["cryo"]:
                    if where == "mars_surface":
                        qf = (30.0 / e.mli_layers) * 8.0 if e.mli_layers else 2.0 * (self.disp.mars_ambient_T - P["T"])
                    else:
                        r_au = self._sun_distance_au()
                        qf = (30.0 / e.mli_layers) * r_au ** -0.5 if e.mli_layers else 250.0 / r_au ** 2 + 20.0
                    Q = qf * tk.area
                    boil.append((s, role, prop, Q))
                    cryo_lift_need += Q
                else:
                    keep = P["freeze"] + 10.0
                    if where == "mars_surface":
                        ql = e.heater_insulation_W_per_K_m2 * max(0.0, keep - self.disp.mars_ambient_T)
                        hourly.append((e.heater_insulation_W_per_K_m2 * tk.area, keep))
                    else:
                        ql = 30.0 / max(e.mli_layers, 1)
                    heaters += ql * tk.area
        heaters_h = None
        if new_power and where == "mars_surface":
            # the Jezero sink hour by hour, shifted by the world's mean surface temperature
            from ...models.thermal.surface import heaters_by_hour
            heaters_h = heaters_by_hour(hourly, self.w["surface_thermal"].sinks(geo["ls"]))
            heaters = sum(heaters_h) / len(heaters_h)
        demand += heaters
        cooler_in = min(cooler_kw * 1000.0, sum(Q * D.CRYOCOOLER[pr]["w_per_w"] for _, _, pr, Q in boil)) if boil else 0.0
        demand += cooler_in
        isru_kw = sum(s.el.isru_kW for s in pres) if (where == "mars_surface" and self.isru_target) else 0.0
        night_ok = True
        night = None
        if new_power and where == "mars_surface":
            # The battery carries the hours the arrays and RTGs do not cover, within its depth of discharge, its
            # fade, and with its round trip taken off the supply.
            from ..power import eclipse as E
            base = demand - heaters
            loads = [base + h - nuclear for h in heaters_h]
            normal = sum(s.el.solar_m2 for s in pres) * geo["normal"] * self.disp.solar_factor
            need = A.night_deficit_Wh(loads, normal, geo["lat"], geo["ls"])
            cap = E.capacity_fraction(years + E.BATTERY_AGE_AT_LAUNCH_YEARS, getattr(self, "battery_cycles", 0.0))
            usable = battery * cap * E.BATTERY_DEPTH_OF_DISCHARGE
            night_ok = need <= usable + 1e-9
            supply -= need * (1.0 / E.BATTERY_ROUND_TRIP - 1.0) / A.SOL_H
            if battery > 0:
                self.battery_cycles = getattr(self, "battery_cycles", 0.0) + need / (battery * cap or 1.0) * dt / A.SOL_S
            night = {"need_Wh": need, "usable_Wh": usable, "loads": loads, "normal": normal}
        elif where == "mars_surface" and nuclear < demand:
            # Solar-only on Mars needs batteries for the night (about half a sol at the night-time load).
            night_need_Wh = (demand - nuclear) * 12.3
            night_ok = battery >= night_need_Wh
        self.power_load_W = demand
        surplus = supply - demand
        if surplus < 0 or not night_ok:
            self.power_deficit_s += dt
            cooler_in = 0.0
            if self.power_deficit_s > 3 * 86400:
                if heaters > 0 and supply < D.BASE_POWER_W + heaters:
                    self.fail(f"power ran short for more than 3 days ({supply:.0f} W supply vs {demand:.0f} W demand"
                              f"{'' if night_ok else ', batteries too small for the night'}): propellant froze and the vehicle was lost")
                if supply < D.BASE_POWER_W:
                    self.fail(f"power ran short for more than 3 days ({supply:.0f} W supply vs {D.BASE_POWER_W:.0f} W avionics): vehicle lost")
        else:
            self.power_deficit_s = 0.0
        # boiloff with whatever the cryocoolers lift
        lift_total = 0.0
        if boil and cooler_in > 0:
            # spread cooler input proportional to heat leak
            Qsum = sum(Q for _, _, _, Q in boil)
            for s, role, prop, Q in boil:
                share = cooler_in * Q / Qsum
                lift = share / D.CRYOCOOLER[prop]["w_per_w"]
                net = max(0.0, Q - lift)
                self._boil(s, role, prop, net, dt)
                lift_total += lift
        else:
            for s, role, prop, Q in boil:
                self._boil(s, role, prop, Q, dt)
        if where == "mars_surface":
            self.track["power"] = {
                "sol": round(self.world.mars_sol(self.t), 2), "supply_W": round(supply, 1), "demand_W": round(demand, 1),
                "nuclear_W": round(nuclear, 1), "heaters_W": round(heaters, 1), "cryocooler_W": round(cooler_in, 1),
                "boiloff_heat_W": round(sum(Q for _, _, _, Q in boil), 1), "battery_Wh": round(battery, 1),
                "night_ok": night_ok, "dust_tau": round(tau, 3), "isru_kW": round(isru_kw, 2),
                "ops_ok_sols": round(self.surface_ops_ok_s / 88775.244, 2)}
            if night is not None:
                self.track["power"].update({"night_need_Wh": round(night["need_Wh"], 1),
                                            "night_usable_Wh": round(night["usable_Wh"], 1),
                                            "array_dust": round(getattr(self, "array_dust", 0.0), 4)})
            if "sample_thermal" in getattr(self, "w", {}):
                self._w_samples(self.w["sample_thermal"].surface(dt, self.disp.mars_ambient_T))
        elif "sample_thermal" in getattr(self, "w", {}):
            self._w_samples(self.w["sample_thermal"].space(dt, self._sun_distance_au()))
        if where == "mars_surface" and surplus >= 0 and night_ok:
            self.surface_ops_ok_s += dt
            if isru_kw > 0:
                avail_kw = min(isru_kw, surplus / 1000.0)
                tgt = self.find(self.isru_target)
                if tgt is not None:
                    if new_isru:
                        made = self._isru_moxie(isru_kw, surplus, night, geo, dt)
                    else:
                        made = avail_kw * D.ISRU_KG_PER_SOL_PER_KW * dt / 88775.244
                    made *= self.truth.get("isru", 1.0)
                    room = tgt.el.capacity.get("ox", 0.0) - tgt.ox
                    tgt.ox += max(0.0, min(made, room))
        elif new_isru and "isru" in getattr(self, "w", {}):
            self.w["isru"].stop()   # no power to spare: the plant goes cold and must heat up again

    def _isru_moxie(self, isru_kw, surplus_W, night, geo, dt):
        """Oxygen (kg) over dt from the MOXIE fit: the plant draws what the power to spare and the night
        battery allow, heats up after a stop, and makes oxygen at MOXIE's measured rate for the ambient temperature
        and the seasonal air density."""
        from ...models.isru import moxie as M
        from ...environment import mars_seasonal_pressure_factor
        from ..power import arrays as A
        rated = isru_kw * 1000.0
        if night is None:   # ISRU on with the v3 power rules: a flat load and no battery limit at night
            loads, normal, usable, need = [0.0] * A.STEPS, 0.0, float("inf"), 0.0
        else:
            loads, normal, usable, need = night["loads"], night["normal"], night["usable_Wh"], night["need_Wh"]
        p = M.plant_power_W(rated, surplus_W, loads, normal, geo["lat"], geo["ls"], usable, need)
        return self.w["isru"].step(p, rated, dt / 3600.0, self.disp.mars_ambient_T,
                                   mars_seasonal_pressure_factor(geo["ls"]))

    def _boil(self, s, role, prop, Q, dt):
        m = Q * dt / D.PROPELLANTS[prop]["hvap"]
        if role == "fuel":
            s.fuel = max(0.0, s.fuel - m)
        else:
            s.ox = max(0.0, s.ox - m)
