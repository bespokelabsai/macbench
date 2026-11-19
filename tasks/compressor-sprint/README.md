# Compressor Sprint

A multi-agent task: build a general-purpose lossless compressor that comes close to, or beats, the best compressors in the world on data of all kinds.

A team of agents has 4 hours. The state of the art is paq8px, and JPEG XL for images. The grader builds the team's `compressor/` folder from `main` and runs it on 25 hidden files of 21 kinds, about 1 MiB each.

## How it works

- **4 hours.** The agents are told they have 4 hours and are stopped at 4 hours 15 minutes. The compressor is graded when they finish.
- **The same problem at every team size.** Every agent gets the whole task, and only the number of agents changes. The team shares a git repository, and `compressor/` on `main` is what gets graded.
- **Twelve kinds of data are shown, 21 are graded.** `/data` has a large public file of each of twelve kinds (the Silesia corpus) and a 1 MiB practice file of each, with the practice files' targets. The hidden files come from other originals, and nine of the hidden kinds have no example at all.
- **No network and no compression libraries.** The agents work without network access. The image has Python and a C/C++ toolchain, and no compression tools beyond `xz`, `bzip2` and `gzip`.

## Grading

Each hidden file is scored on a log scale of its compressed size, so every 1% smaller counts the same:

- **0** at the zero point: the size from lpaq1 (a basic context-mixing compressor), or from the best of `xz -9e`, `zstd --ultra -22 --long=27`, `brotli -q 11 -w 24` and `bzip2 -9` if that is smaller.
- **1.0** at the state of the art: the size from paq8px v217 at `-9LT`, or from JPEG XL in lossless mode for image files if that is smaller.
- **Above 1.0** for a file smaller than the state of the art: 0.01 more for every 1%. A file's score has no cap.
- **0** if the file does not come back byte for byte, or a run breaks a limit. Each compress or decompress run gets 15 minutes of CPU time, 20 minutes of wall time, 8 GiB of memory and 16 MiB of output.

A kind's score is the mean of its files, and the reward is the mean over the 21 kinds, capped at 1.0. The grader is `task/tests/grade.py`; the zero points and state-of-the-art sizes are precomputed in `task/tests/targets.json`. Everything in `compressor/` except `bin/` must fit in 1 MiB after `xz -9e`; a larger submission scores 0.

The hidden kinds: the twelve shown (english, polish, dictionary, xml, source, programs, dll, mri, xray, chemistry, stars, database) and arm64, audio (3 files), chinese, csv, dna, floats, json, logs and photo (3 files).

Every grading writes `report.md` and `details.json` with the team's size, time and memory on each file next to every reference tool's.

## How to run

You need Docker, Python 3.12 or newer, and 8 CPUs and 16 GB of memory for every agent. Install the multi-agent harness, which brings Harbor with it, from its repository at the commit these commands were checked with:

```bash
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout cd6c6ce43168d531033699d1c3f3aefcbae791b9
pip install -e "$HOME/mac-bench/harness[docker]"
```

Put the model key in a file named `.env` in this folder (`ANTHROPIC_API_KEY=...`). Every command below runs from this folder.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8) with a shared git repository and a message board:

```bash
K=4
harbor run -p task \
  -e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=docker --ek n_agents=$K \
  --ek budget_usd=$((150 * K)) --agent-setup-timeout-multiplier 3 \
  -a multi_agent_harness.harbor.agent:TeamAgent --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max \
  --ak orchestration=task/mah/fixed_solo_resume.py \
  --ak timeout_sec=15300 --ak grade_at=30m,1h,2h,3h \
  --env-file .env
```

