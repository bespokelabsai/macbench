# Notice

Mars Return's own code, documentation and data by Bespoke Labs are licensed under the
[Apache License 2.0](../../LICENSE). The third-party software and data below keep their own licences and are not
covered by the Apache License 2.0.

## Commercial use: not offered as commercially usable

The task's image installs [pymsis](https://github.com/SWxTREC/pymsis) (MIT), which wraps the NRL MSIS 2.0 and 2.1
Earth atmosphere models. NRL's licence for MSIS 2.0 and 2.1 does not allow commercial use. pymsis is installed from
PyPI when the image is built and is not bundled in this folder, but running Mars Return still runs MSIS. Mars Return
is therefore offered for non-commercial research use only. Anyone who wants to run it commercially needs a licence
from NRL or a replacement for the Earth atmosphere model.

## Mars dust climatology (CC BY-SA 3.0)

`task/tests/hidden/marsflight/data/mars/dust_montabone.npz` is adapted from the
[Mars dust climatology](https://www-mars.lmd.jussieu.fr/mars/dust_climatology) of the Laboratoire de Météorologie
Dynamique (LMD), the dust scenarios of the LMD Mars Climate Database, which LMD makes available under the
[Creative Commons Attribution-ShareAlike 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/) licence.

- Credit: Montabone et al. (2015), *Eight-year climatology of dust optical depth on Mars*, Icarus 251, 65-95,
  [doi:10.1016/j.icarus.2014.12.034](https://doi.org/10.1016/j.icarus.2014.12.034); Montabone et al. (2020),
  *Martian Year 34 column dust climatology from Mars Climate Sounder observations*, JGR Planets 125,
  [doi:10.1029/2019JE006111](https://doi.org/10.1029/2019JE006111). Data from the LMD Mars Climate Database dust
  climatology.
- Changes: the daily kriged maps (version 2, Mars years 24 to 36) were regridded to 6 × 6 degrees, scaled from
  9.3 µm absorption to visible optical depth (× 2.6), quantized to 8 bits and packed into one file.
- Licence: the adapted file stays under CC BY-SA 3.0, separate from the Apache-licensed code. The licence text and
  credit are in `task/tests/hidden/marsflight/data/mars/LICENSE-dust_montabone.txt`.

## Other software and data

| What | Source | Licence or terms |
|---|---|---|
| Astrodynamics | [Tudat](https://github.com/tudat-team/tudat) (TU Delft) and its data release tudat-resources | BSD 3-Clause |
| Planetary ephemerides | JPL DE440 | Public domain (NASA/JPL) |
| Engine performance | NASA CEA | Apache 2.0 |
| Slosh, avionics and power checks | Basilisk (AVS Lab, University of Colorado Boulder) | ISC |
| Earth atmosphere | NRLMSISE-00 and NRLMSIS 2.1, through pymsis | pymsis: MIT. MSIS 2.0/2.1: NRL licence, no commercial use (see above) |
| Earth winds | NOAA NCEI Integrated Global Radiosonde Archive | Public domain |
| Space weather | CelesTrak, with Kp and ap from GFZ Potsdam | GFZ data: CC BY 4.0 |
| Jezero terrain | USGS products | Public domain (CC0) |
| Mars dust | Montabone et al., LMD Mars Climate Database dust climatology | CC BY-SA 3.0 (see above) |

Every other number in `task/tests/hidden/marsflight/data/` names its published source next to it.
