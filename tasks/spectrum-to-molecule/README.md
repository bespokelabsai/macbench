# SPECTRUM TO MOLECULE

A multi-agent task: build an offline program that identifies the compound behind an MS/MS spectrum. For each
spectrum, `./predict <spectra.jsonl> <out.jsonl>` ranks up to 20 structures from a 4.5 M-molecule candidate
collection, most likely first. The agents get 206,993 training spectra of 26,195 known compounds and the candidate
collection, and no internet access: their boxes reach only the harness's hub, which relays their model calls.

The program is graded on 6,299 hidden spectra of 1,000 molecules that are structurally far from the training set (no
training molecule within MCES distance 10 of any of them), so looking up a near neighbour in the library is not
enough. The data is built from [MassSpecGym](https://huggingface.co/datasets/roman-bushuiev/MassSpecGym) v1.5 by the
scripts in `task/environment/dataset/`.

## How it works

- **One shared repository.** The team's deliverable is whatever is on `main` at the end: the `predict` program and
  any trained models it needs. The agents work in their own boxes and share code through the harness's git remote
  and message board.
- **A real research problem.** There is no known recipe that scores well: spectrum-to-structure retrieval on novel
  scaffolds is open-ended, so the work splits naturally into features, fragmentation models, candidate filtering,
  rankers and evaluation.
- **A long day.** Runs last 4 hours of agent time. Each agent box has 3 CPUs and 6 GB of memory, so training and
  evaluation have to be planned and shared.
- **The same problem at every team size.** One instruction for k = 1, 2, 4 or 8 agents.
- **Hidden grading.** The held-out spectra and labels exist only in the verifier image, which grades `main` in a
  separate sandbox with no network.

## Grading

`task/tests/verify.py` takes the team's `main`, runs `./predict` on the hidden spectra in 5 batches of about 1,300 as
an unprivileged user (8 CPUs, 16 GB, no network, one hour per batch) and scores hit@1 per spectrum: 1 if the
top-ranked structure has the true InChIKey (first 14 characters) after the dataset's own standardisation
(`task/tests/standardize.py`), else 0. The reward is the mean per molecule, then over the 1,000 molecules. A batch
that crashes or times out still counts the valid lines it wrote; spectra with no answer score 0. `details.json` adds
hit@5, hit@20, MRR, bootstrap confidence intervals and per-batch run times.

There is no oracle at 1.0: the problem is open-ended. `task/solution/` is an honest reference, a simple
library-matching ranker, that scores 0.133.

## How to run

You need Docker, `curl`, Python 3.12 and git. Set up once, from this folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
git clone https://github.com/bespokelabsai/multi-agent-coordination-bench ~/mac-bench
git -C ~/mac-bench checkout cd6c6ce43168d531033699d1c3f3aefcbae791b9
pip install -e "$HOME/mac-bench/harness[docker]"
echo "ANTHROPIC_API_KEY=..." > .env
```

This installs [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-coordination-bench/tree/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness) (this repository's `harness/`) at the commit the task was
tested with, and Harbor with it. The clone can live anywhere, but keep it: the harness builds its hub from it. All
commands below run from this folder.

**Build the dataset.** The data does not ship; these scripts download MassSpecGym from Hugging Face at a pinned
revision and rebuild the exact files the task uses:

```bash
task/environment/dataset/download.sh   # 5 files, 3.3 GB, each checked against its sha256
task/environment/dataset/run_all.sh    # steps s01-s08 in a pinned Python container; ends by checking SOURCE
task/environment/dataset/prepare.sh    # places the data into task/environment/app/data and task/tests/hidden
```

- The build takes about 15 minutes on a 32-core machine, plus the download (each step runs in a container capped at
  10 CPUs and 20 GB of memory: `MS2_DOCKER_CPUS`, `MS2_DOCKER_MEMORY`). It needs about 6 GB of disk: the download
  (3.3 GB), the intermediates (0.8 GB), the dataset (230 MB) and the 1.8 GB runner image.
- Everything goes to `build/` in this folder, next to `task/` (so Harbor never hashes or uploads it), or to
  `$MS2_BUILD_DIR` if set (use the same value for all three commands). `run_all.sh` fails unless the five task files
  match the sha256 in `task/environment/dataset/SOURCE`.
- `prepare.sh` hard-links (or copies) the training spectra and candidates into `task/environment/app/data/` (the
  agents' `/app/data`), and the hidden spectra and labels, with a pristine copy of the agents' data, into
  `task/tests/hidden/` (the verifier image only; the hidden data lives nowhere else). It writes their sha256 to
  `task/tests/hidden/SOURCE`. Afterwards `build/` can be deleted.

### Multi-agent harness

A team of `K` agents (1, 2, 4 or 8), with the settings of our reference runs. Put `DAYTONA_API_KEY` in `.env` too:
the runs used the harness's Daytona backend, which keeps the agents' boxes off the internet (they reach only the
hub; the model calls go through it).

```bash
K=4
COMMON="-e multi_agent_harness.harbor.environment:TeamEnvironment --ek backend=daytona --ek n_agents=$K
  --ek budget_usd=10000 --ek board_plugins=task/environment/hub/keepalive_plugin.py
  -a multi_agent_harness.harbor.agent:TeamAgent --ak orchestration=task/environment/hub/keepalive.py
  --ak timeout_sec=14580 --agent-timeout-multiplier 1.25 --env-file .env"

# Opus 5.5 with Claude Code, max effort (on a Claude plan: CLAUDE_CODE_OAUTH_TOKEN in .env)
MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0 harbor run -p task $COMMON \
  --ak harness=claude-code --ak model=claude-opus-5-5 --ek model_upstream=subscription

# GPT-6.1 Sol with Codex, max effort (on a ChatGPT plan: `codex login --device-auth` once on this machine)
harbor run -p task $COMMON --ak harness=codex --ak model=gpt-6.1-sol --ek model_upstream=subscription

# Gemini 4 Argon with Antigravity, high thinking (GEMINI_API_KEY in .env)
harbor run -p task $COMMON --ak harness=antigravity --ak model=gemini-4-argon-high --ek model_upstream=direct
```

- k = 1 is the same command with `K=1`. With an API key instead of a plan (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`),
  leave out `--ek model_upstream=subscription`; `budget_usd` then caps the team's model spend.
