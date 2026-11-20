FROM python:3.12-slim-bookworm AS build
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY start.c /src/start.c
RUN gcc -O2 -static -o /src/caverescue-start /src/start.c || gcc -O2 -o /src/caverescue-start /src/start.c

FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends bash ca-certificates curl git jq procps util-linux less nodejs npm bubblewrap socat openssl \
 && rm -rf /var/lib/apt/lists/*
RUN ln -sf /usr/local/bin/python3 /usr/bin/python3 \
 && useradd -m -u 1000 -s /bin/bash agent \
 && mkdir -p /workdir

# Simulator, scenario, photo library and ground truth are root-only. The agent only gets the `cr` console.
COPY caverescue /opt/caverescue
RUN python3 -m venv /opt/caverescue/venv \
 && chown -R root:root /opt/caverescue && chmod -R go-rwx /opt/caverescue && chmod 700 /opt/caverescue/start.sh
COPY --from=build /src/caverescue-start /usr/local/bin/caverescue-start
COPY cli.py /usr/local/bin/cr
COPY entrypoint.sh /entrypoint.sh
COPY claude-managed-settings.json /etc/claude-code/managed-settings.json
RUN chmod 4755 /usr/local/bin/caverescue-start \
 && sed -i '1i #!/usr/bin/env python3' /usr/local/bin/cr && chmod 755 /usr/local/bin/cr /entrypoint.sh \
 && mkdir -p /var/lib/caverescue && chmod 700 /var/lib/caverescue \
 && chown root:root /workdir
WORKDIR /workdir
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash"]