- These are the settings of our reference runs (Claude Code, Opus 5.5 at max effort). `budget_usd` caps the team's model spend at $150 for each agent.
- Every team size uses `task/mah/fixed_solo_resume.py`: every agent gets the whole task, the CLI's own subagents are off, and an agent whose session ends with an error is resumed in the same session.
- `grade_at` grades the team's `main` at 30 minutes and at hours 1, 2 and 3. The grading after the agents stop is the score. The agents are told that `main` is graded at intervals; they never see a result.
- The agents are stopped at 4 hours 15 minutes (`timeout_sec`); the prompt tells them they have 4 hours.
- The task gives the agents no network. The Docker backend does not enforce that, so on it the agents can reach the internet, where compressors can be downloaded. Our reference runs used a backend with network isolation; the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md) lists the backends.
- To use a Claude plan instead of an API key, put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `.env` and add `--ek model_upstream=subscription`. On the Docker backend also `export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0`: Claude Code's shell sandbox cannot start in a Docker agent box, and this runs it without (the agents' shell commands can then read the token). Other harnesses (Codex, Antigravity) and backends are described in the [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md).

### Harbor

`task/` is a Harbor task. One agent, with plain Harbor:

```bash
harbor run -p task -a claude-code -m anthropic/claude-opus-5-5 --ak reasoning_effort=max \
  --allow-agent-host api.anthropic.com --env-file .env
```

The agent works with no network. `--allow-agent-host` lets its CLI reach the model's API and nothing else; without it the agent cannot make a single call. On some Docker hosts name lookups fail under that rule, and the agent still cannot reach the model; one agent then runs through the harness with `K=1`, as above. The score is in the trial's `verifier/reward.txt`, next to `report.md` and `details.json`. The starter compressor only copies bytes, so a run that changes nothing scores 0 with every file coming back intact. For a quick check of the grader, add `--agent-timeout-multiplier 0.008` (about 2 minutes instead of 4 hours 15 minutes).

The reference solution, with no model:

```bash
harbor run -p task -a oracle
```

It scores 1.0. Its grading takes about 2 hours, because paq8px needs 1 to 8 minutes on each file, each way.

## Contents

| Path | What it is |
|---|---|
| `task/instruction.md` | The prompt every agent gets: goal, what is graded, the data, the targets, the rules. |
| `task/task.toml` | Agents stop at 4 h 15 min and have no network while they work. 8 CPUs and 16 GB per sandbox. |
| `task/environment/` | The image: Ubuntu 24.04 with Python and a C/C++ toolchain. `data/` becomes `/data`, read-only: `public/` (the twelve Silesia files, 210 MB), `samples/` (the practice files) and `samples.json` (their targets). `app/` becomes `/app`, the team repository, with a starter compressor that only copies bytes. |
| `task/tests/` | `test.sh` and `grade.py` (the grader), `hidden/` (the 25 hidden files) and `targets.json`. |
| `task/solution/` | The reference solution. `compressor/paq8px/` is the source of paq8px v217, with the zlib copy and the word lists it ships with, run at `-9LT`. `compressor/g16.cpp` is a coder for 16-bit pixel grids. For each file the smaller of the two outputs is kept. |
| `task/mah/` | `fixed_solo_resume.py`, the orchestration the multi-agent harness loads. |
| `task/licenses/` | The licence texts of the data sources (see Credits). |

## Credits

Every data file is cut from a public source and keeps that source's terms. The licence texts are in `task/licenses/`. Where a source states no terms of use, the table says so.

**Public examples**, `task/environment/data/public/`: the twelve files of the Silesia compression corpus (S. Deorowicz), renamed. The corpus's page states no terms of use; it names where each file came from.

| File | Silesia file | Origin named by the corpus |
|---|---|---|
| `english` | dickens | Fourteen works of Charles Dickens, from Project Gutenberg |
| `polish` | reymont | *Chłopi* by Władysław Reymont, from the Virtual Library of Polish Literature, as an uncompressed PDF |
| `dictionary` | webster | The 1913 Webster Unabridged Dictionary, from Project Gutenberg |
| `xml` | xml | The XML corpus prepared for XMLPPM |
| `source` | samba | Source code of Samba 2-2.3 |
| `programs` | mozilla | Executables of Mozilla 1.0 (Tru64 UNIX edition) |
| `dll` | ooffice | A DLL from OpenOffice.org 1.01 |
| `mri` | mr | A magnetic resonance image of 19 planes, described only as a hospital image |
| `xray` | x-ray | A 12-bit X-ray image of a hand, described only as a hospital image |
| `chemistry` | nci | Part of the NCI 2D structure file of August 2000, in SDF |
| `stars` | sao | The SAO star catalogue |
| `database` | osdb | A sample database from the Open Source Database Benchmark |

