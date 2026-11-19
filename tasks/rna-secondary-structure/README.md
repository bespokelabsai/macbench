# RNA SECONDARY STRUCTURE

A team of agents builds an offline program that predicts an RNA's secondary structure from its sequence alone:
`./predict <in.jsonl> <out.jsonl>` writes a dot-bracket string per sequence, pseudoknots allowed. The agents get
about 57,800 RNAs with known structures (tagged with their Rfam family and clan) and about 285,000 unlabelled RNA
sequences that the program may search at prediction time. The agents have no internet access.

The program is graded on 1,496 hidden RNAs from 261 Rfam families that are held out of the training data. The score
is exact base-pair F1 per RNA, averaged within each family and then across families. The program runs in a separate
verifier, in 8 batches. The data is built from Rfam 15.1 by the scripts in `task/environment/dataset/`.

## How it works

- **Held-out families, independent labels.** 70% of eligible Rfam units (whole clans, or families in no clan) are
  held out, and no RNA class is above 10% of the held-out families. No held-out family has a structure that Rfam
  made with a single-sequence folder (RNAfold, mfold, ...) or by folding an alignment (RNAalifold, ...). With a
  homolog database, a team could otherwise re-run the labeller. Leakage between the splits (BLAST and
  covariance-model hits) is checked until clean.
- **A homolog database at prediction time.** `data/sequences.fa.gz` holds 284,731 Rfam sequences with no labels
  (near-copies of the hidden sequences removed, at most 200 per family). An off-the-shelf comparative pipeline on it
  (BLASTN, MAFFT, RNAalifold) scores 0.560, against 0.535 for the best single-sequence tool (IPknot) and 0.525 for
  ViennaRNA's centroid. None of these tools is in the agents' image.
- **One sequence per family per batch.** The verifier runs `./predict` on 8 batches, each holding at most one
  sequence per family. It runs as an unprivileged user with no network, on a 4 CPU / 8 GB machine, with a fresh copy
  of `data/` for each batch.
- **The same task at every team size.** The instruction is identical for every k. The agents share a git
  repository and a message board, and whatever is on `main` 4 hours after they start is graded.

## Grading

`task/tests/verify.py` runs the team's `main` and scores it with `task/tests/rna_score.py`. For each hidden RNA it
computes the F1 of the predicted base pairs against the label's pairs. Positions must match exactly. There are four
bracket types, so crossing pairs can be written. The F1 values are averaged within each family, then over the 261
families. An invalid or missing prediction counts as no pairs (F1 0).

The labels are Rfam's consensus structure projected onto each seed sequence (canonical pairs only, hairpin loops of
at least 3 nt). The reward is in the trial's `verifier/reward.txt`, and `details.json` next to it has the full
metrics, the batches and the program's logs.

## How to run

