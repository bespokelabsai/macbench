"""Runs one area's controller for the simulator, one JSON message per line on stdin and stdout.

    python3 ctrlhost.py APP_DIR AREA

The simulator starts one of these per area, each as its own user when it can, so an area's controller
sees only its own observations and the messages sent to it. The first line is
{"type": "init", "area", "grid", "private", "step_s", "horizon_min"}; then one {"type": "step", "step",
"obs", "inbox"} line per step, answered with {"step", "actions", "outbox"}. If several step lines are
waiting, only the newest is answered: a controller that falls behind skips steps instead of lagging.

APP_DIR/areas/<AREA>/controller.py must define make_controller(area, grid, private) returning an object with
step(obs, inbox) -> (actions, outbox). APP_DIR is on sys.path, so shared code in APP_DIR/lib/ imports as lib.
"""
import importlib.util
import json
import os
import select
import sys
import traceback

# Areas talk only through the simulator's messages. Before any controller code runs, this audit hook refuses
# everything that could open another channel: writing files, sockets, new processes, signals, shared memory,
# ctypes. Audit hooks cannot be removed once added. Reading files (the repository, Python, numpy) stays allowed.


def _guard(event, args):
    # The lists are constants of this function's code, so controller code cannot edit them; replacing the code
    # itself is an audited event, refused below.
    if event in frozenset({
            "socket.__new__", "socket.bind", "socket.connect", "socket.sendto", "subprocess.Popen", "os.system",
            "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.kill", "os.killpg", "ctypes.dlopen",
            "ctypes.dlsym", "ctypes.cdata", "ctypes.call_function", "mmap.__new__", "os.mkdir", "os.rename",
            "os.remove", "os.rmdir", "os.symlink", "os.link", "os.chmod", "os.chown", "os.truncate", "os.mkfifo",
            "os.mknod", "os.utime", "shutil.copyfile", "shutil.move", "shutil.rmtree", "_posixshmem.shm_open",
            "os.setuid", "os.setgid", "pty.spawn", "os.putenv", "os.unsetenv", "sys.setprofile", "sys.settrace"}):
        raise PermissionError(f"{event} is not allowed in a controller: areas communicate only through messages")
    if event == "object.__setattr__" and len(args) >= 2 and args[1] in ("__code__", "__closure__", "__globals__"):
        raise PermissionError("changing code objects is not allowed in a controller")
    if event == "open" and len(args) >= 2:
        mode, flags = args[1], (args[2] if len(args) > 2 else 0)
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)):
            raise PermissionError("writing files is not allowed in a controller")


def main():
    app, area = sys.argv[1], sys.argv[2]
    out = sys.stdout
    sys.stdout = sys.stderr          # prints from the controller go to its log, not the protocol
    inp = sys.stdin.buffer
    init = json.loads(inp.readline())
    # numpy and scipy load shared libraries through ctypes when imported, so they are imported before the guard,
    # and before the team's repository is on sys.path: a numpy.py, scipy.py or ctypes.py there must not run
    # unguarded (it could keep open a file or socket whose later use no audit event covers).
    for name in ("ctypes", "numpy", "scipy", "scipy.linalg", "scipy.sparse", "scipy.sparse.linalg", "scipy.sparse.csgraph",
                 "scipy.optimize"):
        try:
            importlib.import_module(name)
        except Exception:
            pass
    sys.addaudithook(_guard)
    sys.path.insert(0, app)
    path = os.path.join(app, "areas", area, "controller.py")
    spec = importlib.util.spec_from_file_location(f"controller_{area}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctl = mod.make_controller(area, init["grid"], init["private"])
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    buf = b""
    while True:
        line = inp.readline()
        if not line:
            return
        # take the newest waiting step
        while select.select([inp], [], [], 0)[0]:
            nxt = inp.readline()
            if not nxt:
                break
            line = nxt
        msg = json.loads(line)
        if msg.get("type") == "stop":
            return
        try:
            res = ctl.step(msg["obs"], msg["inbox"])
            actions, outbox = res if isinstance(res, (tuple, list)) and len(res) == 2 else (res, [])
            reply = {"step": msg["step"], "actions": actions or [], "outbox": outbox or []}
            text = json.dumps(reply)
        except Exception:
            text = json.dumps({"step": msg["step"], "actions": [], "outbox": [],
                               "error": traceback.format_exc(limit=6)[-2000:]})
        out.write(text + "\n")
        out.flush()


if __name__ == "__main__":
    main()
