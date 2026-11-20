FROM python:3.12.8-bookworm
# The official, unmodified Mindustry v160.7 server and a JDK. The hub runs the game with
# them; the hidden bridge is compiled there at start (environment/hub/make_hub_plugin.py). Agents get the same public jar and nothing else of the game.
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jdk-headless tmux asciinema \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/mindustry \
 && curl -fsSL --retry 5 -o /opt/mindustry/server.jar https://github.com/Anuken/Mindustry/releases/download/v160.7/server-release.jar \
 && echo "f7b899a4038b95b14a824d5f8993984f79d73baa3254c385b6538327f9c0befd  /opt/mindustry/server.jar" | sha256sum -c - \
 && chmod 0644 /opt/mindustry/server.jar
WORKDIR /app
COPY data/ /app/
RUN chmod -R a+rX /app
# Agents run as this non-root user ([agent] user in task.toml) and own their workdir.
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /app
# Plain Harbor only: the oracle (solution/solve.sh) runs as this user and the verifier reads the grading state here. On
# the harness the game and the verifier run on the hub, and an agent sandbox's copy of this folder is never read.
RUN install -d -m 0700 -o agent -g agent /var/lib/mindustry-many-fronts
CMD ["sleep", "infinity"]
