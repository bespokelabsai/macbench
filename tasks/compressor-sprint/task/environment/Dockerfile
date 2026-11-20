FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
# Python 3.12 and a C/C++ toolchain. No compression tools beyond what Ubuntu and Python ship.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 build-essential git curl ca-certificates procps xz-utils bzip2 tmux asciinema \
    && rm -rf /var/lib/apt/lists/*
# Public data, read-only and outside the team repo.
COPY data/ /data/
COPY fetch_data.py /tmp/fetch_data.py
RUN python3 /tmp/fetch_data.py && rm /tmp/fetch_data.py
RUN chmod -R a+rX,a-w /data
# The team repo is seeded from /app.
COPY app/ /app/
WORKDIR /app
