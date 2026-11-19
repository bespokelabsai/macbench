# Agent image (also the hub's). Python 3.13 with a general CPU ML stack. No RNA-specific packages
# (no folding tools such as ViennaRNA, RNAstructure, LinearFold, arnie or forgi, and no pretrained
# RNA models). Run dataset/prepare.sh first: it puts the dataset into app/data/. hub/ and dataset/ are not in the
# image (.dockerignore).
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

# The same system packages, in the same versions, in both images (environment/ and tests/Dockerfile;
# keep the two RUN lines identical). The base is pinned by digest (Python 3.13.18, Debian 12.15) too.
# build-essential (gcc/g++ 12, make, libc headers): agents' programs may compile code at first run, and
# the verifier must have the same compiler. nodejs + npm: the agent harness needs them; with them in
# the image its installer adds nothing, so the agents' machine is exactly this image (without them it
# would apt-install ~600 packages, gcc and g++ among them, that the verifier would lack). xz-utils and
# bzip2 for tar -J / -j. libgomp1 for lightgbm/xgboost/torch OpenMP; git, curl and procps for the harness.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 git curl procps xz-utils bzip2 \
      build-essential=12.9 gcc-12=12.2.0-14+deb12u1 g++-12=12.2.0-14+deb12u1 make=4.3-4.1 nodejs npm \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && python -c "import numpy, scipy, pandas, pyarrow, sklearn, lightgbm, xgboost, torch, numba, joblib"

# Headless Claude Code otherwise stops background sub-agents 10 min after the lead's last turn.
ENV CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
# Claude Code's reasoning effort for every agent session (beats --effort and settings; Codex ignores it).
ENV CLAUDE_CODE_EFFORT_LEVEL=max

# The team repo is /app: app/ here (data/README.md, and the dataset that dataset/prepare.sh puts into
# app/data/). data/ stays out of git (it is in place wherever the program runs).
WORKDIR /app
COPY app/ /app/
RUN printf '/data/\n' > /app/.gitignore && chmod 664 /app/.gitignore && chmod -R a+rX /app/data

CMD ["sleep", "infinity"]
