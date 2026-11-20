#!/bin/bash
/opt/caverescue/start.sh >/dev/null 2>&1 &
exec "$@"
