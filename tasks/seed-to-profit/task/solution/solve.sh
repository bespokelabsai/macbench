#!/bin/bash
# Reference solution. There is no true oracle for this open-ended task: this commits a plain rule-based company
# (solution/reference: the plan and ops/autopilot.py, from public facts only) to main, as a team would, and checks
# it with the team's own tool. It scores what that company earns; it is a baseline, not a ceiling.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd /app
cp "$HERE/reference/company/plan.yaml" company/plan.yaml
rm -rf ops && mkdir -p ops && cp "$HERE/reference/ops/autopilot.py" ops/autopilot.py
python3 tools/check_ops.py || echo "check_ops reported problems (see above)"
if git rev-parse --git-dir >/dev/null 2>&1; then
  git add -A company ops
  git -c user.name=reference -c user.email=reference@example.com commit -qm "reference: plain rules" || true
  git push -q origin HEAD:main 2>/dev/null || true
fi
echo "reference plan and software in place"
