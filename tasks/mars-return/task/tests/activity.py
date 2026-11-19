"""Print how many files of the deliverable differ from the starting repo: a check that the agents acted.

    python3 activity.py APP_DIR MANIFEST            # MANIFEST: {relative path: sha256} of the starting /app
    python3 activity.py --write APP_DIR MANIFEST    # write the manifest (tests/start_manifest.json, from environment/app)

Prints one line, "[verifier] workspace: N files changed from the starting repo (...)". It does not change the score.
"""
import hashlib
import json
import os
import sys


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree(root):
    out = {}
    for dp, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if d not in (".git", "__pycache__")]
        for f in fs:
            p = os.path.join(dp, f)
            if os.path.isfile(p) and not os.path.islink(p):
                out[os.path.relpath(p, root)] = digest(p)
    return out


def main(app, manifest):
    if not os.path.exists(manifest):
        print("[verifier] workspace: no starting manifest to compare with")
        return
    start = json.load(open(manifest))
    now = tree(app)
    changed = sorted(p for p in now if start.get(p) != now[p])
    removed = sorted(p for p in start if p not in now)
    print(f"[verifier] workspace: {len(changed) + len(removed)} files changed from the starting repo "
          f"({', '.join((changed + removed)[:8])}{', ...' if len(changed) + len(removed) > 8 else ''})")


if __name__ == "__main__":
    if sys.argv[1] == "--write":   # activity.py --write APP_DIR MANIFEST: record the starting repo
        json.dump(tree(sys.argv[2]), open(sys.argv[3], "w"), indent=0, sort_keys=True)
    else:
        main(sys.argv[1], sys.argv[2])
