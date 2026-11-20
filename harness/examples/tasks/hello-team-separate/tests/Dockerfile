# The verifier's own sandbox. Harbor builds it from the tests dir and does not upload the tests
# in separate mode, so they are baked in at /tests.
FROM python:3.12.15-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY test.sh verify.py /tests/
RUN chmod +x /tests/test.sh && mkdir -p /logs/verifier /logs/artifacts
WORKDIR /tests
