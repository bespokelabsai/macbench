#!/bin/sh
# The design-check command: checks the team's SysML model (default design/**/*.sysml) against the design
# language: syntax, names, types, units, designations and how the stack joins. It computes nothing about
# the vehicle. Independent of the caller's PATH and environment.
export PYTHONPATH="/opt/designlang${PYTHONPATH:+:$PYTHONPATH}"
if [ $# -eq 0 ]; then set -- $(find design -name "*.sysml" | sort); fi
exec /opt/conda/envs/marsflight/bin/python3 -m designlang.check "$@"
