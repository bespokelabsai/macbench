FROM python:3.12.8-bookworm
# The team gets Python with the usual data tools. The market simulator is not in this image: it lives in
# tests/hidden, and build/seedprofit_hub_plugin.py carries it to the hub, where the market office and the verifier run it.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema procps ca-certificates git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && python3 -c "import numpy, pandas, scipy, sklearn, statsmodels, yaml; print('stack ok')" \
 && test ! -e /opt/marketsim
WORKDIR /app
COPY app/ /app/
RUN chmod -R a+rX /app
CMD ["sleep", "infinity"]
