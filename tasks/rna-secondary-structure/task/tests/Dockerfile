# Verifier image (separate verifier, no network). Same base and system packages as the agents' image
# (environment/Dockerfile), so a program that runs on the agents' machines runs here.
# Run environment/dataset/prepare.sh first: it puts the dataset (hidden/) and requirements.txt here.
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

ENV CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0

# Everything in tests/ (grader, scorer, held-out data) goes to /tests, root-only (Harbor's separate
# verifier needs `COPY . /tests/`). The agents' data (train.parquet, sequences.fa.gz, README.md) is also put
# at /opt/rna/data, world-readable; each batch gets a fresh, writable copy of it at /app/data.
COPY . /tests/
RUN mkdir -p /opt/rna/data \
 && cp /tests/hidden/train.parquet /tests/hidden/sequences.fa.gz /tests/hidden/README.md /opt/rna/data/ \
 && chmod -R a+rX,go-w /opt/rna \
 && rm -f /tests/requirements.txt /tests/Dockerfile \
 && chown -R root:root /tests && chmod -R go-rwx /tests && chmod 700 /tests/test.sh \
 && mkdir -p /logs/verifier/artifacts /app /srv

WORKDIR /app
CMD ["sleep", "infinity"]
