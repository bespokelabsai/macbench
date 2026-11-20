"""Small geodesy helpers. Positions are (lat, lon) in degrees; distances in NM; bearings in degrees true."""
import math

R_NM = 3440.065
M_PER_NM = 1852.0
FT_PER_M = 3.28084


def dist_nm(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def dest(p, brg, d_nm):
    la1, lo1 = math.radians(p[0]), math.radians(p[1])
    b, d = math.radians(brg), d_nm / R_NM
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return (math.degrees(la2), (math.degrees(lo2) + 540.0) % 360.0 - 180.0)


def angdiff(a, b):
    """Signed smallest difference b - a in degrees, in (-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


class Local:
    """Flat x (east), y (north) in NM around a reference point. Good to a few metres within 150 NM."""

    def __init__(self, ref):
        self.ref = ref
        self.kx = math.cos(math.radians(ref[0])) * 60.0
        self.ky = 60.0

    def xy(self, p):
        return ((p[1] - self.ref[1]) * self.kx, (p[0] - self.ref[0]) * self.ky)

    def ll(self, x, y):
        return (self.ref[0] + y / self.ky, self.ref[1] + x / self.kx)


def wind_components(wind_from_deg, wind_kt, runway_heading):
    """(headwind, crosswind) in kt for a runway heading; headwind < 0 is a tailwind."""
    a = math.radians(wind_from_deg - runway_heading)
    return wind_kt * math.cos(a), abs(wind_kt * math.sin(a))
