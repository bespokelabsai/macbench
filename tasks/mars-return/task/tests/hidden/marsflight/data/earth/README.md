# Earth data

Nothing here is downloaded at run time.

| File | What | Source and license | Built by |
|---|---|---|---|
| `SW-All.csv.gz` | Daily space weather: observed Oct 1957 to Oct 26, 2026, then NOAA SWPC's monthly solar flux forecast to Oct 2041 | CelesTrak `SW-All.csv`, fetched Oct 27, 2026 (sha256 of the uncompressed file: 23895b32bbfd2672595eca97e0f869fb87047fbb01a548c607a0e4b3383e0382). Kp and ap from GFZ Potsdam (CC BY 4.0); F10.7 from NRC Canada's Penticton observatory (open data) | `gzip -9 -n` of the download |
| `winds_igra.npz` | Measured wind profiles, 0 to 30 km every 250 m, at Cape Canaveral, Vandenberg and Salt Lake City, 2005 to 2025 | NOAA NCEI Integrated Global Radiosonde Archive v2 (public domain), stations USM00074794, USM00072393, USM00072572, fetched Oct 27, 2026 | `results/issue-112/igra_profiles.py`, then `build_wind_bank.py` |
