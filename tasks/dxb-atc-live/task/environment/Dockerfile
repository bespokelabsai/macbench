FROM python:3.12.8-bookworm
# The simulator: BlueSky (TU Delft, GPL-3.0) flies the aircraft with the OpenAP performance model (TU Delft, LGPL-3.0);
# atcsim (environment/engine) adds the airport, the ground, the pilots, the radio and the safety rules. BlueSky and OpenAP
# are installed in this image. /app holds only docs/; the simulator engine and the live day are not in /app. The hub runs
# the live day with its own copy (hub/hub_bundle.py); this image holds a root-only copy for runs without the harness.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema jq bubblewrap socat && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "bluesky-simulator==1.1.1" "openap==2.6.2" "numpy<2.3"

# The agent works as an unprivileged user. BlueSky keeps its settings in root's home (the live day's server runs as root).
RUN useradd -m -u 1000 -s /bin/bash agent
RUN HOME=/root python3 -c "import io, contextlib, bluesky as bs; f=io.StringIO(); c=contextlib.redirect_stdout(f); c.__enter__(); bs.init(mode='sim', detached=True)"
WORKDIR /app
COPY data/ /app/
RUN chown -R agent:agent /app && chmod -R u+rwX,go+rX /app

# The live day: its schedule, weather, events and server, with their own copy of the engine, readable by root only.
# The root-run entrypoint serves it on 127.0.0.1:8091. Its state is root-only too, so only the server can write the
# record the verifier grades. On the multi-agent harness the hub serves the day instead (this block is removed there).
# >>> live day
COPY hub/server /opt/dxb-atc
COPY engine/atcsim /opt/dxb-atc/atcsim
RUN chown -R root:root /opt/dxb-atc && chmod -R go-rwx /opt/dxb-atc
ENV MULTI_AGENT_HARNESS_BOARD_URL=http://127.0.0.1:8091 MULTI_AGENT_HARNESS_BOARD_TOKEN=controller
# <<< live day
RUN mkdir -p /var/lib/dxb-atc && chmod 700 /var/lib/dxb-atc
COPY entry.sh /usr/local/sbin/dxb-atc-entry
RUN chown root:root /usr/local/sbin/dxb-atc-entry && chmod 700 /usr/local/sbin/dxb-atc-entry \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null; true
ENTRYPOINT ["/usr/local/sbin/dxb-atc-entry"]
CMD ["sleep", "infinity"]