- Effort: the agent image sets it for every session (`CLAUDE_CODE_EFFORT_LEVEL=max`, and Codex's
  `model_reasoning_effort = "max"` in `/etc/codex/config.toml`; see `task/environment/Dockerfile`). Argon's thinking
  level is in its model name.
- `environment/hub/keepalive.py` gives every agent the full task, as the harness's `fixed` does, and keeps the team
  working until the deadline: it wakes an agent that has ended its session when a board message arrives for it or
  it has been idle, and stops everyone 180 s before `timeout_sec`, so the agents get 4 hours. Its settings are in
  its docstring (`KEEPALIVE_*` environment variables). `environment/hub/keepalive_plugin.py` is its board plugin; both run on the host and
  the hub, never in the agents' image.
  `--agent-timeout-multiplier 1.25` leaves Harbor room for the harness's end of run.
- Every box, the hub included, gets the task's size: 3 CPUs and 6 GB. The verifier gets 8 CPUs and 16 GB.
- `MULTI_AGENT_HARNESS_CLAUDE_ENV_SCRUB=0` runs Claude Code without its shell sandbox, which cannot start in these
  boxes (the agents' shell commands can then read the plan token).
- `--ek backend=docker` runs everything on your own Docker host instead, but the harness cannot keep the agents off
  the internet there (it warns), so those runs are not comparable with ours. The [harness's README](https://github.com/bespokelabsai/multi-agent-coordination-bench/blob/cd6c6ce43168d531033699d1c3f3aefcbae791b9/harness/README.md) describes its
  backends.

**Grades during the run.** The score is the final verifier's, at 4 hours. Our results also graded the team's `main`
at 0.5, 1, 2 and 3 hours. The harness's graded marks (`--ak grade_at`) need a verifier that runs on the hub, and
this task's verifier runs in its own sandbox, so those marks are graded after the run with `task/tests/checkpoints/`:

```bash
task/tests/checkpoints/archive_checkpoints.sh jobs/<job name> &     # next to `harbor run ... -o jobs --job-name <job name>`
task/tests/checkpoints/grade_checkpoints.sh jobs/<job name>          # after the run
```

- The harness checkpoints the team every 15 minutes (`--ak checkpoint_every_min`) but keeps only the last few, so
  `archive_checkpoints.sh` copies each one into `jobs/<job name>.ckpt-archive/` as it appears. It exits by itself
  once the run has been graded.
- `grade_checkpoints.sh` rebuilds the team's repository from the checkpoints, finds the commit `main` pointed to at
  each mark (0.5, 1, 2, 3 and 4 hours of agent time; `--marks` to change them) from the checkpoints and the agents'
  `git push` times, grades each with the verifier image built from `task/tests/` (8 CPUs, 16 GB, no network), as the
  final verifier does, and prints one row per mark. `--dry-run` only finds the commits. `MS2_PREDICT_TIMEOUT_SEC`
  shortens each graded batch for a quick test.

### Harbor

One agent, with plain Harbor:

```bash
harbor run -p task -e daytona -a claude-code -m anthropic/claude-opus-5-5 --ak reasoning_effort=max \
  --allow-agent-host api.anthropic.com --env-file .env
```

The agent's box has no network except its model API (`--allow-agent-host`; use your provider's API host for other
agents). The agent works in `/app`, with the data in `/app/data`; the verifier then grades `/app` in its own sandbox
with no network. The score is in the trial's `verifier/reward.txt`, and `verifier/details.json` has the full
metrics. Our reference results are all multi-agent harness runs (k = 1 included).

On Harbor's Docker backend (no `-e daytona`) this does not work: its allow-list also blocks the box's name lookups
(Docker's resolver forwards them over UDP, which the allow-list rejects), so the agent cannot reach its API. For a
local Docker run, set `network_mode = "public"` under `[agent]` in `task/task.toml`; the agent then has internet
access, unlike in our runs.

- `harbor run -p task -a oracle` runs `solution/solve.sh`: it installs the reference ranker
  (`solution/reference/ms2ref.py`: candidates within the precursor tolerance, scored by spectral similarity to
  training spectra times structural similarity to their compounds) as `./predict`, which scores 0.133 hit@1. There
  is no oracle at 1.0. The run takes about 7 minutes: 3 for `solve.sh` to index the candidates, 3 for the grading.
- For a quick look at a real agent, add `--agent-timeout-multiplier 0.01` (about 2.5 minutes instead of 4 hours)
  and `--ve MS2_PREDICT_TIMEOUT_SEC=300`, which limits each of the 5 graded batches to 5 minutes instead of an
  hour. The same `--ve` works for a short team run.

## Contents

| Path | What it is |
|---|---|
| `task/task.toml`, `task/instruction.md` | The Harbor task's configuration and the agents' instruction |
| `task/environment/` | The agents' (and hub's) image: `Dockerfile`, `requirements.txt`, and `.dockerignore`, which keeps `hub/` and `dataset/` out of it |
| `task/environment/app/` | Becomes `/app`, the agents' starting repository: `data/README.md` describes `/app/data`; `dataset/prepare.sh` puts the data next to it (git-ignored) |
| `task/environment/hub/` | Host and hub only, never in the image: the team's orchestration for the multi-agent harness (`keepalive.py`) and its board plugin (`keepalive_plugin.py`) |
| `task/environment/dataset/` | The dataset build: `download.sh`, `run_all.sh` and the steps `s01`-`s08` it runs (standardisation, the MCES graph, the hidden / dev / train split, the candidate collection, the task files and their checks) through `drun.sh` in the runner image (`Dockerfile.runner`, `requirements-runner.txt`); `SOURCE`, the sha256 of the five task files; and `prepare.sh`, which places the built data |
| `task/tests/` | The verifier image and grader: `Dockerfile`, `test.sh`, `verify.py`, `standardize.py`; `hidden/` (git-ignored, filled by `prepare.sh`) holds the held-out spectra and labels |
| `task/tests/checkpoints/` | Grading the team's `main` at 0.5, 1, 2, 3 and 4 hours after a run: `archive_checkpoints.sh`, `grade_checkpoints.sh` and its helper `grade_checkpoints.py` (kept out of the verifier image) |
| `task/solution/` | `solve.sh`, which installs the reference ranker in `reference/` as `./predict` (0.133 hit@1) |

## Credits

Spectra, structures and candidate pools: MassSpecGym v1.5 (Bushuiev et al., "MassSpecGym: A benchmark for the
discovery and identification of molecules", NeurIPS 2024 Datasets and Benchmarks), from
[Hugging Face](https://huggingface.co/datasets/roman-bushuiev/MassSpecGym) at revision
`018a9542378725cf5620423f930608730c36dd8c`, under the MIT License. The build re-splits the data and standardises
every structure with RDKit; the derived files are not distributed.
