"""Units in brackets after a number, as the SysML v2 standard library names them (SI and ISQ packages).

A unit expression is a product of unit symbols with integer powers, `*` and `/` and parentheses, e.g.
`m`, `mm`, `m/s`, `deg/s`, `m^2`, `W/(m^2*K)`. Values are converted to SI. Plane angle is kept as its
own dimension so that a slew rate in m/s, say, is caught; values in angles come out in degrees, the
unit the simulator's internal design uses.
"""
from __future__ import annotations

import math
import re

# dimension exponents: length, mass, time, temperature, angle
DIM = ("m", "kg", "s", "K", "angle")


def _d(m=0, kg=0, s=0, K=0, a=0):
    return (m, kg, s, K, a)


UNITS = {
    # length
    "m": (1.0, _d(m=1)), "mm": (1e-3, _d(m=1)), "cm": (1e-2, _d(m=1)), "km": (1e3, _d(m=1)),
    # mass
    "L": (1e-3, _d(m=3)),
    "kg": (1.0, _d(kg=1)), "g": (1e-3, _d(kg=1)), "t": (1e3, _d(kg=1)),
    # time
    "s": (1.0, _d(s=1)), "min": (60.0, _d(s=1)), "h": (3600.0, _d(s=1)), "d": (86400.0, _d(s=1)),
    # temperature
    "K": (1.0, _d(K=1)),
    # force, pressure, energy, power
    "N": (1.0, _d(m=1, kg=1, s=-2)), "kN": (1e3, _d(m=1, kg=1, s=-2)), "MN": (1e6, _d(m=1, kg=1, s=-2)),
    "Pa": (1.0, _d(m=-1, kg=1, s=-2)), "kPa": (1e3, _d(m=-1, kg=1, s=-2)), "MPa": (1e6, _d(m=-1, kg=1, s=-2)),
    "bar": (1e5, _d(m=-1, kg=1, s=-2)),
    "J": (1.0, _d(m=2, kg=1, s=-2)), "kJ": (1e3, _d(m=2, kg=1, s=-2)), "MJ": (1e6, _d(m=2, kg=1, s=-2)),
    "W": (1.0, _d(m=2, kg=1, s=-3)), "kW": (1e3, _d(m=2, kg=1, s=-3)),
    # plane angle: values are kept in degrees
    "deg": (1.0, _d(a=1)), "°": (1.0, _d(a=1)), "rad": (180.0 / math.pi, _d(a=1)),
}

# ISQ quantity value types the library uses, and their dimensions.
QUANTITY_TYPES = {
    "LengthValue": _d(m=1), "DistanceValue": _d(m=1), "DiameterValue": _d(m=1), "RadiusValue": _d(m=1),
    "ThicknessValue": _d(m=1),
    "MassValue": _d(kg=1),
    "DurationValue": _d(s=1), "TimeValue": _d(s=1),
    "ForceValue": _d(m=1, kg=1, s=-2),
    "PressureValue": _d(m=-1, kg=1, s=-2),
    "EnergyValue": _d(m=2, kg=1, s=-2),
    "PowerValue": _d(m=2, kg=1, s=-3),
    "AreaValue": _d(m=2),
    "VolumeValue": _d(m=3),
    "SpeedValue": _d(m=1, s=-1),
    "AngularMeasureValue": _d(a=1),
    "AngularVelocityValue": _d(a=1, s=-1),
    "CoefficientOfHeatTransferValue": _d(kg=1, s=-3, K=-1),
}
SCALAR_TYPES = {"Real", "Integer", "Natural", "Positive", "Boolean", "String", "Number", "Rational"}

_TOK = re.compile(r"\s*(?:(\d+)|([A-Za-z°]+)|(.))")


class UnitError(ValueError):
    pass


def parse_unit(text: str):
    """Returns (factor to SI, dimension tuple)."""
    toks = [m.group(1) or m.group(2) or m.group(3) for m in _TOK.finditer(text) if (m.group(1) or m.group(2) or m.group(3))]
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def take():
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def factor():
        t = take() if peek() is not None else None
        if t == "(":
            f, d = product()
            if take() != ")":
                raise UnitError(f"unbalanced parentheses in [{text}]")
        elif t in UNITS:
            f, d = UNITS[t]
        else:
            raise UnitError(f"unknown unit {t!r} in [{text}]")
        if peek() == "^":
            take()
            sign = 1
            if peek() == "-":
                take()
                sign = -1
            n = take()
            if n is None or not n.isdigit():
                raise UnitError(f"a power must be a whole number in [{text}]")
            p = sign * int(n)
            f, d = f ** p, tuple(x * p for x in d)
        return f, d

    def product():
        f, d = factor()
        while peek() in ("*", "/"):
            op = take()
            f2, d2 = factor()
            if op == "*":
                f, d = f * f2, tuple(a + b for a, b in zip(d, d2))
            else:
                f, d = f / f2, tuple(a - b for a, b in zip(d, d2))
        return f, d

    f, d = product()
    if pos != len(toks):
        raise UnitError(f"cannot read unit [{text}]")
    return f, d


def dim_text(d):
    parts = [f"{n}^{p}" if p != 1 else n for n, p in zip(DIM, d) if p]
    return "*".join(parts) or "1"
