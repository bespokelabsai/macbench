FROM python:3.12.8-slim-bookworm
# Power Outage: the team image, also the hub's. Python with numpy and scipy for the controllers, git and the terminal
# tools the agent harnesses need. The simulator (hub/gridsim) is in it root-only, for the operations office and the
# verifier on the hub; agents work as an unprivileged user and cannot read it, and it holds no hidden-scenario salt
# (that is only in tests/). The hub's plugins in hub/ are not copied into the image.
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates procps tmux asciinema \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.1.3 scipy==1.14.1
# One unprivileged user per control area: the runner starts each area's controller as its own user, so controllers
# cannot read each other, the simulator or the scenario.
RUN for i in $(seq -w 1 16); do useradd -M -d /nonexistent -s /usr/sbin/nologin -u 20$i ctl$i; done
# The controller host the runner starts for each area (public: it only loads areas/<area>/controller.py).
COPY host/ /opt/blackstart-host/
RUN chmod -R a+rX /opt/blackstart-host
COPY hub/gridsim/ /opt/blackstart/gridsim/
RUN chown -R root:root /opt/blackstart && chmod -R go-rwx /opt/blackstart \
 && ! grep -rq "black-start-hidden" /opt/blackstart
# The agent works as this user ([agent] user in task.toml). It owns /app,
# where the harness makes its clone of the team repo.
RUN useradd -m -u 1000 -s /bin/bash agent
WORKDIR /app
COPY --chown=agent:agent app/ /app/
RUN chown agent:agent /app && chmod -R u+rwX,a+rX /app
CMD ["sleep", "infinity"]
