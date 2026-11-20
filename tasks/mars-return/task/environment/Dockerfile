FROM python:3.12.8-bookworm
# Tudat (TU Delft astrodynamics, BSD-3) from the tudat-team conda channel, and NASA CEA v3 (Apache-2.0).
# The team gets these public tools and the design language's checker. The mission simulator (marsflight)
# and the hidden compiler are not in this image: they are in tests/hidden/, and environment/hub/make_hub_plugin.py
# carries them to the hub, where the program office and the verifier run them.
RUN apt-get update && apt-get install -y --no-install-recommends tmux asciinema bzip2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV MAMBA_ROOT_PREFIX=/opt/conda
# Pinned pip packages, one file per tool (requirements/README.md).
COPY requirements/ /tmp/requirements/
# Only the Tudat data the simulator loads (DE440, Tudat's merged planet kernel, IAU PCK, masses, gravity
# fields, space weather, Earth orientation): the tudat-resources v2.4 release, checked against its SHA-256, without
# star_catalog_biases and atmosphere_tables and with six SPICE kernels, about 340 MB. The release file was replaced
# upstream on 2026-11-10: it moved the old historical Delta T table to generation/ and added a longer one and
# pre-1962 polar motion. The last step puts the old table back and drops the additions, so the image holds exactly
# the 46 files our published runs used. The conda install below skips Tudat's own post-link script, which would fetch the
# whole release without a check.
RUN set -e \
 && curl -fsSL --retry 5 --retry-delay 10 -o /tmp/resource.tar.gz \
      https://github.com/tudat-team/tudat-resources/releases/download/v2.4/resource.tar.gz \
 && echo "09ab7d377ab5449cdce37a18ade3dcf941180ca2d8f4977b0e95311c303f84fa  /tmp/resource.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/tudat/.tudat \
 && tar -xzf /tmp/resource.tar.gz -C /opt/tudat/.tudat --no-same-owner \
      --exclude=resource/star_catalog_biases --exclude=resource/atmosphere_tables \
 && find /opt/tudat/.tudat/resource/spice_kernels -mindepth 1 -maxdepth 1 ! \( -name naif0012.tls -o -name pck00010.tpc \
      -o -name inpop19a_TDB_m100_p100_spice.tpc -o -name NOE-4-2020.tpc -o -name de440.bsp \
      -o -name tudat_merged_spk_kernel.bsp \) -exec rm -rf {} + \
 && cd /opt/tudat/.tudat/resource/earth_orientation \
 && mv generation/historicalDeltaT_usno_1620-2009.txt historicalDeltaT.txt \
 && rm -rf generation eopc01_polar_motion_1846-1961.txt && cd / \
 && chown -R root:root /opt/tudat && chmod -R u=rwX,go=rX /opt/tudat && rm /tmp/resource.tar.gz
# One layer: install, then share Tudat's data with every user, including the unprivileged user the
# verifier runs flight software as.
RUN set -e \
 && curl -fsSL --retry 5 https://micro.mamba.pm/api/micromamba/linux-64/2.10.0 | tar -xj -C /usr/local bin/micromamba \
 && micromamba create -y -q --skip-run-link-scripts -n marsflight -c tudat-team -c conda-forge python=3.12 tudatpy=1.0.0 \
 && for r in /tmp/requirements/*.txt; do /opt/conda/envs/marsflight/bin/pip install --no-cache-dir -r "$r"; done \
 && micromamba clean -a -y && rm -rf /opt/conda/pkgs /root/.cache /tmp/requirements \
 && test -s /opt/tudat/.tudat/resource/spice_kernels/de440.bsp && rm -rf /root/.tudat \
 && ln -s /opt/tudat/.tudat /root/.tudat && ln -s /opt/tudat/.tudat /etc/skel/.tudat \
 && useradd -m -s /bin/bash flight && rm -rf /home/flight/.tudat && ln -s /opt/tudat/.tudat /home/flight/.tudat \
 && chown -R root:root /opt/conda && chmod -R a+rX,go-w /opt/conda \
 && test -z "$(find /opt/tudat ! -user root -o -perm /022 | head -1)"
# Conda packages can carry uid 1000, which useradd then hands to `flight`, so without the chown the flight
# software could rewrite what the simulator loads. Tudat's data is set to root and modes 755/644
# in its own layer above, so it is only checked here: a chown or chmod of it would store it a second time.
ENV PATH=/opt/conda/envs/marsflight/bin:$PATH \
    PYTHONPATH=/opt/designlang \
    MARSFLIGHT_KERNELS=/opt/tudat/.tudat/resource/spice_kernels \
    MARSFLIGHT_MC_WORKERS=4
COPY designlang/ /opt/designlang/
# The agent CLIs and the verifier may run without the image's PATH, so nothing relies on it: the wrapper
# names the env's Python, and login shells get the env on PATH too.
COPY design-check.sh /usr/local/bin/design-check
RUN chmod 755 /usr/local/bin/design-check \
 && printf 'export PATH=/opt/conda/envs/marsflight/bin:$PATH\nexport PYTHONPATH=/opt/designlang\nexport MARSFLIGHT_KERNELS=/opt/tudat/.tudat/resource/spice_kernels\nexport MARSFLIGHT_MC_WORKERS=4\n' > /etc/profile.d/marsflight.sh \
 && ln -sf /opt/conda/envs/marsflight/bin/python3 /usr/local/bin/mfpython \
 && chmod -R a+rX /opt/designlang && python3 -c "import tudatpy, cea, designlang.check; print('stack ok', tudatpy.__version__)" \
 && test ! -e /opt/marsflight && test ! -e /opt/designlang/designlang/hidden
# The agent works as this user ([agent] user in task.toml). Its home gets
# Tudat's data link from /etc/skel; it owns /app, where the harness makes its clone of the team repo.
RUN useradd -m -s /bin/bash agent && test -L /home/agent/.tudat
WORKDIR /app
COPY --chown=agent:agent app/ /app/
RUN chown agent:agent /app && chmod -R u+rwX,a+rX /app
CMD ["sleep", "infinity"]
