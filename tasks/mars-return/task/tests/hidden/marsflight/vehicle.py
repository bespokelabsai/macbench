"""Vehicle design: parse the team's design file, size every element, compute masses and check that
the design could be built.

The design lists elements from the bottom of the launch stack to the top. Masses are never declared:
they are computed from tanks, walls, engines, insulation, thermal protection, parachutes, power and
structure, so a design can't claim a dry mass it doesn't have.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

import numpy as np

from . import data as D
from .models.samples import manifest as SAMPLES
from .engine import Engine, build_engine
from . import vibro

KINDS = {"stage", "fairing", "heatshield", "backshell", "parachute", "capsule"}


def _f(spec, key, lo, hi, errors, where, default=None, required=True):
    if key not in spec:
        if default is not None or not required:
            return default
        errors.append(f"{where}: missing {key}")
        return lo
    v = spec[key]
    try:
        v = float(v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {key} must be a number")
        return lo
    if math.isnan(v) or not (lo <= v <= hi):
        errors.append(f"{where}: {key}={v} outside [{lo}, {hi}]")
        return lo if math.isnan(v) else min(max(v, lo), hi)
    return v


def dome_volume(R):
    """One 1:sqrt(2) ellipsoidal dome."""
    return (2.0 / 3.0) * math.pi * R ** 3 / math.sqrt(2.0)


def dome_area(R):
    if R <= 0:
        return 0.0   # a zero-volume tank (a stage of an engine with cycle "solid" and no mixture ratio) divided by zero
    a, c = R, R / math.sqrt(2.0)
    e = math.sqrt(1 - (c / a) ** 2)
    return math.pi * a * a * (1 + (1 - e * e) / e * math.atanh(e))


@dataclass
class Tank:
    propellant: str
    capacity_kg: float
    volume: float
    radius: float
    cyl_length: float
    length: float
    area: float
    wall_t: float
    mass: float
    n_cr: float = 0.0        # stiffened walls: axial load per metre of circumference the wall holds, without pressure
    limit: str = ""          # stiffened walls: the failure that sets n_cr


@dataclass
class Element:
    id: str
    kind: str
    spec: dict
    diameter: float = 0.0
    length: float = 0.0
    dry_mass: float = 0.0
    mass_breakdown: dict = field(default_factory=dict)
    # stage
    fuel: str | None = None
    ox: str | None = None
    engines: list = field(default_factory=list)      # [(Engine, count)]
    tanks: dict = field(default_factory=dict)        # role -> Tank
    capacity: dict = field(default_factory=dict)     # role -> kg
    loaded: dict = field(default_factory=dict)       # role -> kg at launch
    tank_pressure: float = 0.0
    material: str | None = None
    common_bulkhead: bool = False
    balloon: bool = False
    stiffeners: dict = field(default_factory=dict)   # stiffened walls, in metres; "smear" is their thickness spread over the wall
    design_accel: float = 0.0                        # m/s^2: the acceleration its tank walls are sized for
    intertank: tuple = ()                            # (mean radius, length) of the skirt between separate tanks
    mli_layers: int = 0
    rcs_prop: float = 0.0
    rcs_thrust: float = 0.0
    battery_Wh: float = 0.0
    solar_m2: float = 0.0
    rtg_units: int = 0
    fission_units: int = 0
    cryocooler_kW: float = 0.0
    isru_kW: float = 0.0
    heater_insulation_W_per_K_m2: float = 0.5
    legs_touchdown_speed: float = 0.0
    slew_rate: float = 5.0
    trn: bool = False              # carries terrain-relative navigation (data.TRN)
    differential_throttle: bool = False
    # aeroshell / capsule
    half_angle: float = 70.0
    nose_radius: float = 0.0
    tps: str | None = None
    tps_areal: float = 0.0
    ld_trim: float = 0.0
    ballast_kg: float = 0.0
    chute_type: str | None = None
    chute_diameter: float = 0.0
    chute_design_q: float = 0.0
    landing: str | None = None
    g_limit: float = 1e9
    sample_containers: int = 0     # capsule: Orbiting Sample containers it brings home (loaded on Mars)
    sample_mass: float = 0.0       # capsule: their mass, kg
    carrier: str | None = None     # parachutes: the backshell or capsule it rides on
    # vibration: the acoustic (dB) and shock (g) levels it is qualified to, and pogo accumulators
    qual_acoustic_dB: float = vibro.DEFAULT_QUAL_ACOUSTIC_DB
    qual_acoustic_set: bool = False
    qual_acoustic_default: float = vibro.DEFAULT_QUAL_ACOUSTIC_DB
    qual_shock_g: float = vibro.DEFAULT_QUAL_SHOCK_G
    pogo_accumulator_L: dict = field(default_factory=dict)
    vibro_payload: bool = False    # payload (vibro.payload_flags): held to its qualification levels

    @property
    def is_stage(self):
        return self.kind == "stage"


@dataclass
class Vehicle:
    elements: list
    engines: dict
    errors: list
    warnings: list

    def by_id(self, eid):
        for e in self.elements:
            if e.id == eid:
                return e
        return None

    @property
    def liftoff_mass(self):
        return sum(e.dry_mass + sum(e.loaded.values()) + e.rcs_prop for e in self.elements)

    def report(self) -> dict:
        rows = []
        for e in self.elements:
            r = {"id": e.id, "kind": e.kind, "diameter_m": round(e.diameter, 3), "length_m": round(e.length, 2),
                 "dry_mass_kg": round(e.dry_mass, 1), "breakdown_kg": {k: round(v, 1) for k, v in e.mass_breakdown.items()}}
            if e.kind == "capsule":
                r.update({"sample_containers": e.sample_containers, "sample_mass_kg": round(e.sample_mass, 2)})
            if e.is_stage:
                r.update({"propellants": f"{e.fuel}/{e.ox}" if e.fuel else None,
                          "loaded_kg": {k: round(v, 1) for k, v in e.loaded.items()},
                          "capacity_kg": {k: round(v, 1) for k, v in e.capacity.items()},
                          "engines": [{"engine": en.name, "count": n} for en, n in e.engines],
                          "rcs_prop_kg": e.rcs_prop, "material": e.material,
                          "common_bulkhead": e.common_bulkhead, "balloon_tanks": e.balloon,
                          "design_axial_accel_g": round(e.design_accel / D.G0, 2),
                          "factor_of_safety": D.PROOF_FACTOR if getattr(e, "qualified", False) else D.PROOF_FACTOR_UNTESTED})
                if e.stiffeners:
                    r["stiffened_walls"] = {role: {"smeared_wall_mm": round((tk.wall_t + e.stiffeners["smear"]) * 1e3, 2),
                                                   "axial_capacity_kN_per_m": round(tk.n_cr / 1e3, 1), "limited_by": tk.limit}
                                            for role, tk in e.tanks.items()}
            if e.vibro_payload:
                r["vibration_qualification"] = {"acoustic_dB": round(e.qual_acoustic_dB, 1), "shock_g": round(e.qual_shock_g, 0)}
            if e.pogo_accumulator_L:
                r["pogo_accumulator_L_per_engine"] = e.pogo_accumulator_L
            r["geometry"] = _geometry(e)
            rows.append(r)
        # Ideal vacuum delta-v if stages fire bottom-up, each carrying everything above it.
        dv = []
        above = 0.0
        stack = []
        for e in reversed(self.elements):
            m_wet = e.dry_mass + sum(e.loaded.values()) + e.rcs_prop
            stack.append((e, m_wet, above))
            above += m_wet
        for e, m_wet, m_above in reversed(stack):
            if e.is_stage and e.engines and sum(e.loaded.values()) > 0:
                F = sum(en.thrust_vac * n for en, n in e.engines)
                mdot = sum(en.mdot * n for en, n in e.engines)
                isp = F / (mdot * D.G0)
                m0 = m_wet + m_above
                m1 = m0 - sum(e.loaded.values())
                dv.append({"stage": e.id, "isp_vac_s": round(isp, 1), "dv_vac_mps": round(isp * D.G0 * math.log(m0 / m1), 1),
                           "initial_mass_kg": round(m0, 1), "thrust_vac_kN": round(F / 1e3, 1),
                           "initial_twr_earth": round(F / (m0 * D.G0), 3)})
        out = {"valid": not self.errors, "errors": self.errors, "warnings": self.warnings,
               "liftoff_mass_kg": round(self.liftoff_mass, 1), "elements": rows,
               "ideal_stage_dv_if_fired_bottom_up": dv,
               "engines": {k: v.summary() for k, v in self.engines.items()}}
        if self.elements and self.elements[0].engines:
            # Vibration (vibro): the liftoff noise each payload element hears in the fairing. The pogo margins are
            # left out: they come from the hidden stage and engine model (vibro.pogo_sweep computes them).
            out["vibration"] = {"liftoff_oaspl_dB": {k: round(v, 1) for k, v in vibro.liftoff_acoustics(self.elements)["levels"].items()}}
        if not self.errors:
            # The stack's mass properties and center of pressure at liftoff (they matter in 6-DOF; sixdof.py).
            try:
                from .sixdof import stack_report
                out["mass_properties_at_liftoff"] = stack_report(self)
            except Exception:  # noqa: BLE001
                pass
        return out


def _geometry(e) -> dict:
    """Shapes for drawing an element to scale: tanks (bottom first), engine nozzles, aeroshell cone."""
    g = {"diameter_m": round(e.diameter, 3), "length_m": round(e.length, 3)}
    if e.is_stage:
        g["tanks"] = [{"role": role, "propellant": tk.propellant, "radius_m": round(tk.radius, 3),
                       "cylinder_m": round(tk.cyl_length, 3), "length_m": round(tk.length, 3),
                       "wall_mm": round(tk.wall_t * 1e3, 2)} for role, tk in e.tanks.items()]
        g["common_bulkhead"] = e.common_bulkhead
        g["engines"] = []
        for en, n in e.engines:
            De, Dt = math.sqrt(4 * en.Ae / math.pi), math.sqrt(4 * en.At / math.pi)
            g["engines"].append({"engine": en.name, "count": n, "exit_diameter_m": round(De, 3),
                                 "throat_diameter_m": round(Dt, 3), "gimbal_deg": en.gimbal_deg,
                                 "nozzle_length_m": round(0.8 * (De - Dt) / 2 / math.tan(math.radians(15)) + 2.0 * Dt, 3)})
        g["legs"] = bool(e.legs_touchdown_speed)
        if e.differential_throttle:
            g["differential_throttle"] = True
    if e.kind in ("heatshield", "capsule"):
        g.update({"half_angle_deg": e.half_angle, "nose_radius_m": round(e.nose_radius, 3), "tps": e.tps})
    if e.kind == "parachute":
        g.update({"type": e.chute_type, "canopy_diameter_m": e.chute_diameter})
    if e.solar_m2:
        g["solar_array_m2"] = e.solar_m2
    return g


def _tank(prop, cap_kg, R, t, mat, ullage):
    rho = D.PROPELLANTS[prop]["rho"]
    V = cap_kg / rho * (1 + ullage)
    vd = 2 * dome_volume(R)
    if V <= vd:
        # small tank: two domes on a shorter radius
        r = (V / (2 * (2.0 / 3.0) * math.pi / math.sqrt(2.0))) ** (1 / 3)
        cyl = 0.0
        area = 2 * dome_area(r)
        length = 2 * r / math.sqrt(2.0)
        Rr = r
    else:
        cyl = (V - vd) / (math.pi * R * R)
        area = 2 * dome_area(R) + 2 * math.pi * R * cyl
        length = cyl + 2 * R / math.sqrt(2.0)
        Rr = R
    m = area * t * D.MATERIALS[mat]["rho"] * D.MATERIALS[mat].get("liner_factor", 1.0)
    return Tank(prop, cap_kg, V, Rr, cyl, length, area, t, m)


def _common_tanks(props, caps, R, walls, mat, ullage):
    """Both tanks in one shell, split by a common bulkhead: a dome inside the shell that adds no volume.
    The shell is one tank holding both volumes. Each tank gets one outer dome and the share of the
    cylinder that its volume takes. Returns the two tanks and the bulkhead's radius."""
    vols = {role: caps[role] / D.PROPELLANTS[props[role]]["rho"] * (1 + ullage) for role in props}
    V = sum(vols.values())
    vd = 2 * dome_volume(R)
    if V <= vd:
        r = (V / (2 * (2.0 / 3.0) * math.pi / math.sqrt(2.0))) ** (1 / 3)
        cyl = 0.0
    else:
        r = R
        cyl = (V - vd) / (math.pi * R * R)
    f = D.MATERIALS[mat]["rho"] * D.MATERIALS[mat].get("liner_factor", 1.0)
    tanks = {}
    for role in props:
        c = cyl * vols[role] / V
        area = dome_area(r) + 2 * math.pi * r * c
        tanks[role] = Tank(props[role], caps[role], vols[role], r, c, c + r / math.sqrt(2.0), area, walls[role],
                           area * walls[role] * f)
    return tanks, r