**Practice files**, `task/environment/data/samples/`, 1 MiB each:

| File | Source | Terms | Text in `task/licenses/` |
|---|---|---|---|
| `english` | Project Gutenberg ebook 599, *Vanity Fair* | Public domain in the US | `Project-Gutenberg-License.txt` |
| `polish` | Fourteen Polish-language Project Gutenberg ebooks | Public domain in the US | `Project-Gutenberg-License.txt` |
| `dictionary` | Project Gutenberg ebook 38390, *A Dictionary of English Synonymes* | Public domain in the US | `Project-Gutenberg-License.txt` |
| `xml` | Unicode CLDR 45, `common/main` | Unicode License v3. Copyright © 1991-2024 Unicode, Inc. | `Unicode-3.0.txt` |
| `source` | zlib 1.3.1 source | zlib licence | `zlib.txt` |
| `programs` | Files of the Ubuntu 24.04 `vim` package (9.1.0016) | Vim licence, with other notices for some files | `Vim.txt`, `ubuntu-copyright-vim.txt` |
| `dll` | Wine 9.0 `shell32.dll`, from the Ubuntu `libwine` package | LGPL 2.1 or later | `LGPL-2.1.txt`, `Wine-9.0-notice.txt`, `ubuntu-copyright-libwine.txt` |
| `mri` | OpenNeuro ds000001, the T1-weighted image of sub-01 | CC0 in the dataset description, PDDL 1.0 in its README | `CC0-1.0.txt`, `PDDL-1.0.txt` |
| `xray` | pydicom-data `RG3_UNCI.dcm`, from the NEMA WG04 compression samples | MIT for the repository; the NEMA set states no terms | `MIT-pydicom-data.txt` |
| `chemistry` | ChEBI, the lite SDF file | CC BY 4.0 | `CC-BY-4.0.txt` |
| `stars` | The PPM star catalogue (Roeser and Bastian, 1991), in the binary format of the Harvard-Smithsonian Telescope Data Center | No terms stated | |
| `database` | The Chinook sample database | MIT. Copyright © 2008-2024 Luis Rocha | `MIT-chinook.txt` |

**Hidden files**, `task/tests/hidden/`, about 1 MiB each. `task/tests/targets.json` records how each one was cut (`source`).

