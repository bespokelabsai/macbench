#!/bin/bash
set -e
APP=${MULTI_AGENT_HARNESS_GRADE_APP:-/app}
rm -rf "$APP/compressor"
cp -r "$(dirname "$0")/reference" "$APP/compressor"
chmod +x "$APP/compressor/build.sh"