def _head_reinforcement(e, a, sf):
    """Wall mass (kg per tank) that stage e needs beyond its design walls to hold the tank pressure plus the
    head of its full propellant load at axial acceleration a (m/s^2), with factor of safety sf. At depth z
    below the propellant surface the cylinder needs sf (p + rho a z) r / yield, so where that passes the
    design wall the extra thickness grows linearly, as on a tapered tank wall. The bottom dome carries the
    full head at 0.707 of the cylinder's hoop stress (the crown of a 1:sqrt(2) ellipsoid)."""
    mat = D.MATERIALS[e.material]
    f = mat["rho"] * mat.get("liner_factor", 1.0)
    out = {}
    for role, tk in e.tanks.items():
        rho = D.PROPELLANTS[tk.propellant]["rho"]
        r, t, p = tk.radius, tk.wall_t, e.tank_pressure
        if r <= 0:
            continue   # an empty tank (engine cycle "solid", built with mixture ratio 0) divided by zero below
        vd, hd = dome_volume(r), r / math.sqrt(2.0)
        v = e.loaded.get(role, 0.0) / rho
        hc = min(tk.cyl_length, max(0.0, v - vd) / (math.pi * r * r))     # propellant column in the cylinder
        h = min(tk.length, hd * min(1.0, v / vd) + max(0.0, v - vd) / (math.pi * r * r))   # whole column
        k = sf * r / mat["sy"]                                              # wall thickness per Pa
        extra = 0.0
        if a > 0 and hc > 0:
            z0 = max(0.0, (t / k - p) / (rho * a))                          # depth where the design wall is enough
            if hc > z0:
                extra += k * rho * a * (hc - z0) ** 2 / 2 * 2 * math.pi * r * f
        t_dome = 0.707 * k * (p + rho * a * h)
        if t_dome > t:
            extra += (t_dome - t) * dome_area(r) * f
        out[role] = extra
    return out


