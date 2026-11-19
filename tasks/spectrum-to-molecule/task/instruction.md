We want a program that identifies the compound behind an MS/MS spectrum from our instrument. For each spectrum it should rank structures from our candidate collection, most likely first.

What we have is in `data/` (formats in `data/README.md`):

- `train_spectra.parquet`: 206,993 spectra of 26,195 known compounds, with their structures.
- `candidates.parquet`: 4,486,177 structures. The compounds we need identified are all in it.

The program is `./predict <spectra.jsonl> <out.jsonl>`, run from the repo root. Each input line is one spectrum with the fields `spectrum_id`, `mzs`, `intensities`, `precursor_mz`, `adduct`, `instrument_type` and `collision_energy`, as in the training file. For each spectrum, write one line `{"spectrum_id": "...", "smiles": [...]}` with up to 20 SMILES, best first.

We'll run it on our processing server, which has the same setup and `data/` but no internet access, on batches of spectra from compounds we haven't identified before. A batch of around 1,300 spectra needs to finish within an hour. We deploy from `main`, so commit everything it needs beyond `data/`, trained models included, and keep `main` working as you go.

Precursor m/z values on these spectra come straight from the instrument: Orbitrap precursors are typically within 3 ppm (never more than 6); QTOF typically within 6 ppm (rarely up to ~20). Treat an unknown instrument like QTOF. An identification is correct if it has the right 2D structure; stereochemistry doesn't matter.

Make it as accurate as you can; we'll use whatever is on `main` at the end of the day.
