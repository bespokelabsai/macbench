import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import metrics  # noqa: E402


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _trial(root: Path, name: str, reward: float, bundle: bool = False) -> Path:
    t = root / name
    ev = [{"ts": 100.0, "event": "register", "agent": a} for a in ("a", "b", "c")]
    ev += [
        {"ts": 101.0, "event": "post", "agent": "a", "channel": "general", "to": None, "text": "I am a. I take branch feat/x"},
        {"ts": 102.0, "event": "post", "agent": "b", "channel": "general", "to": None, "text": "I am b. I take branch feat/x"},
        {"ts": 170.0, "event": "send", "agent": "a", "to": "c", "text": "ping"},
        {"ts": 171.0, "event": "inbox", "agent": "c", "returned": 1},
        {"ts": 172.0, "event": "rate_limited", "agent": "c", "action": "inbox", "count": 4},
        {"ts": 171.5, "event": "refused", "agent": "b", "action": "send", "to": None, "reason": "no"},
    ]
    _jsonl(t / "agent" / "board-events.jsonl", ev)
    _jsonl(t / "agent" / "proxy-usage.jsonl", [
        {"ts": "1970-01-01T00:01:43+00:00", "agent": "a", "status": 200, "usd": 0.5},
        {"ts": "1970-01-01T00:01:44+00:00", "agent": "b", "status": 429, "usd": 0, "reason": "rate_limit"},
    ])
    (t / "verifier").mkdir()
    (t / "verifier" / "details.json").write_text(json.dumps({"reward": reward, "agents": ["a", "b", "c"]}))
    if bundle:
        repo = root / f"{name}-src"
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(root), "PATH": "/usr/bin:/bin",
               "GIT_AUTHOR_DATE": "@150 +0000", "GIT_COMMITTER_DATE": "@150 +0000"}

        def git(*args, author="a"):
            subprocess.run(["git", "-C", str(repo), "-c", f"user.name={author}", "-c", f"user.email={author}@team.local",
                            *args], check=True, capture_output=True, env=env)

        repo.mkdir()
        git("init", "-q", "-b", "main")
        for author, fname in (("a", "f1"), ("b", "f1"), ("b", "f2")):
            (repo / fname).write_text(author + fname)
            git("add", fname, author=author)
            git("commit", "-qm", f"add {fname}", author=author)
        (t / metrics.BUNDLE).parent.mkdir(parents=True)
        git("bundle", "create", str(t / metrics.BUNDLE), "--all")
    return t


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_trial_metrics(tmp_path):
    m = metrics.trial_metrics(_trial(tmp_path, "t1", 1.0, bundle=True))
    a, b, c = (m["agents"][k] for k in "abc")
    # the first two are old-board `post` events, which still count as broadcasts
    assert (a["sends_broadcast"], a["sends_direct"], c["received_direct"], c["inbox_calls"], c["rate_limited"]) == \
        (1, 1, 1, 1, 4)
    assert b["refused"] == 1 and m["team"]["sends"] == 3 and m["team"]["sends_direct"] == 1
    assert a["model_usd"] == 0.5 and b["proxy_rate_limited"] == 1
    assert (a["commits"], b["commits"], b["files_touched"]) == (1, 2, 2)
    assert m["git"]["share_commits_touching_shared_files"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["team"]["sends_per_minute"] == [2, 1] and m["team"]["duration_s"] == 71.0
    assert m["duplication"]["exact_groups"] == 1  # names masked, so "I am a" == "I am b"
    assert m["duplication"]["branch_names_proposed_by_several_agents"] == {"feat/x": ["a", "b"]}
    assert m["gini"]["commits"] == pytest.approx(0.4444, abs=1e-3) and m["reward"] == 1.0


def test_job_aggregates(tmp_path):
    for i, r in enumerate((1.0, 0.0, 0.5)):
        _trial(tmp_path, f"t{i}", r)
    (tmp_path / "not-a-trial").mkdir()
    assert metrics.main([str(tmp_path), "--out", str(tmp_path / "m.json")]) == 0
    m = json.loads((tmp_path / "m.json").read_text())
    assert m["trials"] == 3 and m["aggregate"]["reward"] == {"n": 3, "mean": 0.5, "sd": 0.5}
    assert m["total_model_usd"] == 1.5