def _size_intertank(e, above):
    """The intertank of stage e: an unpressurized skirt that carries, at the stage's design acceleration, the
    upper tank's structure and full propellant load and everything above the stage, with the buckling factor
    of safety. Sized as in NASA Langley's S-IC intertank estimate (Launch Vehicle Loads and Sizing, NTRS
    20240002646, pp. 10 and 11): a cylinder at the classical buckling stress of NASA SP-8007,
    0.605 E t / R, with no knockdown, since flown intertanks are corrugated or stiffened skirts. At least
    the minimum gauge, and no thinner than yield allows."""
    R, L = e.intertank[:2]
    upper = e.tanks["ox"]
    m_above = (e.loaded.get("ox", 0.0) + upper.mass + sum(x.dry_mass + sum(x.loaded.values()) + x.rcs_prop for x in above
                                                         if x.kind != "fairing"))
    sf = D.BUCKLING_SF if getattr(e, "qualified", False) else D.BUCKLING_SF_UNTESTED
    mat = D.MATERIALS[e.material]
    n = sf * m_above * max(e.design_accel, D.G0) / (2 * math.pi * R)      # N per metre of circumference
    t = max(D.MIN_GAUGE_M, math.sqrt(n * R / (0.605 * mat["E"])), n / mat["sy"])
    e.intertank = (R, L, t)
    e.mass_breakdown["intertank"] = 2 * math.pi * R * L * t * mat["rho"] * mat.get("liner_factor", 1.0)
    e.dry_mass = sum(e.mass_breakdown.values())


def _section(h, w, flange):
    """A stiffener standing on the skin: a blade of height h and thickness w, or with flange > 0 a T topped by a
    flange that wide and w thick (h is then the height to the top of the flange). Returns its area, inertia
    about its centroid, centroid height above the skin, and torsion constant."""
    hw = h - w if flange > 0 else h
    parts = [(w, hw, hw / 2)] + ([(flange, w, hw + w / 2)] if flange > 0 else [])
    A = sum(b * t for b, t, _ in parts)
    zc = sum(b * t * z for b, t, z in parts) / A
    I = sum(b * t ** 3 / 12 + b * t * (z - zc) ** 2 for b, t, z in parts)
    J = sum(max(b, t) * min(b, t) ** 3 / 3 for b, t, _ in parts)
    return A, I, zc, J


def shell_buckling_load(r, length, t, E, stringers=None, rings=None):
    """Axial buckling load per metre of circumference (N/m) of a cylinder with skin t, from NASA SP-8007
    eq. 37 to 43, at the m >= 1 axial half waves and n >= 4 circumferential waves that give the least.
    stringers and rings are (area, inertia, centroid height, torsion constant, spacing) of stiffeners on the
    inside of the skin, smeared as in its eq. 69 to 78 about the middle of the skin. No knockdown factor."""
    nu = D.POISSON
    c, G = E / (1 - nu * nu), E / (2 * (1 + nu))
    Ex = Ey = c * t
    Exy, Gxy = nu * c * t, G * t
    Dx = Dy = c * t ** 3 / 12
    Dxy = 2 * Dx                   # eq. 62 for one isotropic layer
    Cx = Cy = 0.0                  # Cxy = Kxy = 0 (eq. 78)
    if stringers:
        A, I, zc, J, b = stringers
        z = -(t / 2 + zc)          # SP-8007 measures z outward from the axis, so a stiffener inside the skin has z < 0
        Ex, Dx, Cx, Dxy = Ex + E * A / b, Dx + E * (I + z * z * A) / b, z * E * A / b, Dxy + G * J / b
    if rings:
        A, I, zc, J, d = rings
        z = -(t / 2 + zc)
        Ey, Dy, Cy, Dxy = Ey + E * A / d, Dy + E * (I + z * z * A) / d, z * E * A / d, Dxy + G * J / d
    m = np.unique(np.round(np.geomspace(1, 3000, 160)))
    n = np.unique(np.round(np.geomspace(4, 3000, 160)))
    for _ in range(2):             # a coarse grid, then every integer pair near its least value
        a = (m * math.pi / length)[:, None]
        k = (n / r)[None, :]
        A11 = Ex * a ** 2 + Gxy * k ** 2
        A22 = Ey * k ** 2 + Gxy * a ** 2
        A33 = Dx * a ** 4 + Dxy * a ** 2 * k ** 2 + Dy * k ** 4 + Ey / r ** 2 + 2 * Cy / r * k ** 2
        A12 = (Exy + Gxy) * a * k
        A23 = Ey / r * k + Cy * k ** 3
        A13 = Exy / r * a + Cx * a ** 3
        det3 = A11 * (A22 * A33 - A23 ** 2) - A12 * (A12 * A33 - A23 * A13) + A13 * (A12 * A23 - A22 * A13)
        N = det3 / (A11 * A22 - A12 ** 2) / a ** 2
        i, j = np.unravel_index(np.argmin(N), N.shape)
        best = float(N[i, j])
        m = np.arange(max(1.0, m[i] - 25), m[i] + 26)
        n = np.arange(max(4.0, n[j] - 25), n[j] + 26)
    return best


def _stiffened_capacity(tk, st, mat):
    """Axial load per metre of circumference (N/m) that a stiffened tank cylinder holds without pressure, and the
    failure that sets it: SP-8007 eq. 37 for the whole wall and for the bay between two rings (stringers only),
    times 0.75; plate buckling of the skin between stiffeners and of the stiffeners; and yield. Skin and
    stringers share one strain, so a stress limit becomes a load through their combined thickness."""
    E, t, r = mat["E"], tk.wall_t, tk.radius
    sA, sI, sz, sJ = _section(st["h"], st["w"], st["f"])
    rings = _section(st["hr"], st["wr"], 0.0) + (st["d"],)
    tx = t + sA / st["b"]
    plate = math.pi ** 2 * E / (12 * (1 - D.POISSON ** 2))
    a, b = st["d"] - st["wr"], st["b"] - st["w"]                       # skin panel between stiffeners
    k_skin = min((j * b / a + a / (j * b)) ** 2 for j in range(1, 60))
    if st["f"] > 0:
        k_web = 4.0 if st["f"] >= 0.3 * st["h"] else 0.43             # a flange this wide holds the web's top edge
        s_rib = plate * min(k_web * (st["w"] / (st["h"] - st["w"])) ** 2, 0.43 * (2 * st["w"] / (st["f"] - st["w"])) ** 2)
    else:
        s_rib = plate * 0.43 * (st["w"] / st["h"]) ** 2
    modes = {"whole wall": D.STIFFENED_KNOCKDOWN * shell_buckling_load(r, max(tk.cyl_length, 2 * r), t, E, (sA, sI, sz, sJ, st["b"]), rings),
             "bay between rings": D.STIFFENED_KNOCKDOWN * shell_buckling_load(r, st["d"], t, E, (sA, sI, sz, sJ, st["b"])),
             "skin between stiffeners": plate * k_skin * (t / b) ** 2 * tx,
             "stiffeners": s_rib * tx,
             "yield": mat["sy"] * tx}
    limit = min(modes, key=modes.get)
    return modes[limit], limit


