"""Read a vehicle model into a plain description, and check it, without any hidden data.

This is the public half of the compiler, the part a team's image may carry. It checks syntax, names,
types, units, required attributes, designations and how the stack joins, and returns every element
with the values the team wrote, in SI units (angles in degrees). It derives nothing: no mass, size,
strength or performance.
"""
from __future__ import annotations

import re

from . import designations as DN
from .model import Inst, load

KIND_BY_DEF = [  # most specific first
    ("EarthEntryCapsule", "capsule"), ("Heatshield", "heatshield"), ("Backshell", "backshell"),
    ("Parachute", "parachute"), ("PayloadFairing", "fairing"), ("Stage", "stage"),
]
CYCLES = {"pressureFed": "pressure_fed", "electricPump": "electric_pump", "gasGenerator": "gas_generator",
          "expander": "expander", "fuelRichStagedCombustion": "staged_fuel",
          "oxidizerRichStagedCombustion": "staged_ox", "fullFlowStagedCombustion": "full_flow"}
COOLING = {"regenerative": "regen", "ablative": "ablative", "radiative": "radiative"}
IGNITION = {"hypergolic": "hypergolic", "sparkTorch": "spark_torch", "teaTeb": "tea_teb"}
CANOPY = {"diskGapBand": "dgb", "ringsail": "ringsail"}
INJECTOR = {"conventional": "standard", "pintle": "pintle"}
GAUGES = {"capacitance": "capacitance", "pointSensors": "point_sensors", "pvt": "pvt"}
REDUNDANCY = {"single": "single", "dual": "dual", "crossStrapped": "cross_strapped"}
OXIDIZERS = {"LOX", "NTO"}
_EPOCH = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


