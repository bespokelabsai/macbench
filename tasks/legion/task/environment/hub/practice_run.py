"""Plays one practice match against Petra on the hub, for practice_plugin.py. Run as
`python3 -I -S practice_run.py` with a JSON request on stdin: {"match", "bot_dir", "verify", "time_limit_min"}.
Prints the match record as one JSON line. Uses the trusted harness and Petra from the hub bundle
(/opt/legion-hub/harness, LEGION_PETRA_ZIP); make_hub_plugin.py packs this file next to them."""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "harness"))
import legion  # noqa: E402  (the trusted copy)


def main():
    req = json.loads(sys.stdin.read())
    bot, limit = Path(req["bot_dir"]), float(req.get("time_limit_min") or legion.DEFAULT_TIME_LIMIT_MIN)
    rec = legion.play(req["match"], bot, limit, legion.DEFAULT_WALL_LIMIT_SEC)
    if req.get("verify") and rec["result"] != "loss":
        rec["reproduction"] = legion.reproduces(rec, bot, limit, legion.DEFAULT_WALL_LIMIT_SEC)
        if not rec["reproduction"]["ok"]:
            rec["unverified_result"] = rec["result"]
            rec["result"], rec["reason"] = "loss", "not_reproducible"
    rec["command_counts"] = {k: len(v) for k, v in rec.pop("commands").items()}
    print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    os.umask(0o022)
    main()
