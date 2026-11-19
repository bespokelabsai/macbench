# Verifier image (separate verifier, no network). Same base and packages as the agents' image.
# Run ../environment/dataset/prepare.sh first: it fills hidden/ (the held-out data, a pristine copy of the agents'
# data and requirements.txt). checkpoints/ stays out of the image (.dockerignore).
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libxrender1 libxext6 procps git curl \
 && rm -rf /var/lib/apt/lists/*

COPY hidden/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && python -c "import numpy, scipy, pandas, pyarrow, sklearn, lightgbm, xgboost, torch, rdkit, joblib"

ENV CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0

# The agents' data; each run gets a fresh, writable copy at /app/data.
COPY hidden/train_spectra.parquet hidden/candidates.parquet hidden/README.md /opt/task/data/
# Grader and held-out data: root-only.
COPY test.sh verify.py standardize.py /tests/
COPY hidden/hidden_spectra.jsonl hidden/hidden_labels.jsonl hidden/manifest.json hidden/SOURCE /tests/data/
RUN chmod -R a+rX,go-w /opt/task \
 && chown -R root:root /tests && chmod -R go-rwx /tests && chmod 700 /tests/test.sh \
 && mkdir -p /logs/verifier /app /srv

WORKDIR /app
CMD ["sleep", "infinity"]
