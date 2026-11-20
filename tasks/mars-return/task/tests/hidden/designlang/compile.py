"""Compile a vehicle model in SysML v2 into marsflight's internal design (today's `vehicle.json`).

This is the hidden half of the compiler. It runs next to the simulator, never in a team's image,
because it turns what the team wrote into simulator inputs with the simulator's own data: propellant
densities give tank capacities, TPS densities give areal masses, and the aerodynamic tables give the
trimmed lift-to-drag ratio. The internal design it writes is exactly the format `marsflight` reads
today, so the simulator and everything built on it (verifier, scoring, program office) is unchanged.

    python3 -m designlang.compile MODEL.sysml [MORE.sysml ...] -o vehicle.json [--report report.json]

What the v1 compiler supports, and what it does not, is listed in docs/DESIGN_LANGUAGE.md.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from ..walk import walk

DOME_ASPECT = 1.0 / math.sqrt(2.0)
RADIUS_TOL_M = 5e-4


class CompileError(Exception):
    """public: the errors come from the public checker, so a team may read them as they are."""

    def __init__(self, errors, public=False):
        super().__init__("\n".join(errors))
        self.errors = errors
        self.public = public


def compile_files(paths):
    """Returns (internal design dict, report dict). Raises CompileError with every problem found."""
    desc, errors, warnings = walk(paths)
    if errors:
        raise CompileError(errors, public=True)
    c = _Compiler(desc)
    design = c.run()
    if c.errors:
        raise CompileError(c.errors)
    return design, {"warnings": warnings + c.warnings, "notes": c.notes, "description": desc}


class _Compiler:
    def __init__(self, desc):
        from marsflight import data as D
        from marsflight import economics as E
        from marsflight import vehicle as V
        self.D, self.E, self.V = D, E, V
        self.desc = desc
        self.errors, self.warnings, self.notes = [], [], []

    def run(self):
        d = self.desc
        out = {"launch_epoch_utc": d["launch_epoch_utc"], "engines": {}, "elements": []}
        for name, spec in d["engines"].items():
            out["engines"][name] = {k: v for k, v in spec.items() if v is not None}
        for e in d["elements"]:
            out["elements"].append(self.element(e, out["engines"]))
        # mission-level fields of the wired models (comms, launch range)
        if d.get("dsn_passes_per_day") is not None:
            out["dsn"] = {"passes_per_day": int(d["dsn_passes_per_day"])}
        if d.get("ground_wind_limit_mps") is not None:
            out["launch"] = {"ground_wind_limit_ms": float(d["ground_wind_limit_mps"])}
        if d.get("launch_site") is not None:
            out.setdefault("launch", {})["site"] = str(d["launch_site"])
        try:   # redundancy: the model's own check, so the rules live in one place
            from marsflight.models.failures.inflight import check_design
            self.errors += check_design(out)
        except ImportError:
            pass
        return out

    def element(self, e, engines):
        k = e["kind"]
        s = {"id": e["id"], "kind": k}
        if k == "stage":
            self.stage(e, s, engines)
        elif k == "fairing":
            s.update(diameter_m=e["diameter_m"], length_m=e["length_m"])
        elif k in ("heatshield", "capsule"):
            s.update(diameter_m=e["diameter_m"], half_angle_deg=e["half_angle_deg"], nose_radius_m=e["nose_radius_m"])
            s.update(self.tps(e))
            if e["trim_angle_deg"]:
                s["ld_trim"] = self.ld_trim(e)
            if k == "capsule":
                s["structure_mass_kg"] = e["containment_mass_kg"]
                if e.get("sample_alpha_over_eps") is not None:
                    s["sample_alpha_over_eps"] = e["sample_alpha_over_eps"]
                if e.get("sample_containers") is not None:
                    s["sample_containers"] = e["sample_containers"]
                s["landing"] = e["landing"]
                if e.get("max_load_factor_g") is not None:
                    s["g_limit"] = e["max_load_factor_g"]
        elif k == "backshell":
            s.update(self.tps(e))
        elif k == "parachute":
            s.update(type=e["canopy"], diameter_m=e["diameter_m"], design_q_Pa=e["design_q_Pa"])
        self.common(e, s)
        return s

    def tps(self, e):
        """The areal mass of the TPS layer is its thickness times the material's density."""
        t = e["tps"]
        return {"tps": t["material"], "areal_mass_kg_m2": t["thickness_m"] * self.D.TPS[t["material"]]["rho"]}

    def ld_trim(self, e):
        """Trimmed L/D of a sphere-cone grows about linearly with the trim angle of attack: 0.015 per degree
        at 70 deg (MSL trims near 16 deg for L/D 0.24), scaled with the cone's maximum trim L/D."""
        return e["trim_angle_deg"] * self.V._ld_max(e["half_angle_deg"]) / 20.0

    def common(self, e, s):
        if e.get("slew_rate_deg_s") is not None:
            s["slew_rate_deg_s"] = e["slew_rate_deg_s"]
        if e.get("rcs_thrust_N") is not None:
            s["rcs_prop_kg"], s["rcs_thrust_N"] = e["rcs_prop_kg"], e["rcs_thrust_N"]
        for key, val in (("rtg_units", e["rtg_units"]), ("fission_units", e["fission_units"]),
                         ("solar_array_m2", e["solar_array_m2"]), ("battery_kWh", e["battery_J"] / 3.6e6),
                         ("cryocooler_kW", e["cryocooler_W"] / 1e3), ("isru_kW", e["isru_W"] / 1e3)):
            if val:
                s[key] = val
        extra = e["equipment_kg"] + s.pop("_unused_tank_kg", 0.0)
        if extra:
            s["extra_mass_kg"] = extra
        if e.get("surface_insulation_W_per_K_m2") is not None:
            s["surface_insulation_W_per_K_m2"] = e["surface_insulation_W_per_K_m2"]
        for key in ("qual_acoustic_dB", "qual_shock_g"):
            if e.get(key) is not None:
                s[key] = e[key]
        if e.get("redundancy"):
            s["redundancy"] = {k: v for k, v in e["redundancy"].items() if v is not None}
        if e.get("comms"):
            s["comms"] = {k: v for k, v in e["comms"].items() if v is not None}

    # ---- stages
    def stage(self, e, s, engines):
        s["diameter_m"] = e["diameter_m"]
        R = e["diameter_m"] / 2
        tanks = {t["role"]: t for t in e["tanks"]}
        if e["engines"]:
            s["engines"] = [{"engine": x["engine"], "count": x["count"]} for x in e["engines"]]
        s["interstage"] = bool(e["interstage_below"])
        if e["mli_layers"]:
            s["mli_layers"] = e["mli_layers"]
        if e["sofi"]:
            s["sofi"] = True
        if e.get("legs_touchdown_speed_mps") is not None:
            s["legs"] = {"touchdown_speed_mps": e["legs_touchdown_speed_mps"]}
        if e.get("pogo_L"):
            s["pogo_accumulator_L"] = dict(e["pogo_L"])
        if e.get("trn"):
            s["trn"] = True
        if e.get("gauge") is not None:                      # propellant gauging
            s["gauge"] = e["gauge"]
        if e.get("low_level_cutoff") is not None:
            s["low_level_cutoff"] = bool(e["low_level_cutoff"])
        if e.get("pyro_isolation_valves"):                  # propulsion flaws: check valve migration
            s["pyro_isolation_valves"] = True
        if e.get("fins"):                                   # fins, Barrowman's normal force in 6-DOF
            s["fins"] = {k: v for k, v in e["fins"].items() if v is not None}
        # the common bulkhead (merged), and fields of simulator changes still in review, which
        # today's simulator ignores.
        if e["common_bulkhead"]:
            s["common_bulkhead"] = True
        if e["differential_throttle"]:
            s["differential_throttle"] = True
        if e["loaded_mixture_ratio"] is not None:
            s["loaded_mixture_ratio"] = e["loaded_mixture_ratio"]
        if not tanks:
            return
        where = f"stage {e['id']}"
        f, o = tanks["fuel"], tanks["ox"]
        for key, label in (("material", "material"), ("ullage", "ullageFraction"), ("layup", "layup")):
            if f[key] != o[key]:
                self.errors.append(f"{where}: the simulator takes one tank {label} per stage, and the tanks differ ({f[key]} and {o[key]})")
        s.update(material=f["material"], tank_pressure_bar=e["meop_Pa"] / 1e5,
                 wall_thickness_mm={"fuel": f["wall_m"] * 1e3, "ox": o["wall_m"] * 1e3},
                 fuel=f["propellant"], oxidizer=o["propellant"])
        if abs(f["ullage"] - 0.03) > 1e-9:
            s["ullage_fraction"] = f["ullage"]
        if e["balloon_tanks"]:
            s["balloon_tanks"] = True
        for key in ("stiffeners", "baffles"):
            if (key in f) != (key in o) or f.get(key) != o.get(key):
                self.errors.append(f"{where}: the simulator takes one {key} design for both tanks of a stage")
        if "stiffeners" in f:
            s["stiffeners"] = {_SNAKE[k]: v * 1e3 for k, v in f["stiffeners"].items() if v is not None}
        if "baffles" in f:
            s["baffles"] = dict(f["baffles"])
        of = self.mixture_ratio(e, engines)
        for x in e["engines"]:
            spec = engines[x["engine"]]
            ef, eo = self.engine_propellants(spec)
            if (ef, eo) != (f["propellant"], o["propellant"]):
                self.errors.append(f"{where}: engine {x['part']} burns {ef}/{eo} but the tanks hold {f['propellant']}/{o['propellant']}")
        # Capacity from the drawn tanks. The simulator rebuilds the tanks from the capacity, so a tank drawn
        # bigger than the mixture ratio needs is carried as its unused shell and stage systems.
        geo = {}
        for role, t in (("fuel", f), ("ox", o)):
            if abs(t["dome_aspect"] - DOME_ASPECT) > 5e-4:
                self.errors.append(f"{where}: tank {t['name']} has domeAspectRatio {t['dome_aspect']}; the v1 compiler takes 0.7071 (1/sqrt 2) domes only")
            r, L = t["radius_m"], t["barrel_m"]
            if L > 0 and abs(r - R) > RADIUS_TOL_M:
                self.errors.append(f"{where}: tank {t['name']} has a barrel, so its radius must be the stage radius {R:.4f} m (the v1 compiler)")
            vol = math.pi * r * r * L + 2 * self.V.dome_volume(r)
            area = 2 * math.pi * r * L + 2 * self.V.dome_area(r)
            geo[role] = (vol, area, t)
        rho = {role: self.D.PROPELLANTS[geo[role][2]["propellant"]]["rho"] for role in geo}
        ull = f["ullage"]
        if e["common_bulkhead"]:
            # One tank body: the two barrels end to end, closed by two outer domes. The bulkhead inside adds no
            # volume and sits where the mixture ratio puts it, so the body's volume sets the capacity.
            if abs(f["radius_m"] - o["radius_m"]) > RADIUS_TOL_M:
                self.errors.append(f"{where}: with a CommonBulkhead both tanks have one radius")
            r = f["radius_m"]
            vol = math.pi * r * r * (f["barrel_m"] + o["barrel_m"]) + 2 * self.V.dome_volume(r)
            per_kg = (1 / (1 + of)) / rho["fuel"] + (of / (1 + of)) / rho["ox"]
            total = vol / (1 + ull) / per_kg
            geo = {}
        else:
            cap = {role: geo[role][0] * rho[role] / (1 + ull) for role in geo}
            total = min(cap["fuel"] * (1 + of), cap["ox"] * (1 + of) / of)
        s["tank_capacity_kg"] = total
        s["propellant_kg"] = e["propellant_kg"]
        if e["oxidizer_loaded_fraction"] != 1.0:
            s["oxidizer_loaded_fraction"] = e["oxidizer_loaded_fraction"]
        if e["propellant_kg"] > total * (1 + 1e-9):
            self.errors.append(f"{where}: the tanks hold {total:,.0f} kg of propellant at mixture ratio {of:g}; the load is {e['propellant_kg']:,.0f} kg")
        mat = self.D.MATERIALS[f["material"]]
        v_int = sum(self.V._tank(geo[r][2]["propellant"], total * sh, R, geo[r][2]["wall_m"], f["material"], ull).volume
                    for r, sh in (("fuel", 1 / (1 + of)), ("ox", of / (1 + of))) if r in geo)
        unused = 0.0
        for role, share in (("fuel", 1 / (1 + of)), ("ox", of / (1 + of))):
            if role not in geo:
                continue
            vol, area, t = geo[role]
            tk = self.V._tank(t["propellant"], total * share, R, t["wall_m"], f["material"], ull)
            dv, da = vol - tk.volume, area - tk.area
            if dv > 1e-6 * vol:
                extra = da * t["wall_m"] * mat["rho"] * mat.get("liner_factor", 1.0) + self._systems(v_int + dv) - self._systems(v_int)
                unused += extra
                if extra >= 0.05:
                    self.notes.append(f"{where}: tank {t['name']} is {dv:.3f} m^3 bigger than the mixture ratio needs; "
                                      f"its unused shell and systems ({extra:.1f} kg) are carried as extra mass")
        if unused:
            s["_unused_tank_kg"] = unused

    def _systems(self, volume):
        """The stage's secondary structure and systems for a tank volume, as the simulator sizes them."""
        D = self.D
        if hasattr(D, "STAGE_SYSTEMS_KG_PER_M3"):
            return D.STAGE_SYSTEMS_KG_PER_M3 * volume
        return D.STAGE_SYSTEMS_KG * volume ** D.STAGE_SYSTEMS_EXPONENT

    def engine_propellants(self, spec):
        if "catalog" in spec:
            c = self.E.CATALOG_ENGINES[spec["catalog"]]["spec"]
            return c["fuel"], c["oxidizer"]
        return spec.get("fuel"), spec.get("oxidizer")

    def mixture_ratio(self, e, engines):
        if e["loaded_mixture_ratio"] is not None:
            return e["loaded_mixture_ratio"]
        ofs = set()
        for x in e["engines"]:
            spec = engines[x["engine"]]
            ofs.add(round(self.E.CATALOG_ENGINES[spec["catalog"]]["spec"]["mixture_ratio"] if "catalog" in spec else spec["mixture_ratio"], 4))
        if len(ofs) > 1:
            self.errors.append(f"stage {e['id']}: all engines on one stage must share a mixture ratio")
        if not ofs:
            self.errors.append(f"stage {e['id']}: a stage with tanks and no engines needs loadedMixtureRatio")
            return 3.0
        return next(iter(ofs))


_SNAKE = {"stringerHeight": "stringer_height_mm", "stringerThickness": "stringer_thickness_mm",
          "stringerSpacing": "stringer_spacing_mm", "ringSpacing": "ring_spacing_mm",
          "stringerFlangeWidth": "stringer_flange_mm", "ringHeight": "ring_height_mm", "ringThickness": "ring_thickness_mm"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    try:
        design, report = compile_files(a.model)
    except CompileError as ex:
        print("\n".join(ex.errors), file=sys.stderr)
        return 1
    with open(a.out, "w") as f:
        json.dump(design, f, indent=1)
    if a.report:
        with open(a.report, "w") as f:
            json.dump(report, f, indent=1)
    for m in report["warnings"] + report["notes"]:
        print(m, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
