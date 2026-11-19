"""Runs the team's operating software in its own process. Standard library only.

    python3 -I ops_runner.py OPS_DIR

It imports OPS_DIR/autopilot.py and answers one JSON request per line on stdin:

    {"cmd": "init", "data": setup}   ->  {"ok": true}             (calls autopilot.init(setup) if it exists)
    {"cmd": "step", "data": obs}     ->  {"ok": true, "data": actions}
    {"cmd": "close"}                 ->  exits

on the stdout it was started with. Anything the team's code or its subprocesses print goes to stderr, so it
cannot break the protocol. An exception answers {"ok": false, "error": "..."}.
"""
import json
import os
import sys
import traceback


def main():
    ops = os.path.abspath(sys.argv[1])
    # The protocol keeps its own copies of stdin and stdout (os.dup's are not inherited), and fds 0 and 1 become
    # /dev/null and stderr, so neither the team's prints nor any process it starts can reach the protocol pipes
    # (a child's `echo` wrote into the reply stream).
    out = os.fdopen(os.dup(1), "w", buffering=1)
    inp = os.fdopen(os.dup(0), "r")
    os.dup2(2, 1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout = sys.stderr            # the team's prints go to stderr
    sys.stdin = open(os.devnull)
    sys.path.insert(0, ops)
    os.chdir(ops)
    mod = None
    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd = msg.get("cmd")
        if cmd == "close":
            break
        try:
            if mod is None:
                import autopilot as mod  # noqa: F811
            if cmd == "init":
                fn = getattr(mod, "init", None)
                if fn:
                    fn(msg.get("data"))
                res = {"ok": True}
            elif cmd == "step":
                act = mod.step(msg.get("data"))
                res = {"ok": True, "data": act if isinstance(act, dict) else {}}
            else:
                res = {"ok": False, "error": f"unknown command {cmd!r}"}
        except BaseException as e:   # noqa: B036 - the team's code may raise anything, even SystemExit
            res = {"ok": False, "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"}
        try:
            out.write(json.dumps(res, allow_nan=False) + "\n")
        except (TypeError, ValueError) as e:
            out.write(json.dumps({"ok": False, "error": f"actions are not plain JSON: {e}"}) + "\n")


if __name__ == "__main__":
    main()
