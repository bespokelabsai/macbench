"""Mars gravity, the ``gravity.mars`` role.

``jgmro120f`` (the default): JGMRO_120F, MRO radio science (Konopliv et al. 2020, GRL 47, e2020GL090568,
doi:10.1029/2020GL090568), from NASA PDS volume MRO-M-RSS-5-SDP-V1 (public domain). The file
``data/mars/jgmro_120f_deg50.txt`` is the PDS table cut at degree 50 and put in Tudat's format by
``results/issue-113/build/make_gravity.py``. Flights use degree and order ``DEGREE`` (30).

Why 30: ``results/issue-113/gravity_degree.py`` flew a day in low Mars orbit and an entry against the full
degree 120 field. After one day at 200 km the position is off by 3.3 km at degree 12 (today's), 0.30 km at 30
and 0.14 km at 50; at 400 km by 1.6 km, 0.13 km and 0.01 km. Entry moves by less than 10 m at any degree from 8
up. Degree 30 costs about twice degree 12 per step (0.17 against 0.08 ms) and degree 50 about four times.

``jgmro120d``: today's field, Tudat's built-in JGMRO_120D at degree 12, kept for before-and-after runs. At equal
degree the two fields agree to 2 m after a day; the change that matters is the degree.
"""
from __future__ import annotations

import os

from ...core.model import Provider

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "mars", "jgmro_120f_deg50.txt")
DEGREE = 30
FILE_DEGREE = 50


class JGMRO120F(Provider):
    role = "gravity.mars"
    name = "jgmro120f"
    default = True
    degree = DEGREE
    path = DATA

    def tudat_settings(self, degree=None):
        from tudatpy.dynamics import environment_setup as es
        n = int(degree or self.degree)
        if n > FILE_DEGREE:
            raise ValueError(f"the shipped JGMRO_120F file stops at degree {FILE_DEGREE}")
        return es.gravity_field.from_file_spherical_harmonic(os.path.abspath(self.path), n, n)

    def acceleration_degree(self):
        """Degree and order for ``propagation_setup.acceleration.spherical_harmonic_gravity``."""
        return self.degree, self.degree


class JGMRO120D(Provider):
    role = "gravity.mars"
    name = "jgmro120d"
    default = False
    degree = 12

    def tudat_settings(self, degree=None):
        from tudatpy.dynamics import environment_setup as es
        return es.gravity_field.predefined_spherical_harmonic(
            es.gravity_field.PredefinedSphericalHarmonicsModel.jgmro120d, int(degree or self.degree))

    def acceleration_degree(self):
        return self.degree, self.degree


PROVIDERS = [JGMRO120F, JGMRO120D]
