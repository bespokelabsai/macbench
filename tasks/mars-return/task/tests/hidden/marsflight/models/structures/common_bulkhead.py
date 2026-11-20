"""A common bulkhead between LH2 and another propellant as a honeycomb sandwich sized for its pressure difference
(open item 2).

Switch: MARSFLIGHT_COMMON_BULKHEAD_SANDWICH=1 sizes the bulkhead here. Unset or 0 (the default), vehicle.py keeps
the v3 rule: the thicker tank wall plus a fixed 6 kg/m^2 core (data.COMMON_BULKHEAD_CORE_KG_PER_M2). A common
bulkhead between two propellants that are not LH2 stays a plain dome either way, as on Falcon 9.

The sandwich (S-II and S-IVB: 2014 aluminum face sheets bonded to fiberglass-phenolic honeycomb) is two equal face
sheets of thickness t_f, a core of depth h and two bond lines. It is sized on the crown of the dome, a 2:1 ellipsoid
of radius a whose crown radius of curvature is Rc = a^2 / b = sqrt(2) a:

1. Burst. One tank at its full pressure and the other vented gives a pressure difference dp = tank pressure. The
   two faces share the membrane load N = dp Rc / 2, so t_f = FS dp Rc / (4 sy), and no thinner than the minimum
   gauge. FS is the stage's factor of safety on yield (1.25 tested, 1.6 untested).
2. Collapse. The same dp the other way pushes the dome in. NASA CR-1457 (Sullins, Smith and Spier, "Manual for
   structural stability analysis of sandwich plates and shells", 1969) gives the classical buckling pressure of a
   sandwich sphere with thin faces and a core that carries only shear as

       p_cr = 4 E t_f h / (Rc^2 sqrt(1 - nu^2))

   (the isotropic 2 E t^2 / (R^2 sqrt(3 (1 - nu^2))) with the sandwich's bending and membrane stiffnesses). With the
   knockdown factor 0.3 that NASA SP-8032 uses for spherical caps, the core depth is
   h = FS dp Rc^2 sqrt(1 - nu^2) / (0.3 * 4 E t_f), and no thinner than the minimum core depth.
3. Mass per m^2 = 2 t_f rho_face + h rho_core + 2 bond lines.

Checked against the S-II (dev/test_sprint_mass.py): the saving against separate tanks is 3.6 to 4.1 t in the
documents listed in our validation notes (not included) The test prints what this model gives.
"""
from __future__ import annotations

import math
import os

from ... import data as D

# Fiberglass-phenolic honeycomb of 3.5 lb/ft^3 (56 kg/m^3), a common grade of the HRP family the S-II used.
CORE_DENSITY = 56.0
# One bond line of film adhesive, 0.06 lb/ft^2.
BOND_KG_PER_M2 = 0.29
# A core thinner than half an inch is not made for a bulkhead this size; it is also the floor when dp is small.
MIN_CORE_M = 0.0127
# Knockdown factor on the classical buckling pressure of a spherical cap (NASA SP-8032, 1969).
KNOCKDOWN = 0.3
POISSON = 0.33


def enabled() -> bool:
    return os.environ.get("MARSFLIGHT_COMMON_BULKHEAD_SANDWICH", "1") == "1"


def sandwich(radius: float, dp: float, mat: dict, fs: float) -> dict:
    """The sandwich of a common bulkhead dome of `radius` (m) for a pressure difference `dp` (Pa) both ways, faces of
    material `mat` (an entry of data.MATERIALS) with factor of safety `fs` on yield. Returns the layup and kg per m^2."""
    rc = math.sqrt(2.0) * radius
    dp = max(float(dp), 0.0)
    t_f = max(fs * dp * rc / (4.0 * mat["sy"]), D.MIN_GAUGE_M)
    h = fs * dp * rc * rc * math.sqrt(1.0 - POISSON ** 2) / (KNOCKDOWN * 4.0 * mat["E"] * t_f)
    h = max(h, MIN_CORE_M)
    kg_m2 = 2.0 * t_f * mat["rho"] * mat.get("liner_factor", 1.0) + h * CORE_DENSITY + 2.0 * BOND_KG_PER_M2
    return {"face_m": t_f, "core_m": h, "kg_per_m2": kg_m2, "crown_radius_m": rc, "dp_pa": dp}
