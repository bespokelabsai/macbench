# Agent image (also the hub's). Python 3.13 with a CPU ML stack; no mass-spec libraries.
# Run dataset/prepare.sh first: it puts the agents' data into app/data/. hub/ and dataset/ stay out of the
# image (.dockerignore).
FROM python:3.13-slim-bookworm

# libgomp1 for lightgbm/xgboost/torch OpenMP; git and curl so the harness needn't install them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libxrender1 libxext6 git curl procps \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && python -c "import numpy, scipy, pandas, pyarrow, sklearn, lightgbm, xgboost, torch, rdkit, joblib"

# Headless Claude Code otherwise stops background sub-agents 10 min after the lead's last turn.
ENV CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
# Claude Code's reasoning effort for every agent session (beats --effort and settings; Codex ignores it).
ENV CLAUDE_CODE_EFFORT_LEVEL=max
# Codex's reasoning effort for every agent session: the system config layer, under the harness's
# $CODEX_HOME/config.toml, which doesn't set it. "max" is Codex's top pure-effort level ("ultra" adds
# Codex's own task delegation). Claude Code ignores it.
RUN mkdir -p /etc/codex && printf 'model_reasoning_effort = "max"\n' > /etc/codex/config.toml \
 && chmod 644 /etc/codex/config.toml

# The team repo is /app, from app/. data/ is the dataset; it stays out of git (it is in place wherever
# the program runs).
WORKDIR /app
COPY app/ /app/
RUN printf '/data/\n' > /app/.gitignore && chmod -R a+rX /app/data

CMD ["sleep", "infinity"]
