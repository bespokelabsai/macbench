FROM debian:trixie
# 0 A.D. Release 28 "Boiorix" from trixie-backports, pinned. If backports moves
# on, fetch the same versions from snapshot.debian.org.
RUN echo "deb http://deb.debian.org/debian trixie-backports main" > /etc/apt/sources.list.d/backports.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends -t trixie-backports \
      0ad=0.28.0-3~bpo13+1 0ad-data=0.28.0-1~bpo13+1 0ad-data-common=0.28.0-1~bpo13+1 curl \
 && apt-get install -y --no-install-recommends python3 bash nodejs npm procps unzip ca-certificates \
 && apt-get install -y --no-install-recommends tmux asciinema \
 && rm -rf /var/lib/apt/lists/*
# tmux and asciinema back terminal-driven agent harnesses, which run the agent
# through a tmux session in this container; curl, bash, nodejs, npm and procps
# let in-container agents (e.g. Claude Code) install.
# The game refuses to run as root; legion.py drops to this user when it runs as root (on the hub).
RUN useradd -m -s /bin/bash legion
# The agent works as this user ([agent] user in task.toml). It owns /app,
# where the harness makes its clone of the team repo; the game then runs as the agent itself.
RUN useradd -m -s /bin/bash agent
# Petra is removed from this image. The hub and every agent sandbox are built from it, and agents
# never get Petra: every match against Petra is played on the hub by the practice office
# (practice_plugin.py), from one pool shared by the whole team, with Petra from the hub bundle
# (environment/hub/make_hub_plugin.py). The game itself stays, so LEGION AIs can play each other here (legion.py spar).
# zip -d keeps every other entry byte for byte (the engine's archive reader rejects a re-written zip).
RUN apt-get update && apt-get install -y --no-install-recommends zip \
 && zip -q -d /usr/share/games/0ad/mods/public/public.zip 'simulation/ai/petra/*' \
 && ! unzip -l /usr/share/games/0ad/mods/public/public.zip | grep -qi petra \
 && apt-get purge -y zip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --chown=agent:agent app/ /app/
# Readable copy of the game's scripts: the AI API, templates, map scripts (Petra is not included).
RUN mkdir -p /app/reference \
 && cd /app/reference \
 && unzip -q /usr/share/games/0ad/mods/public/public.zip 'simulation/*' 'globalscripts/*' 'maps/random/*' 'maps/scripts/*' \
 && chmod -R a+rX /app && chown agent:agent /app
# No Petra anywhere in the image, /app included.
RUN ! find / -xdev -ipath '*petra*' -not -path '/proc/*' 2>/dev/null | grep -q . || { find / -xdev -ipath '*petra*' -not -path '/proc/*'; exit 1; }
CMD ["sleep", "infinity"]
