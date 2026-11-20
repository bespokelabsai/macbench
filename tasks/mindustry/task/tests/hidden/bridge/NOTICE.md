# Notice: Mindustry agent bridge

The bridge in this folder (`src/`, `resources/`, `plugin.json` and `build.sh`) is licensed under the
**GNU General Public License, version 3 only (GPL-3.0-only)**. The full text is in [LICENSE](LICENSE). Every Java
source file carries the identifier `SPDX-License-Identifier: GPL-3.0-only`.

The bridge runs inside the Mindustry server as a plugin and is compiled against Mindustry's classes, so it is a work
based on Mindustry (GPLv3) and is distributed under the GPL. It is adapted from
[olamlabs/multi-agent-mindustry](https://github.com/olamlabs/multi-agent-mindustry) (commit 1374e46), Copyright Olam
Labs, licensed under GPL-3.0-only. Bespoke Labs modified it for Multi Agent Coordination Bench in 2026; the
modifications are also licensed under GPL-3.0-only.

This folder is the only part of the Mindustry task under the GPL. The rest of the task, and the rest of this
repository's code by Bespoke Labs, is licensed under the Apache License 2.0. Mindustry itself is not bundled: the
image downloads the official, unmodified Mindustry v160.7 server JAR (GPLv3, Anuken and contributors). This is not an
official Mindustry project.