| File | Source | Terms | Text in `task/licenses/` |
|---|---|---|---|
| `arm64` | `libcrypto.so.3` of the Ubuntu 24.04 `libssl3t64` package (OpenSSL 3.0.13), arm64 | Apache 2.0 | `Apache-2.0.txt`, `ubuntu-copyright-openssl.txt` |
| `audio` | archive.org item `MEGranHistoriadeAmor`, `AMOR.wav` | Marked public domain by its uploader | |
| `audio2` | archive.org item `LeviticusKJV`, a reading by Duane Alan Paulson (The Bible Reading) | Dedicated to the public domain by its creator | |
| `audio3` | archive.org item `Voyager1`, from NASA's audio collection | Public domain (Public Domain Mark) | |
| `chemistry` | PubChem compound records 60000 to 61499, in SDF | NCBI places no restrictions on use; submitters of the data may hold rights | `NCBI-PubChem-README.txt` |
| `chinese` | Project Gutenberg ebook 24264, 紅樓夢 | Public domain in the US | `Project-Gutenberg-License.txt` |
| `csv` | Our World in Data, `owid-co2-data.csv`, collected and documented by Pablo Rosado, Hannah Ritchie, Max Roser, Edouard Mathieu and Bobbie Macdonald | CC BY 4.0 | `CC-BY-4.0.txt` |
| `database` | The Northwind sample database, in the SQLite port by JP White | MIT, for the port and for Microsoft's SQL Server samples | `MIT-northwind-sqlite3.txt`, `MIT-microsoft-sql-server-samples.txt` |
| `dictionary` | Project Gutenberg ebook 38538, *Chambers's Twentieth Century Dictionary*, part 2 | Public domain in the US | `Project-Gutenberg-License.txt` |
| `dll` | SQLite 3.54.1 `sqlite3.dll` for Windows x86 | Public domain | |
| `dna` | NCBI RefSeq NC_000964.3, the genome of *Bacillus subtilis* 168 | NCBI places no restrictions on use | |
| `english` | Project Gutenberg ebook 5231, *The Way We Live Now* | Public domain in the US | `Project-Gutenberg-License.txt` |
| `floats` | NASA's FITS sample `FOCx38i0101t_c0f.fits`, a Hubble image from the MAST archive (NASA, STScI) | Public domain under MAST's data-use policy | |
| `json` | GH Archive, the hour 2024-01-15-12 of public GitHub events | No licence stated for the data. It holds account names and text written by GitHub users. | |
| `logs` | The web server log of NASA's Kennedy Space Center for July 1995, from the Internet Traffic Archive (collected by Jim Dumoulin, contributed by Martin Arlitt and Carey Williamson) | "The traces may be freely redistributed." It holds the clients' host names. | |
| `mri` | OpenNeuro ds000002, the T1-weighted image of sub-01 | PDDL 1.0 | `PDDL-1.0.txt` |
| `photo` | Wikimedia Commons, "Ares Tower Vienna from SW on 2015-07-10.png" by Robert F. Tobler, cropped | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) | `CC-BY-SA-4.0.txt` |
| `photo2` | Library of Congress, Prints & Photographs Division, photograph by Carol M. Highsmith, LC-DIG-highsm-66198, cropped | Public domain | |
| `photo3` | Library of Congress, Prints & Photographs Division, FSA/OWI Collection, LC-DIG-fsa-8b29516 (Dorothea Lange, 1936), cropped | No known restrictions | |
| `polish` | Fourteen Polish-language Project Gutenberg ebooks | Public domain in the US | `Project-Gutenberg-License.txt` |
| `programs` | Files of the Ubuntu 24.04 `libavcodec60` package (FFmpeg 6.1.1) | GPL 2 or later | `GPL-2.txt`, `ubuntu-copyright-ffmpeg.txt` |
| `source` | redis 7.2.5 source | BSD 3-Clause. Copyright © 2006-2020 Salvatore Sanfilippo | `BSD-3-Clause-redis.txt` |
| `stars` | Stellarium's star catalogue file `stars_2_0v0_17.cat`, built from Hipparcos and Gaia DR3 (credit: ESA, Gaia DPAC) | GPL 2 or later for Stellarium. ESA distributes Gaia data under [CC BY-NC 3.0 IGO](https://creativecommons.org/licenses/by-nc/3.0/igo/). | `GPL-2.txt` |
| `xml` | The PubMed baseline file `pubmed26n0001.xml`, courtesy of the U.S. National Library of Medicine | NLM's terms. This copy does not reflect the most current data available from NLM, and some abstracts may be protected by copyright. | `NLM-PubMed-terms.txt` |
| `xray` | pydicom-data `RG1_UNCI.dcm`, from the NEMA WG04 compression samples | MIT for the repository; the NEMA set states no terms | `MIT-pydicom-data.txt` |

**Reference solution**, `task/solution/compressor/paq8px/`: paq8px v217 by Matt Mahoney and the paq8px contributors, GPL 2 or later (`LICENSE` in that folder), with the zlib copy (zlib licence, in `zlib/zlib.h`) and the word lists it ships with.
