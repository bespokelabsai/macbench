"""Planet and Moon positions, the ``ephemeris`` role.

What the simulator flies today is not DE440. ``environment.KERNELS`` loads ``de440.bsp`` and then Tudat's
``tudat_merged_spk_kernel.bsp``, and SPICE answers from the kernel loaded last. The merged kernel (SPKMERGE log in
its comment area, made 2019-10-17) holds DE438 for every planet barycenter, the Sun, Earth and Moon, from 1995 to
2040, plus mar097 for the centre of Mars and jup310 for Jupiter. So every body comes from DE438. DE438 and DE440 differ by 0.3 km for Earth and 0.7 km for Mars
relative to the Sun in the mission years: small, but it is not the ephemeris the docs name.

``de440`` (the default): the same kernels with DE440 loaded last, so DE440 (Park et al. 2021, AJ 161, 105,
doi:10.3847/1538-3881/abd414; JPL/NAIF, public) gives the barycenters, Sun, Earth and Moon. Since the area models were wired in
(Oct 27, 2026) the simulator no longer reads the merged kernel: it ends on 2040-01-01, and Mars and Jupiter fly on
their DE440 barycentres (0.2 m from Mars's centre), so the simulated ephemeris runs to 2650. The merged kernel
stays loaded underneath only for flight software that asks SPICE for Mars's centre (to 2040).

``tudat_merged``: today's order, kept for before-and-after runs.
"""
from __future__ import annotations

import os

from ...core.model import Provider

KDIR_DEFAULT = os.path.expanduser("~/.tudat/resource/spice_kernels")
TEXT = ["naif0012.tls", "pck00010.tpc", "inpop19a_TDB_m100_p100_spice.tpc", "NOE-4-2020.tpc"]


def _kdir():
    return os.environ.get("MARSFLIGHT_KERNELS", KDIR_DEFAULT)


class DE440(Provider):
    """DE440 for the simulator . The merged kernel ends on 2040-01-01 and
    was the only source of Mars's and Jupiter's centres. Mars and Jupiter follow their DE440
    system barycentres instead (``centres``): Mars's centre is 0.2 m from its barycentre (mar097), and the
    Jupiter planet's offset from its barycentre (up to about 1,000 km) does not matter at Earth or Mars distances.
    DE440 runs to 2650."""
    role = "ephemeris"
    name = "de440"
    default = True
    # The merged kernel is still loaded first (lowest priority) only so that flight software asking SPICE for
    # "Mars" (body 499), as the reference's gnc/util.py does, keeps working to 2040; the simulator itself never
    # reads it: Mars and Jupiter fly on their DE440 barycentres (``centres``).
    order = TEXT + ["tudat_merged_spk_kernel.bsp", "de440.bsp"]
    centres = {"Mars": "MARS BARYCENTER", "Jupiter": "JUPITER BARYCENTER"}

    def kernels(self):
        return [os.path.join(_kdir(), k) for k in self.order]

    def load(self):
        from tudatpy.interface import spice
        for k in self.kernels():
            spice.load_kernel(k)


class TudatMerged(Provider):
    role = "ephemeris"
    name = "tudat_merged"
    default = False
    order = TEXT + ["de440.bsp", "tudat_merged_spk_kernel.bsp"]
    centres = {}
    kernels = DE440.kernels
    load = DE440.load


PROVIDERS = [DE440, TudatMerged]
