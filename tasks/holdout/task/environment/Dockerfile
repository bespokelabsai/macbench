FROM python:3.12.8-bookworm
# Holdout: the agents' image holds only the public docs and the API client. The game runs on the hub and is
# unpacked there by the board plugin that environment/hub/make_hub_plugin.py generates from tests/hidden/; it is never
# in this build context.
RUN apt-get update && apt-get install -y --no-install-recommends tmux jq && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY data/ /app/
RUN chmod -R a+rX /app
# One agent is one survivor: no subagents. The harness turns off Agent and Task only; this also closes workflows, skills
# (batch, deep-research and others spawn agents), remote agents and a nested claude from Bash. Managed settings, so an agent
# cannot override them. Claude Code adds these to the harness's own deny rules (checked on a sandbox).
COPY claude-managed.json /etc/claude-code/managed-settings.d/50-holdout.json
RUN chmod -R a+rX /etc/claude-code
CMD ["sleep", "infinity"]