You need Docker, `curl`, Python 3.12, and about 5 GB of free disk for the dataset build. The build's heavy steps
run in a container capped at 10 CPUs and 20 GB of RAM. Every agent sandbox, the hub and the verifier get 4 CPUs
and 8 GB of RAM.

Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout f834aee31c6148f63327d0ddb1a72cdc5074ed9b
pip install -e "$HOME/mac-bench/harness[docker]"
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/f834aee31c6148f63327d0ddb1a72cdc5074ed9b/harness) (this repository's `harness/`) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder.

Put the keys in `.env` here: `DAYTONA_API_KEY` for the Daytona sandboxes, and the model key: `ANTHROPIC_API_KEY`
(or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, a Claude plan), `OPENAI_API_KEY` (or a ChatGPT login on
the host, `codex login --device-auth`), or `GEMINI_API_KEY`.

**Build the data (once).** The dataset is not shipped; these scripts build it from the public Rfam files:

```bash
task/environment/dataset/run_all.sh   # downloads Rfam 15.1, builds the dataset into build/dataset (about 30 min)
task/environment/dataset/prepare.sh   # copies it into task/environment/app/data and task/tests/hidden
```

- `run_all.sh` downloads about 620 MB from the Rfam FTP site (Rfam 15.1, and the Rfam 15.0 family table to find
  the families new in 15.1) and checks every file against `rfam.sha256`. It then fetches BLAST+ 2.16.0, Infernal
  1.1.5 and a few Python wheels, checks them against `docker/vendor.sha256`, and builds the build image from
  `docker/Dockerfile` (all in `task/environment/dataset/`).
- The steps then run offline in that container (`drun.sh`): project each family's consensus structure onto its
  seed sequences, remove near-duplicates, group homologous families into units, draw the held-out units and check
  leakage until no unit moves, write the dataset, validate it, and build the homolog database.
- At the end it compares the five dataset files with the sha256 values in `SOURCE` and fails if any differs. The
  training data, sequence database and hidden set are byte-identical to those of our reference runs.
- Everything goes under `build/` (outside `task/`), or under `$RNA_DATASET_BUILD` if you set it: about 4.2 GB,
  of which 360 MB are the build tools in `build/image/`. Only `build/dataset/` (18 MB) is needed afterwards. The
  build took about 33 minutes on a 32-core machine, most of it in the leakage checks. A rerun keeps the downloads
  and the sequence pool. `prepare.sh /path/to/dataset` takes a dataset from elsewhere.
- `prepare.sh` puts the agents' files (`train.parquet`, `sequences.fa.gz`) into `task/environment/app/data/`, and
  those and the hidden set into `task/tests/hidden/` (the verifier image only). Rerun it, and rebuild the images,
  after any change to the dataset.

### Multi-agent harness

Our reference runs: a team of `K` agents (1, 2, 4 or 8) on Daytona sandboxes, 4 hours. The agents reach only the
hub (message board, model proxy, git) while they work: `[agent] network_mode = "no-network"` in `task.toml`, which
the harness enforces on Daytona. The three models, with the settings of our runs:

```bash
K=4
TEAM="-e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=daytona --ek n_agents=$K
  --ek board_plugins=task/environment/hub/keepalive_plugin.py
  -a multi_agent_harness.harbor.agent:TeamAgent --ak orchestration=task/environment/hub/keepalive.py
  --ak timeout_sec=14580 --ak checkpoint_every_min=15
  --agent-timeout-multiplier 1.25 --agent-setup-timeout-multiplier 3 --env-file .env"

# Opus 5.5, Claude Code, max effort (a Claude plan: CLAUDE_CODE_OAUTH_TOKEN and model_upstream=subscription)
export MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0
harbor run -p task $TEAM --ek budget_usd=2500 --ek model_upstream=subscription \
  --ak harness=claude-code --ak model=claude-opus-5-5 --ak effort=max

# GPT-6.1 Sol, Codex, max effort (a ChatGPT login on the host and model_upstream=subscription)
harbor run -p task $TEAM --ek budget_usd=10000 --ek model_upstream=subscription \
  --ak harness=codex --ak model=gpt-6.1-sol --ak effort=max

# Gemini 4 Argon, Antigravity, high thinking (GEMINI_API_KEY)
harbor run -p task $TEAM --ek budget_usd=2500 --ek model_upstream=direct \
  --ak harness=antigravity --ak model=gemini-4-argon-high
```

- With an API key instead of a plan, drop `--ek model_upstream=subscription`; `budget_usd` then caps the team's
  model spend. With a plan, `MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0` runs Claude Code without its shell sandbox,
  which cannot start in these boxes (the agents' shell commands can then read the plan token).
- Opus's max effort also comes from `CLAUDE_CODE_EFFORT_LEVEL=max` in the agents' image. Our Sol runs set Codex's
  `model_reasoning_effort = "max"` in a config file in the image; the pinned harness passes the same setting with
  `--ak effort=max`. Antigravity takes its thinking level from the model name.
- Every k, k = 1 included, uses `task/environment/hub/keepalive.py`: k peers with the same prompt. An agent that
  ends its CLI session before the time is up is woken again, on a board message or after 10 minutes idle, until 4
  hours of agent time have passed. `--ak timeout_sec=14580` is 4 hours plus the strategy's 180 s end margin.
  Harbor's own limit is the task's 14,400 s times 1.25, which leaves room for the end of the run. The strategy's
  settings are the `KEEPALIVE_*` environment variables listed in `keepalive.py`.
- The verifier runs in its own sandbox built from `task/tests/Dockerfile`, with no network. The hidden set is
  only in that image, never in the agents' image or on the hub.
- **Docker.** `--ek backend=docker` runs the same team on your own Docker host, without `DAYTONA_API_KEY`. The
  harness cannot keep the agents off the internet there (it warns that the task's `no-network` is not enforced),
  so those runs are not comparable with ours.

**Intermediate scores.** Our runs were graded at 0.5, 1, 2, 3 and 4 hours of agent time; the 4-hour grade is the
run's `verifier/reward.txt`. The earlier ones come from the run's 15-minute checkpoints. The harness keeps only the
last few, so start the archiver next to the run, with the same job dir (`-o jobs --job-name NAME` on the
`harbor run`):

```bash
task/tests/checkpoints/archive_checkpoints.sh jobs/NAME &    # copies each checkpoint to jobs/NAME.ckpt-archive/
```

After the run, grade what was on `main` at each checkpoint time:

```bash
task/tests/checkpoints/grade_checkpoints.sh jobs/NAME        # a job dir or its trial dir
```

`grade_checkpoints.sh` reconstructs the team's `main` at 0.5, 1, 2, 3 and 4 hours (`--marks` to change them),
grades each commit with the verifier image as the final verifier does (no network, 4 CPUs, 8 GB; the image is
built from `task/tests/` if missing), and prints one row per checkpoint: the time, the commit, whether the commit
is certain, and the reward. Results go to `jobs/NAME.graded-checkpoints/`. Main is known exactly at each
harness checkpoint; between two checkpoints a new commit is placed on `main` at the agents' `git push` (from the
Claude Code session logs), else at the next checkpoint (details in `grade_checkpoints.py`). A time before the
first `./predict` on `main` scores 0. The harness's live graded marks (`--ak grade_at`) need a verifier that runs
on the hub, so they do not apply to this task.

### Harbor

One agent, with plain Harbor, on Daytona:

```bash
harbor run -p task -e daytona -a claude-code -m anthropic/claude-opus-5-5 --ak reasoning_effort=max \
  --allow-agent-host api.anthropic.com --env-file .env
```

- The agent phase has no network (`task.toml`); `--allow-agent-host` lets the agent reach its model API and
  nothing else. For another agent, allow its API host instead.
- On Harbor's Docker backend (no `-e daytona`) this does not work: its allow-list also blocks the box's name
  lookups (Docker's resolver forwards them over UDP, which the allow-list rejects), so the agent cannot reach its
  API. For a local Docker run, set `network_mode = "public"` under `[agent]` in `task/task.toml`; the agent then
  has internet access, unlike in our runs.
- The agent works in `/app` of the agents' image for 4 hours. Harbor then copies `/app` into the separate
  verifier, and the score is in the trial's `verifier/reward.txt`. Our reference runs are team runs (k = 1 to 8)
  with the harness; plain Harbor is the simplest way to try the task.
- `harbor run -p task -a oracle` runs the reference solution: `task/solution/solve.sh` installs a greedy-stem
  predictor (`task/solution/reference/rnaref.py`, standard library only) as `./predict`. It scores 0.329. There is
  no 1.0 oracle: the hidden labels exist only in `task/tests/hidden/` and the verifier image.
- For a quick look at a real agent, add `--agent-timeout-multiplier 0.05` (12 minutes instead of 4 hours). Grading
  a fast program takes a few minutes; the verifier allows up to an hour per batch.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The Harbor task |
| `task/environment/` | The agents' image: `Dockerfile`, `requirements.txt` (Python 3.13 with a CPU ML stack, no RNA tools), `.dockerignore` (only `app/` goes in) |
| `task/environment/app/` | The agents' starting `/app`: `data/README.md`, the description of the data; `prepare.sh` adds the data next to it |
| `task/environment/hub/` | Hub-only files, never in the image: the multi-agent harness orchestration (`keepalive.py`) and its message-board plugin |
| `task/environment/dataset/` | The dataset build: `run_all.sh` and its numbered steps, the build image (`docker/`), `prepare.sh`, and checksums of the Rfam downloads (`rfam.sha256`), of the build tools (`docker/vendor.sha256`) and of the five dataset files (`SOURCE`) |
| `task/tests/` | The verifier: `test.sh`, `verify.py` (batches, sandboxing, scoring), `rna_score.py` (the scorer) and its tests, the verifier image; `prepare.sh` puts the hidden set into `hidden/` |
| `task/tests/checkpoints/` | Intermediate scores: `archive_checkpoints.sh` (keep every checkpoint during a run) and `grade_checkpoints.sh` (grade `main` at each checkpoint time) |
| `task/solution/` | `solve.sh` and `reference/rnaref.py`: the reference solution, greedy stems (0.329) |

## Credits

Data: [Rfam](https://rfam.org) 15.1 (and the Rfam 15.0 family table), from the European Bioinformatics Institute,
under the Creative Commons Zero (CC0) licence. Rfam asks that you cite: N. Ontiveros-Palacios et al., "Rfam 15: RNA
families database in 2025", Nucleic Acids Research, 2025. The dataset is built from the Rfam seed alignments,
covariance models, family and clan tables, and the Rfam sequence files.

Build tools (downloaded by `task/environment/dataset/docker/fetch_vendor.sh`, not shipped): NCBI
[BLAST+](https://blast.ncbi.nlm.nih.gov) 2.16.0 (public domain), [Infernal](http://eddylab.org/infernal/) 1.1.5
(BSD licence), and numpy, pandas and pyarrow (BSD and Apache 2.0 licences).
