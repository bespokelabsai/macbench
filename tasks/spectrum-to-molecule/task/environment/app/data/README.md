# data

`train_spectra.parquet`, one row per spectrum:

| column | type | |
|---|---|---|
| `spectrum_id` | string | |
| `mzs`, `intensities` | list of double | fragment peaks, same length |
| `precursor_mz` | double | |
| `adduct` | string | `[M+H]+` or `[M+Na]+` |
| `instrument_type` | string or null | `Orbitrap`, `QTOF`, or null if unknown |
| `collision_energy` | double or null | |
| `smiles` | string | the compound's structure |
| `inchikey14` | string | first 14 characters of its InChIKey |

`candidates.parquet`, one row per structure, sorted by `inchikey14`: `smiles`, `inchikey14`.