class Walker:
    def __init__(self, model):
        self.m = model
        self.errors = list(model.errors)
        self.warnings = []

    def err(self, inst, msg):
        self.errors.append(f"{inst.file}:{inst.line}: {inst.path}: {msg}")

    def is_a(self, inst, name):
        return self.m.specializes(inst.type, name)

    def children_of(self, inst, defname):
        return [c for c in inst.parts() if self.is_a(c, defname)]

    def one(self, inst, defname):
        cs = self.children_of(inst, defname)
        if len(cs) > 1:
            self.err(inst, f"has {len(cs)} parts of type {defname}; the design language takes one")
        return cs[0] if cs else None

    def need(self, inst, name):
        c = inst.children.get(name)
        if c is None:
            self.err(inst, f"has no attribute {name}")
            return None
        if c.value is None:
            if not c.optional:
                self.err(inst, f"set {name}")
            return None
        return c.value

    def required_attributes(self, inst):
        for c in inst.children.values():
            if c.metaclass == "attribute" and c.value is None and not c.optional:
                self.err(inst, f"set {c.name}")
            elif c.metaclass in ("part", "item", "requirement") and c.count > 0:
                self.required_attributes(c)

    def positive(self, inst, name, allow_zero=False):
        v = self.need(inst, name)
        if v is not None and not (v > 0 or (allow_zero and v == 0)):
            self.err(inst, f"{name} must be {'zero or more' if allow_zero else 'more than zero'}")
        return v

    # ---- top level
    def run(self):
        if self.m.errors:
            return None
        insts = self.m.instances()
        if self.m.errors:            # names, types and units first: later checks would only repeat them
            self.errors = list(self.m.errors)
            return None
        stacks = [i for i in insts if self.is_a(i, "MissionStack")]
        if len(stacks) != 1:
            self.errors.append(f"the model needs exactly one part typed by MissionStack (found {len(stacks)})")
            return None
        stack = stacks[0]
        self.required_attributes(stack)
        out = {"launch_epoch_utc": self.need(stack, "launchEpoch"), "engines": {}, "elements": []}
        if "dsnPassesPerDay" in stack.children:
            out["dsn_passes_per_day"] = self.need(stack, "dsnPassesPerDay")
            if out["dsn_passes_per_day"] is not None and out["dsn_passes_per_day"] not in (1, 2, 3):
                self.err(stack, "dsnPassesPerDay must be 1, 2 or 3")
        if "groundWindLimit" in stack.children:
            out["ground_wind_limit_mps"] = self.need(stack, "groundWindLimit")
            if out["ground_wind_limit_mps"] is not None and not (0 < out["ground_wind_limit_mps"] <= 40):
                self.err(stack, "groundWindLimit must be more than 0 and at most 40 m/s")
        if "launchSite" in stack.children:
            out["launch_site"] = self.need(stack, "launchSite")
            if out["launch_site"] is not None and out["launch_site"] not in ("LC-39A", "SLC-40", "LC-46", "SLC-4E"):
                self.err(stack, "launchSite must be \"LC-39A\", \"SLC-40\", \"LC-46\" or \"SLC-4E\"")
        if out["launch_epoch_utc"] is not None and not _EPOCH.match(out["launch_epoch_utc"]):
            self.err(stack, "launchEpoch must look like 2030-12-28T00:00:00 (UTC)")
        elements = {c.name: c for c in stack.parts() if self.is_a(c, "StackElement")}
        for c in stack.parts():
            if c.name not in elements:
                self.err(c, "a part of the stack must be a StackElement (a stage, fairing, heatshield, backshell, parachute or capsule)")
        order, interstage = self.stack_order(stack, elements)
        for name in order:
            e = elements[name]
            d = self.element(e, out["engines"])
            if d is not None:
                d["interstage_below"] = interstage.get(name, False)
                out["elements"].append(d)
        return out

    def stack_order(self, stack, elements):
        above, below, interstage = {}, {}, {}
        for c, sc in stack.connections:
            tname = c.types[0] if c.types else None
            if tname is None or not self.m.specializes(self.m.resolve(tname, sc, c.line, c.path), "StageJoint"):
                if not (tname and tname.endswith("PropellantFeed")):
                    self.errors.append(f"{c.path}:{c.line}: a connection between stack elements must be a StageJoint")
                continue
            a, b = c.ends
            ea, pa = (a.split(".") + [""])[:2]
            eb, pb = (b.split(".") + [""])[:2]
            if ea not in elements or eb not in elements or pa != "fwd" or pb != "aft":
                self.errors.append(f"{c.path}:{c.line}: a StageJoint connects LOWER.fwd to UPPER.aft of two stack elements, not {a} to {b}")
                continue
            if ea in above or eb in below:
                self.errors.append(f"{c.path}:{c.line}: {ea if ea in above else eb} already has a joint on that side")
                continue
            above[ea], below[eb] = eb, ea
            for m in c.children:
                if m.name == "interstage" and m.value is not None:
                    interstage[eb] = bool(m.value.value)
        bottoms = [n for n in elements if n not in below]
        if len(bottoms) != 1:
            self.errors.append(f"StageJoints must join every stack element into one column; it has {len(bottoms)} bottom elements ({', '.join(bottoms)})")
            return [], interstage
        order, n = [], bottoms[0]
        while n is not None and n not in order:
            order.append(n)
            n = above.get(n)
        if len(order) != len(elements):
            self.errors.append("StageJoints do not reach every stack element: " + ", ".join(sorted(set(elements) - set(order))))
        return order, interstage

    # ---- elements
    def element(self, e, engines):
        kind = next((k for d, k in KIND_BY_DEF if self.is_a(e, d)), None)
        self.required_attributes(e)
        d = {"id": e.name, "kind": kind, "type": e.type.name if e.type is not None and hasattr(e.type, "name") else e.type_name}
        if kind == "stage":
            self.stage(e, d, engines)
        elif kind == "fairing":
            d["diameter_m"] = self.positive(e, "outerDiameter")
            d["length_m"] = self.positive(e, "length")
        elif kind in ("heatshield", "capsule"):
            d["diameter_m"] = self.positive(e, "baseDiameter")
            d["half_angle_deg"] = self.need(e, "halfAngle")
            d["nose_radius_m"] = self.positive(e, "noseRadius")
            d["trim_angle_deg"] = self.need(e, "trimAngleOfAttack")
            d["tps"] = self.tps(e)
            if kind == "capsule":
                d["landing"] = self.need(e, "landing")
                c = self.one(e, "SampleContainmentAssembly")
                if c is None:
                    self.err(e, "needs a SampleContainmentAssembly part")
                else:
                    d["containment_mass_kg"] = self.positive(c, "mass")
                    if "sampleCarrierAlphaOverEpsilon" in c.children:
                        ae = self.need(c, "sampleCarrierAlphaOverEpsilon")
                        if ae is not None and not (0.05 <= ae <= 10.0):
                            self.err(c, "sampleCarrierAlphaOverEpsilon must be 0.05 to 10")
                        d["sample_alpha_over_eps"] = ae
                    n = self.need(c, "sampleContainers")
                    d["sample_containers"] = int(n) if n is not None else None
                reqs = [c for c in e.children.values() if c.metaclass == "requirement" and self.is_a(c, "DecelerationLimit")]
                if reqs:
                    d["max_load_factor_g"] = self.positive(reqs[0], "maxLoadFactor")
        elif kind == "backshell":
            d["tps"] = self.tps(e)
        elif kind == "parachute":
            d["canopy"] = CANOPY.get(self.need(e, "canopy"))
            d["diameter_m"] = self.positive(e, "nominalDiameter")
            d["design_q_Pa"] = self.positive(e, "designDynamicPressure")
        self.common(e, d)
        return d

    def tps(self, e):
        t = self.one(e, "ThermalProtection")
        if t is None:
            self.err(e, "needs a ThermalProtection part")
            return None
        mat = self.need(t, "material")
        if mat is not None and mat not in DN.TPS:
            self.err(t, f"TPS material {mat!r} is not one of {', '.join(DN.TPS)}")
        return {"material": DN.TPS.get(mat), "thickness_m": self.positive(t, "thickness")}

    def common(self, e, d):
        """Subsystems any element may carry."""
        acs = self.one(e, "AttitudeControlSystem")
        if acs is not None:
            d["slew_rate_deg_s"] = self.positive(acs, "maxSlewRate")
        rcs = self.one(e, "ReactionControlSystem")
        if rcs is not None:
            p = self.need(rcs, "propellant")
            try:
                DN.propellant(p, DN.RCS_PROPELLANTS)
            except ValueError as ex:
                self.err(rcs, str(ex))
            d["rcs_prop_kg"] = self.positive(rcs, "propellantLoad", allow_zero=True)
            d["rcs_thrust_N"] = self.positive(rcs, "totalThrust")
        d["rtg_units"] = sum(c.count for c in self.children_of(e, "MMRTG"))
        d["fission_units"] = sum(c.count for c in self.children_of(e, "FissionPowerUnit"))
        d["solar_array_m2"] = sum((self.positive(c, "area") or 0.0) * c.count for c in self.children_of(e, "SolarArray"))
        d["battery_J"] = sum((self.positive(c, "energy") or 0.0) * c.count for c in self.children_of(e, "Battery"))
        d["cryocooler_W"] = sum((self.positive(c, "inputPower") or 0.0) * c.count for c in self.children_of(e, "Cryocooler"))
        d["isru_W"] = sum((self.positive(c, "inputPower") or 0.0) * c.count for c in self.children_of(e, "IsruOxygenPlant"))
        d["equipment_kg"] = sum((self.positive(c, "mass") or 0.0) * c.count for c in self.children_of(e, "EquipmentItem"))
        blanket = self.one(e, "SurfaceThermalBlanket")
        if blanket is not None:
            d["surface_insulation_W_per_K_m2"] = self.positive(blanket, "heatLeakCoefficient")
        qual = self.one(e, "QualificationLevels")
        if qual is not None:
            d["qual_acoustic_dB"] = self.need(qual, "acousticLevel")
            d["qual_shock_g"] = self.need(qual, "shockLevel")
        red = self.one(e, "Redundancy")
        if red is not None:
            r = {"avionics": REDUNDANCY.get(self.need(red, "avionics"))}
            for attr, key, hi in (("rcsBranches", "rcs_branches", 2), ("landingRadars", "landing_radars", 2),
                                  ("pyroInitiators", "pyro_initiators", 2), ("batteryStrings", "battery_strings", 4),
                                  ("isruUnits", "isru_units", 2)):
                v = self.need(red, attr)
                if v is not None and not (isinstance(v, (int, float)) and float(v).is_integer() and 1 <= v <= hi):
                    self.err(red, f"{attr} must be a whole number from 1 to {hi}")
                    v = None
                r[key] = None if v is None else int(v)
            d["redundancy"] = r
        tel = self.one(e, "TelecomSystem")
        if tel is not None:
            hga = self.positive(tel, "highGainAntennaDiameter", allow_zero=True)
            tx = self.positive(tel, "transmitPower")
            if hga is not None and hga > 5.0:
                self.err(tel, "highGainAntennaDiameter must be at most 5 m")
            if tx is not None and not (1.0 <= tx <= 500.0):
                self.err(tel, "transmitPower must be 1 to 500 W")
            d["comms"] = {"band": self.need(tel, "band"), "hga_m": hga, "tx_rf_W": tx,
                          "gimbal": self.need(tel, "gimbal"), "uhf": self.need(tel, "uhfRelay")}

    def stage(self, e, d, engines):
        d["diameter_m"] = self.positive(e, "outerDiameter")
        d["propellant_kg"] = self.positive(e, "propellantLoad", allow_zero=True)
        d["oxidizer_loaded_fraction"] = self.need(e, "oxidizerLoadFraction")
        d["loaded_mixture_ratio"] = self.need(e, "loadedMixtureRatio")
        d["differential_throttle"] = self.need(e, "differentialThrottle")
        d["tanks"] = []
        for t in self.children_of(e, "PropellantTank"):
            if t.count != 1:
                self.err(t, "declare each propellant tank as its own part, with no count")
            d["tanks"].append(self.tank(t, d.get("diameter_m")))
        roles = [t["role"] for t in d["tanks"] if t["role"]]
        if d["tanks"] and sorted(roles) != ["fuel", "ox"]:
            self.err(e, "a stage with tanks needs one fuel tank and one oxidizer tank")
        d["engines"] = []
        for c in self.children_of(e, "RocketEngine"):
            key = c.type.name
            spec = self.engine(c)
            if key in engines and spec is not None and engines[key] != spec:
                self.err(c, f"engine definition {key} is used with two different specifications")
            engines.setdefault(key, spec)
            d["engines"].append({"engine": key, "count": c.count, "part": c.name})
        if d["engines"] and not d["tanks"]:
            self.err(e, "has engines but no propellant tanks")
        self.feeds(e, d)
        press = self.one(e, "PressurizationSystem")
        if d["tanks"]:
            if press is None:
                self.err(e, "has tanks but no PressurizationSystem (set its meop)")
            else:
                d["meop_Pa"] = self.positive(press, "meop")
                d["balloon_tanks"] = self.need(press, "balloonTanks")
                if "pyroIsolationValves" in press.children:
                    d["pyro_isolation_valves"] = bool(self.need(press, "pyroIsolationValves"))
        fins = self.one(e, "FinSet")
        if fins is not None:
            n = self.need(fins, "finCount")
            if n is not None and not (isinstance(n, (int, float)) and float(n).is_integer() and 2 <= n <= 8):
                self.err(fins, "finCount must be a whole number from 2 to 8")
                n = None
            d["fins"] = {"count": None if n is None else int(n), "span_m": self.positive(fins, "span"),
                         "root_chord_m": self.positive(fins, "rootChord"),
                         "tip_chord_m": self.positive(fins, "tipChord", allow_zero=True),
                         "sweep_m": self.positive(fins, "sweep", allow_zero=True)}
        mli = self.one(e, "MultiLayerInsulation")
        d["mli_layers"] = int(self.positive(mli, "layers")) if mli is not None else 0
        d["sofi"] = bool(self.children_of(e, "SprayOnFoamInsulation"))
        d["common_bulkhead"] = bool(self.children_of(e, "CommonBulkhead"))
        legs = self.one(e, "LandingGear")
        if legs is not None:
            d["legs_touchdown_speed_mps"] = self.positive(legs, "touchdownSpeedRating")
        acc = self.one(e, "PogoAccumulator")
        if acc is not None:
            vol = {"ox": self.need(acc, "oxidizerGasVolume"), "fuel": self.need(acc, "fuelGasVolume")}
            d["pogo_L"] = {k: v * 1e3 for k, v in vol.items() if v is not None}
        d["trn"] = bool(self.children_of(e, "TerrainRelativeNavigation"))
        gauging = self.one(e, "PropellantGauging")
        if gauging is not None:
            d["gauge"] = GAUGES.get(self.need(gauging, "gauge"))
            d["low_level_cutoff"] = self.need(gauging, "lowLevelCutoff")

    def tank(self, t, stage_diameter):
        prop = self.need(t, "propellant")
        try:
            internal = DN.propellant(prop)
        except ValueError as ex:
            self.err(t, str(ex))
            internal = None
        mat = self.need(t, "material")
        if mat is not None and mat not in DN.STRUCTURAL:
            self.err(t, f"material {mat!r} is not one of {', '.join(DN.STRUCTURAL)}")
        layup = self.need(t, "layup")
        if mat in DN.COMPOSITES and not DN.layup_ok(layup):
            self.err(t, f"a composite tank needs a layup such as \"[45/-45/0/90]s\" (got {layup!r})")
        if mat in DN.METALS and layup is not None:
            self.err(t, "a metal tank has no layup")
        r = self.positive(t, "radius")
        if r is not None and stage_diameter is not None and r > stage_diameter / 2 + 5e-4:
            self.err(t, f"radius {r:.3f} m is larger than the stage radius {stage_diameter / 2:.3f} m")
        out = {"name": t.name, "propellant": internal, "role": None if internal is None else ("ox" if internal in OXIDIZERS else "fuel"),
               "material": DN.STRUCTURAL.get(mat), "layup": layup, "radius_m": r,
               "barrel_m": self.positive(t, "barrelLength", allow_zero=True),
               "dome_aspect": self.positive(t, "domeAspectRatio"), "wall_m": self.positive(t, "wallThickness"),
               "ullage": self.need(t, "ullageFraction")}
        st = self.one(t, "StringerRingStiffening")
        if st is not None:
            out["stiffeners"] = {k: st.get(k) for k in ("stringerHeight", "stringerThickness", "stringerSpacing", "ringSpacing",
                                                         "stringerFlangeWidth", "ringHeight", "ringThickness")}
        bf = self.one(t, "RingBaffles")
        if bf is not None:
            out["baffles"] = {"width_frac": self.positive(bf, "widthFraction"), "spacing_m": self.positive(bf, "spacing")}
        return out

    def engine(self, c):
        if self.is_a(c, "ProcuredEngine"):
            des = self.need(c, "designation")
            if des is not None and des not in DN.PROCURED_ENGINES:
                self.err(c, f"no flight-proven engine {des!r}; choose from {', '.join(DN.PROCURED_ENGINES)}")
            spec = {"catalog": des}
        elif self.is_a(c, "LiquidRocketEngine"):
            spec = {}
            for role in ("fuel", "oxidizer"):
                try:
                    spec[role] = DN.propellant(self.need(c, role))
                except ValueError as ex:
                    self.err(c, str(ex))
            cool = self.need(c, "cooling")
            spec.update({
                "cycle": CYCLES.get(self.need(c, "cycle")), "pc_bar": (self.positive(c, "chamberPressure") or 0) / 1e5,
                "mixture_ratio": self.positive(c, "mixtureRatio"), "expansion_ratio": self.positive(c, "areaRatio"),
                "throat_diameter_m": self.positive(c, "throatDiameter"), "cooling": COOLING.get(cool),
                "film_cooling_fraction": self.need(c, "filmCoolingFraction"), "injector": INJECTOR.get(self.need(c, "injector")),
                "igniter": IGNITION.get(self.need(c, "ignition")), "gimbal_deg": self.need(c, "gimbalRange"),
            })
            if spec["igniter"] == "tea_teb":
                spec["igniter_units"] = int(self.need(c, "igniterUnits"))
            cm, nm = self.need(c, "chamberMaterial"), self.need(c, "nozzleMaterial")
            if cool in DN.CHAMBER_MATERIALS and cm is not None and cm not in DN.CHAMBER_MATERIALS[cool]:
                self.err(c, f"a {cool} chamber is made of {', '.join(sorted(DN.CHAMBER_MATERIALS[cool]))}, not {cm!r}")
            if nm is not None and nm not in DN.NOZZLE_MATERIALS:
                self.err(c, f"nozzle material {nm!r} is not one of {', '.join(sorted(DN.NOZZLE_MATERIALS))}")
        else:
            self.err(c, "an engine is a ProcuredEngine or a LiquidRocketEngine")
            return None
        ch = self.need(c, "chilldown")
        if ch is not None:
            spec["chilldown"] = ch
        if "pulsed" in c.children and self.need(c, "pulsed"):
            spec["pulsed"] = True
        return spec

    def feeds(self, e, d):
        """Every engine needs a feed line from the stage's fuel tank and from its oxidizer tank."""
        tanks = {t["name"]: t for t in d["tanks"]}
        fed = {}
        for c, sc in e.connections:
            if not (c.types and self.m.specializes(self.m.resolve(c.types[0], sc, c.line, c.path), "PropellantFeed")):
                self.errors.append(f"{c.path}:{c.line}: inside a stage, a connection is a PropellantFeed from a tank to an engine")
                continue
            a, b = c.ends
            if a not in tanks or b not in {x["part"] for x in d["engines"]}:
                self.errors.append(f"{c.path}:{c.line}: a PropellantFeed connects a tank of this stage to an engine of this stage, not {a} to {b}")
                continue
            fed.setdefault(b, set()).add(tanks[a]["role"])
        for x in d["engines"]:
            if fed.get(x["part"], set()) != {"fuel", "ox"}:
                self.err(e, f"engine part {x['part']} needs a PropellantFeed from the fuel tank and one from the oxidizer tank")


def walk(paths):
    """(description or None, errors, warnings) for a model in the given files, with the library."""
    w = Walker(load(paths))
    out = w.run()
    return (out if not w.errors else None), w.errors, w.warnings
