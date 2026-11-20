#!/bin/bash
# Runs on the hub. Reads /srv/git/team.git and the board log; writes /logs/verifier/reward.txt.
mkdir -p /logs/verifier
python3 /tests/verify.py || echo 0 > /logs/verifier/reward.txt