def build_vehicle(design: dict, qualified=None) -> Vehicle:
    """qualified: ids of stages whose tank design passed a structural qualification test. Those may use
    the tested factor of safety (1.25 on yield at proof); every other tank needs the no-test factor (1.6)."""
    errors, warnings = [], []
    qualified = set(qualified or ())
    engines = {}
    for name, spec in (design.get("engines") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        cat = spec.get("catalog")
        if cat is not None:
            from .economics import CATALOG_ENGINES, build_catalog_engine
            if cat not in CATALOG_ENGINES:
                errors.append(f"engine {name}: no catalog engine {cat!r}; choose from {sorted(CATALOG_ENGINES)}")
                continue
            en = build_catalog_engine(name, cat, spec.get("chilldown"))
        else:
            en = build_engine(name, spec)
            en.catalog = None
            from .models.structures import engine_mass as EMF
            if EMF.fit_enabled() and not en.errors:
                en.mass = EMF.engine_mass_kg(en)   # fitted to flown engines; off by default
        pulsed = spec.get("pulsed", False)
        if not isinstance(pulsed, bool):
            errors.append(f"engine {name}: pulsed must be true or false")
        elif pulsed and cat is not None:   # a catalog engine's spec is the catalog's; carry the team's choice on it
            en.spec = dict(en.spec or {}, pulsed=True)
        engines[name] = en
        for m in en.errors:
            errors.append(f"engine {name}: {m}")
        for m in en.warnings:
            warnings.append(f"engine {name}: {m}")
    els_spec = design.get("elements")
    if not isinstance(els_spec, list) or not els_spec:
        errors.append("design needs a non-empty 'elements' list, bottom of the launch stack first")
        return Vehicle([], engines, errors, warnings)
    _check_launch_site(design, errors)
    elements = []
    seen = set()
    for i, s in enumerate(els_spec):
        if not isinstance(s, dict):
            errors.append(f"elements[{i}] must be an object")
            continue
        eid = s.get("id")
        if not isinstance(eid, str) or not eid.strip():
            # Hazards, tests and the hidden truth all key a part by its id, so every element needs its own.
            errors.append(f"elements[{i}]: needs an 'id', a non-empty string unique in the design")
            eid = f"elements[{i}]"
        where = f"element {eid}"
        if eid in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(eid)
        kind = s.get("kind")
        if kind not in KINDS:
            errors.append(f"{where}: kind {kind!r} not one of {sorted(KINDS)}")
            continue
        e = Element(eid, kind, s)
        bd = e.mass_breakdown
        if kind == "stage":
            _build_stage(e, s, engines, errors, warnings, where, eid in qualified)
        elif kind == "fairing":
            e.diameter = _f(s, "diameter_m", 0.5, 20, errors, where)
            e.length = _f(s, "length_m", 0.5, 60, errors, where)
            area = math.pi * e.diameter * e.length + math.pi * (e.diameter / 2) ** 2
            bd["fairing_shell"] = area * D.FAIRING_KG_PER_M2
        elif kind in ("heatshield", "capsule"):
            e.diameter = _f(s, "diameter_m", 0.3, 20, errors, where)
            e.half_angle = _f(s, "half_angle_deg", 45, 70, errors, where)
            e.nose_radius = _f(s, "nose_radius_m", 0.05 * e.diameter, 0.6 * e.diameter, errors, where, default=0.25 * e.diameter)
            e.tps = s.get("tps")
            if e.tps not in D.TPS:
                errors.append(f"{where}: tps {e.tps!r} not one of {sorted(D.TPS)}")
                e.tps = "PICA"
            e.tps_areal = _f(s, "areal_mass_kg_m2", 0.5, 200, errors, where)
            ld_max = _ld_max(e.half_angle)
            e.ld_trim = _f(s, "ld_trim", 0.0, ld_max, errors, where, default=0.0)
            Rb = e.diameter / 2
            # forebody area of a sphere-cone ~ base area / sin(half angle)
            fore = math.pi * Rb * Rb / math.sin(math.radians(e.half_angle))
            bd["tps_forebody"] = fore * e.tps_areal
            bd["aeroshell_structure"] = fore * 6.0
            e.length = 0.35 * e.diameter
            if kind == "capsule":
                e.landing = s.get("landing", "parachute")
                if e.landing not in ("parachute", "crushable"):
                    errors.append(f"{where}: landing must be parachute or crushable")
                e.g_limit = _f(s, "g_limit", 5, 150, errors, where, default=60)
                bd["structure_and_containment"] = _f(s, "structure_mass_kg", 10, 5000, errors, where, default=40.0)
                n = _f(s, "sample_containers", 1, D.SAMPLE_CONTAINERS_MAX, errors, where, default=1)
                if n != int(n):
                    errors.append(f"{where}: sample_containers must be a whole number")
                e.sample_containers = int(n)
                e.sample_mass = e.sample_containers * SAMPLES.payload_mass()
                if e.sample_containers > 1:
                    bd["containment_of_more_containers"] = (D.SAMPLE_CONTAINER_KG_PER_KG * (e.sample_containers - 1)
                                                            * SAMPLES.payload_mass())
                bd["backshell_tps"] = 0.5 * fore * e.tps_areal * 0.3
                if e.landing == "crushable":
                    bd["crushable_impact_absorber"] = 0.15 * (bd["structure_and_containment"]
                                                              + bd.get("containment_of_more_containers", 0.0) + bd["tps_forebody"])
                if e.ld_trim > 0:
                    e.ballast_kg = 0.0   # capsules trim by CG placement inside the structure mass budget
                    bd["trim_ballast"] = D.BALLAST_FRACTION_PER_LD * e.ld_trim * (bd["structure_and_containment"]
                                         + bd.get("containment_of_more_containers", 0.0) + bd["tps_forebody"])
                e.length = 0.7 * e.diameter
            else:
                e.g_limit = 1e9
        elif kind == "backshell":
            e.tps = s.get("tps", "SLA561V")
            if e.tps not in D.TPS:
                errors.append(f"{where}: tps {e.tps!r} not one of {sorted(D.TPS)}")
                e.tps = "SLA561V"
            e.tps_areal = _f(s, "areal_mass_kg_m2", 0.5, 100, errors, where)
        elif kind == "parachute":
            e.chute_type = s.get("type", "dgb")
            if e.chute_type not in D.PARACHUTES:
                errors.append(f"{where}: type {e.chute_type!r} not one of {sorted(D.PARACHUTES)}")
                e.chute_type = "dgb"
            e.chute_diameter = _f(s, "diameter_m", 1, 60, errors, where)
            e.chute_design_q = _f(s, "design_q_Pa", 50, 10000, errors, where)
            if os.environ.get("MARSFLIGHT_CANOPY_MASS", "1") == "1":
                # fabric over the nominal area plus strength members for the rated load, fitted to the
                # Apollo main and the MSL DGB (Orion main within 15%, not fitted). Off until the reference's S2
                # resize merges: the reference DGB goes from 372 to 442 kg.
                from .models.parachutes.chutes import canopy_mass_kg
                bd["canopy_and_mortar"] = canopy_mass_kg(e.chute_type, e.chute_diameter, e.chute_design_q)
            else:
                bd["canopy_and_mortar"] = D.PARACHUTE_MASS_K * e.chute_diameter ** 2 * e.chute_design_q + D.PARACHUTE_BASE_KG
        _common_systems(e, s, errors, where)
        vibro.qual_fields(e, s, _f, errors, where)
        if e.is_stage and e.pogo_accumulator_L:
            bd["pogo_accumulators"] = vibro.accumulator_mass(e)
        e.vibro_payload = vibro.payload_flags([x.kind for x in elements] + [kind])[-1]
        e.dry_mass = sum(bd.values())
        elements.append(e)

    _check_stack(elements, errors, warnings)
    for e in elements:
        _check_rcs(e, errors, f"element {e.id}")
    return Vehicle(elements, engines, errors, warnings)


def _ld_max(half_angle):
    keys = sorted(D.CAPSULE_LD_MAX)
    for a, b in zip(keys, keys[1:]):
        if a <= half_angle <= b:
            return D.CAPSULE_LD_MAX[a] + (D.CAPSULE_LD_MAX[b] - D.CAPSULE_LD_MAX[a]) * (half_angle - a) / (b - a)
    return D.CAPSULE_LD_MAX[keys[-1]]


def _common_systems(e, s, errors, where):
    """Power, thermal, RCS, ISRU and extra mass can sit on any element."""
    bd = e.mass_breakdown
    e.battery_Wh = 1000 * _f(s, "battery_kWh", 0, 5000, errors, where, default=0.0)
    e.solar_m2 = _f(s, "solar_array_m2", 0, 5000, errors, where, default=0.0)
    e.rtg_units = int(_f(s, "rtg_units", 0, D.RTG["max_units"], errors, where, default=0))
    e.fission_units = int(_f(s, "fission_units", 0, 20, errors, where, default=0))
    e.cryocooler_kW = _f(s, "cryocooler_kW", 0, 1000, errors, where, default=0.0)
    e.isru_kW = _f(s, "isru_kW", 0, 1000, errors, where, default=0.0)
    e.rcs_prop = _f(s, "rcs_prop_kg", 0, 1e5, errors, where, default=0.0)
    e.rcs_thrust = _f(s, "rcs_thrust_N", 0, D.RCS_THRUST_MAX_N, errors, where, default=0.0)
    e.heater_insulation_W_per_K_m2 = _f(s, "surface_insulation_W_per_K_m2", 0.05, 10, errors, where, default=0.5)
    if e.tanks and e.heater_insulation_W_per_K_m2 < 0.5:
        # Insulation better than the default 0.5 W/K/m^2 costs 0.25 kg/m^2 of tank area per halving step:
        # areal mass grows as 0.25 x (0.5 / U - 1) kg/m^2 (aerogel and foam blankets).
        area = sum(tk.area for tk in e.tanks.values())
        bd["surface_insulation"] = area * 0.25 * (0.5 / e.heater_insulation_W_per_K_m2 - 1.0)
    if e.battery_Wh:
        bd["battery"] = e.battery_Wh / D.BATTERY_WH_PER_KG
    if e.solar_m2:
        bd["solar_array"] = e.solar_m2 * D.SOLAR_KG_PER_M2
    if e.rtg_units:
        bd["rtg"] = e.rtg_units * D.RTG["mass_kg"]
    if e.fission_units:
        bd["fission_power"] = e.fission_units * D.FISSION["mass_kg"]
    if e.cryocooler_kW:
        bd["cryocoolers"] = e.cryocooler_kW * D.CRYOCOOLER_KG_PER_KW
    if e.isru_kW:
        bd["isru_plant"] = e.isru_kW * D.ISRU_KG_PER_KW
    if e.rcs_prop or e.rcs_thrust:
        bd["rcs_tanks_and_thrusters"] = 0.12 * e.rcs_prop + 0.01 * e.rcs_thrust + 5.0
    extra = _f(s, "extra_mass_kg", 0, 1e7, errors, where, default=0.0)
    if extra:
        bd["extra"] = extra
    e.trn = bool(s.get("trn", False))
    if e.trn:
        bd["terrain_relative_navigation"] = D.TRN["mass_kg"]
    if e.solar_m2 or e.rtg_units or e.fission_units or e.kind in ("stage", "capsule"):
        if e.kind != "fairing":
            bd["avionics"] = D.AVIONICS_PER_ELEMENT_KG
        if e.kind == "capsule":
            bd["avionics"] = min(D.AVIONICS_PER_ELEMENT_KG, D.CAPSULE_AVIONICS_KG_PER_M2 * e.diameter ** 2)
    e.slew_rate = _f(s, "slew_rate_deg_s", 0.5, 30, errors, where, default=5.0)
    dt = s.get("differential_throttle", False)
    if not isinstance(dt, bool):
        errors.append(f"{where}: differential_throttle must be true or false")
    e.differential_throttle = dt is True


def _check_rcs(e, errors, where):
    """RCS is for attitude and small burns: its propellant is capped by the element's own mass (data.py)."""
    if not e.rcs_prop:
        return
    own = e.dry_mass - e.mass_breakdown.get("rcs_tanks_and_thrusters", 0.0) + sum(e.loaded.values())
    cap = max(D.RCS_PROP_MIN_KG, D.RCS_PROP_MAX_FRACTION * own)
    if e.rcs_prop > cap + 1e-6:
        errors.append(f"{where}: rcs_prop_kg={e.rcs_prop:.0f} is more than an RCS carries: at most "
                      f"{D.RCS_PROP_MAX_FRACTION:.0%} of the element's {own:.0f} kg without it, or "
                      f"{D.RCS_PROP_MIN_KG:.0f} kg (use a stage with an engine for main propulsion)")


def _size_limits(elements, errors):
    """The largest hardware the program builds (data.MAX_*): past these sizes nothing has flown or been tested,
    and the models would only extrapolate."""
    for e in elements:
        if e.kind in ("stage", "fairing") and e.diameter > D.MAX_LAUNCH_DIAMETER_M + 1e-9:
            errors.append(f"element {e.id}: a {e.kind} {e.diameter:g} m across is wider than the program builds "
                          f"({D.MAX_LAUNCH_DIAMETER_M:g} m)")
        if e.kind in ("heatshield", "capsule") and e.diameter > D.MAX_SHELL_DIAMETER_M + 1e-9:
            errors.append(f"element {e.id}: a rigid {e.kind} {e.diameter:g} m across is wider than the program builds "
                          f"({D.MAX_SHELL_DIAMETER_M:g} m)")
        n = sum(k for _, k in e.engines)
        if n > D.MAX_ENGINES_PER_STAGE:
            errors.append(f"element {e.id}: {n} engines on one stage, more than the program integrates "
                          f"({D.MAX_ENGINES_PER_STAGE})")
    dgb = [e.id for e in elements if e.kind == "parachute" and e.chute_type == "dgb"]
    if len(dgb) > D.MAX_SUPERSONIC_CHUTES:
        errors.append(f"{len(dgb)} disk gap band parachutes ({', '.join(dgb)}): the program qualifies at most "
                      f"{D.MAX_SUPERSONIC_CHUTES} supersonic canopy per vehicle (no supersonic cluster has flown)")


def _build_stage(e, s, engines, errors, warnings, where, qualified=False):
    bd = e.mass_breakdown
    e.qualified = qualified
    proof = D.PROOF_FACTOR if qualified else D.PROOF_FACTOR_UNTESTED
    e.diameter = _f(s, "diameter_m", 0.3, 20, errors, where)
    R = e.diameter / 2
    e.material = s.get("material", "Al2219")
    if e.material not in D.MATERIALS:
        errors.append(f"{where}: material {e.material!r} not one of {sorted(D.MATERIALS)}")
        e.material = "Al2219"
    e.tank_pressure = 1e5 * _f(s, "tank_pressure_bar", 1.0, 400.0, errors, where, default=3.0)
    ullage = _f(s, "ullage_fraction", 0.02, 0.5, errors, where, default=0.03)
    e.mli_layers = int(_f(s, "mli_layers", 0, 200, errors, where, default=0))
    sofi = bool(s.get("sofi", False))
    e.common_bulkhead = bool(s.get("common_bulkhead", False))
    e.balloon = bool(s.get("balloon_tanks", False))
    st = s.get("stiffeners")
    if st and not isinstance(st, dict):
        errors.append(f"{where}: stiffeners must be an object with the stringer and ring sizes")
        st = None
    if st:
        w = f"{where} stiffeners"
        h = _f(st, "stringer_height_mm", 5.0, 500.0, errors, w)
        th = _f(st, "stringer_thickness_mm", D.MIN_GAUGE_M * 1e3, 50.0, errors, w)
        x = e.stiffeners = {"h": h / 1e3, "w": th / 1e3,
                            "b": _f(st, "stringer_spacing_mm", 20.0, 2000.0, errors, w) / 1e3,
                            "f": _f(st, "stringer_flange_mm", 0.0, 300.0, errors, w, default=0.0) / 1e3,
                            "d": _f(st, "ring_spacing_mm", 20.0, 10000.0, errors, w) / 1e3,
                            "hr": _f(st, "ring_height_mm", 5.0, 500.0, errors, w, default=h) / 1e3,
                            "wr": _f(st, "ring_thickness_mm", D.MIN_GAUGE_M * 1e3, 50.0, errors, w, default=th) / 1e3}
        if e.balloon:
            errors.append(f"{where}: balloon tanks take no compression in the skin, so they cannot have stiffeners")
        if x["b"] <= max(2 * x["w"], x["f"]) or x["d"] <= 2 * x["wr"]:
            errors.append(f"{where}: stiffeners overlap. Each spacing must be more than twice the thickness, and the "
                          "stringer spacing more than the flange")
        if x["f"] > 0 and (x["f"] <= x["w"] or x["h"] <= 2 * x["w"]):
            errors.append(f"{where}: a stringer flange must be wider than the stringer is thick, on a stringer more "
                          "than twice as high as it is thick")
            x["f"] = 0.0
        x["smear"] = _section(x["h"], x["w"], x["f"])[0] / x["b"] + _section(x["hr"], x["wr"], 0.0)[0] / x["d"]
    for item in s.get("engines", []) or []:
        name = item.get("engine") if isinstance(item, dict) else None
        try:
            n = int(item.get("count", 1)) if isinstance(item, dict) else 0
        except (TypeError, ValueError):   # "count": "twelve" crashed the build
            errors.append(f"{where}: engine count {item.get('count')!r} must be a whole number")
            continue
        if name not in engines:
            errors.append(f"{where}: engine {name!r} is not defined in 'engines'")
            continue
        if not (1 <= n <= 60):
            errors.append(f"{where}: engine count {n} outside [1, 60]")
            continue
        e.engines.append((engines[name], n))
    e.fuel, e.ox = s.get("fuel"), s.get("oxidizer")
    prop_total = _f(s, "propellant_kg", 0, 2e7, errors, where, default=0.0)
    capacity_total = _f(s, "tank_capacity_kg", 0, 2e7, errors, where, default=prop_total)
    ox_loaded_frac = _f(s, "oxidizer_loaded_fraction", 0.0, 1.0, errors, where, default=1.0)
    if capacity_total < prop_total:
        errors.append(f"{where}: tank_capacity_kg is less than propellant_kg")
        capacity_total = prop_total
    if capacity_total > 0:
        if (e.fuel, e.ox) not in D.PAIRS:
            errors.append(f"{where}: fuel/oxidizer {e.fuel}/{e.ox} not a supported pair")
            e.fuel, e.ox = None, None
        else:
            ofs = {round(en.of, 4) for en, _ in e.engines}
            for en, _ in e.engines:
                if (en.fuel, en.ox) != (e.fuel, e.ox):
                    errors.append(f"{where}: engine {en.name} burns {en.fuel}/{en.ox}, but the stage holds {e.fuel}/{e.ox}")
            if len(ofs) > 1:
                errors.append(f"{where}: all engines on one stage must share a mixture ratio")
            of = next(iter(ofs)) if ofs else float(s.get("mixture_ratio", 3.0))
            if "loaded_mixture_ratio" in s:
                # the oxidizer-to-fuel mass ratio of the load and of the tanks. It differs from the engines'
                # design ratio when the flight software plans to shift the mixture ratio during the burn.
                of = _f(s, "loaded_mixture_ratio", 0.5, 9.0, errors, where, default=of)
            cap_f = capacity_total / (1 + of)
            cap_o = capacity_total - cap_f
            e.capacity = {"fuel": cap_f, "ox": cap_o}
            load_f = prop_total / (1 + of)
            load_o = (prop_total - load_f) * ox_loaded_frac
            e.loaded = {"fuel": load_f, "ox": load_o}
            mat = D.MATERIALS[e.material]
            if not mat["lox_ok"] and e.ox == "LOX":
                errors.append(f"{where}: {e.material} is not compatible with LOX (impact ignition)")
            walls = s.get("wall_thickness_mm") or {}
            if not isinstance(walls, dict):   # "wall_thickness_mm": 4.0 crashed the build
                errors.append(f"{where}: wall_thickness_mm must be an object {{\"fuel\": mm, \"ox\": mm}}")
                walls = {}
            # Balloon tanks may be thinner than the usual minimum gauge, because pressure carries their loads.
            gauge = D.BALLOON_MIN_GAUGE_M if e.balloon else D.MIN_GAUGE_M
            if e.balloon and e.material not in D.BALLOON_MATERIALS:
                errors.append(f"{where}: balloon tanks must be {' or '.join(D.BALLOON_MATERIALS)}, the thin cold-rolled "
                              "stainless steel of Atlas and Centaur")
            props, caps, ts = {"fuel": e.fuel, "ox": e.ox}, {"fuel": cap_f, "ox": cap_o}, {}
            for role in props:
                t = _f(walls, role, gauge * 1e3, 100.0, errors, f"{where} wall_thickness_mm") / 1e3 if role in walls else None
                if t is None:
                    errors.append(f"{where}: wall_thickness_mm.{role} missing")
                    t = gauge
                ts[role] = t
            if e.common_bulkhead:
                e.tanks, r_cb = _common_tanks(props, caps, R, ts, e.material, ullage)
            else:
                e.tanks = {role: _tank(props[role], caps[role], R, ts[role], e.material, ullage) for role in props}
            for role, tk in e.tanks.items():
                prop, t = tk.propellant, tk.wall_t
                hoop = e.tank_pressure * tk.radius / t
                allow = mat["sy"] / proof
                if hoop > allow:
                    errors.append(f"{where}: {role} tank wall {t * 1e3:.2f} mm yields at proof pressure "
                                  f"(hoop stress {hoop / 1e6:.0f} MPa > {allow / 1e6:.0f} MPa allowed with the "
                                  f"{'tested' if qualified else 'untested'} factor of safety {proof})")
                bd[f"{role}_tank"] = tk.mass
                if e.stiffeners:
                    bd[f"{role}_tank_stiffeners"] = 2 * math.pi * tk.radius * tk.cyl_length * e.stiffeners["smear"] * mat["rho"]
                    tk.n_cr, tk.limit = _stiffened_capacity(tk, e.stiffeners, mat)
                if prop == "LH2":
                    bd[f"{role}_tank_insulation"] = tk.area * D.LH2_INSULATION_KG_PER_M2
                elif sofi and D.PROPELLANTS[prop]["cryo"]:
                    bd[f"{role}_tank_foam"] = tk.area * D.SOFI_KG_PER_M2
                if e.mli_layers:
                    bd[f"{role}_tank_mli"] = tk.area * (D.MLI_BASE_KG_PER_M2 + D.MLI_KG_PER_M2_PER_LAYER * e.mli_layers)
            t_max = max(ts.values())
            if e.common_bulkhead:
                from .models.structures import common_bulkhead as CB
                if "LH2" in props.values() and CB.enabled():
                    # a honeycomb sandwich sized for the pressure difference (off by default)
                    e.bulkhead_layup = CB.sandwich(r_cb, e.tank_pressure, mat, proof)
                    bd["common_bulkhead"] = dome_area(r_cb) * e.bulkhead_layup["kg_per_m2"]
                else:
                    core = D.COMMON_BULKHEAD_CORE_KG_PER_M2 if "LH2" in props.values() else 0.0
                    bd["common_bulkhead"] = dome_area(r_cb) * (t_max * mat["rho"] * mat.get("liner_factor", 1.0) + core)
            else:
                # a skirt at the tanks' mean diameter spanning their two facing domes, each r / sqrt(2) high;
                # it is sized for the axial load above it once the stack is known (_size_intertank)
                rf, ro = e.tanks["fuel"].radius, e.tanks["ox"].radius
                e.intertank = ((rf + ro) / 2, (rf + ro) / math.sqrt(2.0))
            # Pressurant: helium in COPVs at 300 bar, enough to keep the tanks at pressure when empty.
            V = sum(t.volume for t in e.tanks.values())
            p_copv = max(300e5, 1.5 * e.tank_pressure)
            n_he = e.tank_pressure * V / (8.314 * 250.0)
            V_c = n_he * 8.314 * D.HELIUM_T / (p_copv - e.tank_pressure)
            bd["helium"] = n_he * 0.004003
            bd["copv"] = p_copv * V_c / (D.COPV_PV_OVER_W * D.G0)
            # Residual (unusable) propellant: 0.5% of capacity.
            bd["residual_propellant"] = 0.005 * capacity_total
            bd["secondary_structure_and_systems"] = D.STAGE_SYSTEMS_KG * V ** D.STAGE_SYSTEMS_EXPONENT
            from .models.slosh.coupled import baffle_mass
            m_baffles = baffle_mass(e)   # the ring baffles; only a stage that asks for them carries any
            if m_baffles > 0:
                bd["ring_baffles"] = m_baffles
            for en, _ in e.engines:
                if e.tank_pressure < en.tank_pressure_min - 1:
                    errors.append(f"{where}: tank pressure {e.tank_pressure / 1e5:.1f} bar is below the "
                                  f"{en.tank_pressure_min / 1e5:.1f} bar engine {en.name} needs at its inlet")
    elif e.engines:
        errors.append(f"{where}: has engines but no propellant capacity")
    if e.engines:
        bd["engines"] = sum(en.mass * n for en, n in e.engines)
        F = sum(en.thrust_vac * n for en, n in e.engines)
        bd["thrust_structure"] = D.THRUST_STRUCTURE_KG_PER_KN * F / 1e3
        # Electric pumps: the battery must deliver the pumps' power.
        epow = sum(en.pump_power_W * n for en, n in e.engines)
        if epow > 0:
            batt_W = 1000 * _f(s, "battery_kWh", 0, 5000, errors, where, default=0.0) / D.BATTERY_WH_PER_KG * D.BATTERY_W_PER_KG
            if batt_W < epow:
                errors.append(f"{where}: electric pumps draw {epow / 1e3:.0f} kW but the battery can deliver only {batt_W / 1e3:.0f} kW")
    legs = s.get("legs")
    if legs:
        e.legs_touchdown_speed = _f(legs, "touchdown_speed_mps", 0.5, 6.0, errors, f"{where} legs", default=2.0)
        bd["landing_legs_per_kg_landed"] = 0.0  # sized against landed mass in _check_stack
    _check_fins(s, errors, f"{where} fins", e.diameter)
    piv = s.get("pyro_isolation_valves", False)
    if not isinstance(piv, bool):
        errors.append(f"{where}: pyro_isolation_valves must be true or false")
    elif piv and not e.tanks:
        errors.append(f"{where}: pyro_isolation_valves needs propellant tanks to isolate")
    if s.get("interstage", True) and e.engines:
        bd["interstage"] = D.INTERSTAGE_KG_PER_M2 * math.pi * e.diameter * (0.5 * e.diameter)
    tank_len = sum(t.length for t in e.tanks.values())
    eng_len = 0.0
    for en, _ in e.engines:
        De = math.sqrt(4 * en.Ae / math.pi)
        Dt = math.sqrt(4 * en.At / math.pi)
        eng_len = max(eng_len, 0.8 * (De - Dt) / 2 / math.tan(math.radians(15)) + 2.0 * Dt + 0.5)
    e.length = tank_len + eng_len + (0.5 * e.diameter if e.engines else 0.0)
    if e.mli_layers and e.mli_layers < 10:
        warnings.append(f"{where}: fewer than 10 MLI layers insulates poorly")


def _check_launch_site(design, errors):
    """launch.site: one of the published pads the range model knows (data.LAUNCH_SITES); its launch azimuth limits
    and impact checks are the range's own (models/range, data/range/sites.json), checked in flight."""
    la = design.get("launch")
    if la is None:
        return
    if not isinstance(la, dict):
        errors.append("launch must be an object")
        return
    if "site" not in la:
        return
    s = la["site"]
    if s not in D.LAUNCH_SITES:
        errors.append(f"launch.site {s!r} is not one of the published pads {list(D.LAUNCH_SITES)}")
        return
    from .models.range import safety
    lim = safety.sites()["sites"][s].get("azimuth_deg")
    if not (isinstance(lim, list) and len(lim) == 2 and lim[0] < lim[1]):
        errors.append(f"launch.site {s}: the range has no launch azimuth limits for it")


def _check_fins(s, errors, where, body_d):
    """A stage's fins, as dynamics/disturbances.py reads them: {"count", "span_m", "root_chord_m",
    "tip_chord_m", "sweep_m"}. Checked here so the 6-DOF never meets a malformed fin set."""
    f = s.get("fins")
    if f is None:
        return
    if not isinstance(f, dict):
        errors.append(f"{where}: fins must be an object with count, span_m and root_chord_m")
        return
    n = f.get("count", 4)
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not float(n).is_integer() or not 2 <= n <= 8:
        errors.append(f"{where}: count must be a whole number from 2 to 8")
    _f(f, "span_m", 0.01, max(0.01, 2.0 * (body_d or 1.0)), errors, where)
    cr = _f(f, "root_chord_m", 0.01, 20.0, errors, where)
    _f(f, "tip_chord_m", 0.0, cr, errors, where, default=0.0)
    _f(f, "sweep_m", 0.0, 20.0, errors, where, default=0.0)


def _check_stack(elements, errors, warnings):
    ids = [e.id for e in elements]
    # A stage's interstage joins it to the element below and stays with that element when they separate
    # (as on Falcon 9). The bottom element of the stack has no interstage.
    for i, e in enumerate(elements):
        if e.is_stage and "interstage" in e.mass_breakdown:
            m = e.mass_breakdown.pop("interstage")
            below = elements[i - 1] if i > 0 else None
            if below is not None and below.kind in ("stage", "heatshield"):
                below.mass_breakdown["interstage_above"] = below.mass_breakdown.get("interstage_above", 0.0) + m
                below.dry_mass = sum(below.mass_breakdown.values())
            e.dry_mass = sum(e.mass_breakdown.values())
    if not any(e.is_stage and e.engines for e in elements):
        errors.append("the vehicle has no stage with engines")
    caps = [e for e in elements if e.kind == "capsule"]
    if len(caps) != 1:
        errors.append("the vehicle needs exactly one capsule: the Earth return capsule that carries the samples home")
    # Aeroshell pairs: a heatshield protects elements above it up to its backshell.
    for i, e in enumerate(elements):
        if e.kind == "heatshield":
            j = next((k for k in range(i + 1, len(elements)) if elements[k].kind == "backshell"), None)
            if j is None:
                errors.append(f"heatshield {e.id} needs a backshell above it (the elements between them are inside the aeroshell)")
                continue
            inside = elements[i + 1:j]
            e.spec["_inside"] = [x.id for x in inside]
            bs = elements[j]
            e.spec["_backshell"] = bs.id
            bs.diameter = e.diameter
            Rb = e.diameter / 2
            height = max(0.6 * e.diameter, sum(x.length for x in inside if x.kind not in ("parachute",)) * 1.05)
            slant = math.sqrt(height ** 2 + (0.3 * Rb) ** 2)
            area = math.pi * (Rb + 0.4 * Rb) * slant
            bs.mass_breakdown["backshell_tps"] = area * bs.tps_areal
            bs.mass_breakdown["backshell_structure"] = area * 5.0
            bs.dry_mass = sum(bs.mass_breakdown.values())
            bs.length = height
            for x in inside:
                if x.diameter > 0.92 * e.diameter:
                    errors.append(f"element {x.id} ({x.diameter:.2f} m) does not fit inside aeroshell {e.id} ({e.diameter:.2f} m; 92% usable)")
            if e.ld_trim > 0:
                m_entry = e.dry_mass + bs.dry_mass + sum(x.dry_mass + sum(x.loaded.values()) + x.rcs_prop for x in inside)
                e.ballast_kg = D.BALLAST_FRACTION_PER_LD * e.ld_trim * m_entry
                e.mass_breakdown["trim_ballast_jettisoned_before_parachute"] = e.ballast_kg
                e.dry_mass = sum(e.mass_breakdown.values())
    # A parachute rides on the nearest backshell or capsule below it in the stack.
    for i, e in enumerate(elements):
        if e.kind == "parachute":
            e.carrier = next((elements[k].id for k in range(i - 1, -1, -1)
                              if elements[k].kind in ("backshell", "capsule")), None)
    _size_limits(elements, errors)
    for i, e in enumerate(elements):
        if e.kind == "fairing":
            covered = elements[i + 1:]
            e.spec["_covers"] = [x.id for x in covered]
            need_d = max([x.diameter for x in covered] + [0.0]) + 0.2
            # What rides inside an aeroshell is already inside the backshell's height.
            enclosed = set()
            for x in covered:
                enclosed.update(x.spec.get("_inside", []))
            need_l = sum(x.length for x in covered if x.kind != "parachute" and x.id not in enclosed)
            if e.diameter < need_d:
                errors.append(f"fairing {e.id} diameter {e.diameter:.2f} m is less than the payload's {need_d:.2f} m (incl. 0.2 m clearance)")
            if e.length < need_l:
                errors.append(f"fairing {e.id} is {e.length:.1f} m long but the payload stack above it is about {need_l:.1f} m")
    # Payload is built to its vibration qualification levels (vibro): the liftoff noise it hears sets the default.
    if elements and elements[0].engines:
        vibro.default_levels(elements)
    for e in elements:
        if e.vibro_payload:
            hm = vibro.hardening_mass(e)
            if hm:
                e.mass_breakdown["vibration_hardening"] = hm
                e.dry_mass = sum(e.mass_breakdown.values())
    # The propellant head: under acceleration the propellant presses on the lower wall of its tank. The
    # stages are taken to fire bottom-up, each at full thrust to depletion with everything above attached.
    # A tank is worst loaded while it is still full: at the highest burnout acceleration of the stages
    # below it, or at its own ignition. Its lower wall is thickened for that head (_head_reinforcement).
    # Fairings are left out of the stack, since they are dropped early in an upper stage's burn.
    # The intertanks (_size_intertank) carry the stack at the same design acceleration. Head walls and
    # intertanks change the masses that set the accelerations, so the sizing runs twice.
    for _ in range(2):
        wet = [0.0 if x.kind == "fairing" else x.dry_mass + sum(x.loaded.values()) + x.rcs_prop for x in elements]
        worst = 0.0
        for i, e in enumerate(elements):
            F = sum(en.thrust_vac * n for en, n in e.engines)
            m = sum(wet[i:])
            if e.is_stage and e.tanks:
                e.design_accel = max(worst, F / m)
                sf = D.PROOF_FACTOR if getattr(e, "qualified", False) else D.PROOF_FACTOR_UNTESTED
                for role, kg in _head_reinforcement(e, e.design_accel, sf).items():
                    if kg > 0:
                        e.mass_breakdown[f"{role}_tank_head"] = kg
                e.dry_mass = sum(e.mass_breakdown.values())
            prop = sum(e.loaded.values())
            if F > 0 and prop > 0:
                worst = max(worst, F / (m - prop))
        for i, e in enumerate(elements):
            if e.is_stage and e.intertank:
                _size_intertank(e, elements[i + 1:])
    # Landing legs are sized against everything they carry at touchdown (the element and all above).
    for i, e in enumerate(elements):
        if e.is_stage and e.legs_touchdown_speed:
            landed = sum(x.dry_mass + sum(x.loaded.values()) + x.rcs_prop for x in elements[i:]
                         if x.kind not in ("heatshield", "backshell", "parachute", "fairing"))
            e.mass_breakdown["landing_legs"] = D.LEG_MASS_FRACTION * landed * (e.legs_touchdown_speed / 2.0) ** 0.5
            e.mass_breakdown.pop("landing_legs_per_kg_landed", None)
            e.dry_mass = sum(e.mass_breakdown.values())


# --- Hidden mass growth on the built dry mass  -------------
# A part whose mass comes from a model fitted to flown hardware already carries the growth real programs saw, so
# its hidden growth has median 0 and a spread equal to that model's own error. Masses the team enters directly get
# the full AIAA S-120A draw (models/mass_growth/growth.py). For the fitted parts the new growth replaces
# Dispersions.dry_mass_factor (the old 1% as-built spread); entered items keep it. MARSFLIGHT_MASS_GROWTH=1 turns it
# on; it stays off until the reference carries the S2 reserve (2%) the growth needs.
# Spread = the model's mean absolute leave-one-out error x sqrt(pi / 2), the normal sigma with that mean error.
_SQ = math.sqrt(math.pi / 2)
FIT_SPREAD = {
    # stage mass model: leave-one-out 6.6% on five flown stages (our validation notes, not included)
    "stage": 0.066 * _SQ,
    # engine model of models/propulsion/engine.py: mean error 28.8% on the 37 flown engines (measured for the
    # follow-up PR); catalog engines are flown hardware and keep the existing-hardware row
    "engine": 0.288 * _SQ,
    # parachute canopy: Orion main, not fitted, 119 kg against 140.6 kg (15%)
    "parachute": 0.15 * _SQ,
    # aeroshell, backshell and fairing areal rules: no flown check of their own, so the stage model's spread
    "shell": 0.066 * _SQ,
}
# Items the team enters directly (S-120A growth): the capsule's structure_mass_kg and extra_mass_kg.
ENTERED_ITEMS = {"structure_and_containment", "extra"}


def growth_enabled():
    return os.environ.get("MARSFLIGHT_MASS_GROWTH", "1") == "1"


def _fit_kind(el, item):
    if item == "engines":
        return "engine"
    if el.kind == "parachute":
        return "parachute"
    if el.kind == "stage":
        return "stage"
    return "shell"


def mass_growth_truth(secret, design, vehicle) -> dict:
    """Hidden growth of every element, by id: {"fitted_basic_kg", "fitted_growth_kg", "entered_growth_kg"}. Drawn
    from the program secret with the part keys and streams, so a weighing of the same part sees the same
    growth. For program.flight_truth (key "mass_growth")."""
    from . import program as P
    from .models.mass_growth import growth as G
    engines = design.get("engines") or {}
    specs = {str(e.get("id")): e for e in design.get("elements") or [] if isinstance(e, dict)}
    out = {}
    for el in vehicle.elements:
        spec = specs.get(el.id, {"kind": el.kind})
        key = G.element_key(spec, engines)
        fb = fg = eg = 0.0
        for item, m in (el.mass_breakdown or {}).items():
            col, existing = G.category(item)
            if col is None or m <= 0:
                continue
            if item in ENTERED_ITEMS:
                eg += m * G.growth_draw(secret, key, col, existing)
                continue
            if item == "engines":
                for en, n in el.engines or []:
                    espec = engines.get(en.name) or {}
                    if espec.get("catalog"):
                        eg += en.mass * n * G.growth_draw(secret, P.engine_key(espec), "propulsion", existing=True)
                        continue
                    z = float(G._rng(secret, f"mass:{P.engine_key(espec)}:propulsion").standard_normal())
                    fb += en.mass * n
                    fg += en.mass * n * (math.exp(FIT_SPREAD["engine"] * z) - 1.0)
                continue
            z = float(G._rng(secret, f"mass:{key}:{col}").standard_normal())
            fb += m
            fg += m * (math.exp(FIT_SPREAD[_fit_kind(el, item)] * z) - 1.0)
        out[el.id] = {"fitted_basic_kg": fb, "fitted_growth_kg": fg, "entered_growth_kg": eg}
    return out


def as_built_dry(el, dry_mass_factor=1.0, growth=None) -> float:
    """The dry mass an element flies with. Without growth (or with MARSFLIGHT_MASS_GROWTH off) it is today's
    design mass times the world's dry_mass_factor. With it, the fitted parts fly at their basic mass plus their
    growth, and the rest at the design mass times dry_mass_factor plus the S-120A growth of entered items."""
    g = (growth or {}).get(el.id) if growth_enabled() else None
    if not g:
        return el.dry_mass * dry_mass_factor
    fb = g["fitted_basic_kg"]
    return fb + g["fitted_growth_kg"] + (el.dry_mass - fb) * dry_mass_factor + g["entered_growth_kg"]


def load_design(path) -> Vehicle:
    with open(path) as f:
        return build_vehicle(json.load(f))
