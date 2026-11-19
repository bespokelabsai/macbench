"""multi-agent-harness-cleanup against a fake resume: deletes only undeleted ledger ids, skips session mismatch."""

import json
from types import SimpleNamespace

from bespokelabs.sandbox import SandboxNotFoundError

from multi_agent_harness.harbor.cleanup import cleanup


class Boxes:
    def __init__(self, boxes):
        self.boxes, self.deleted, self.got = boxes, [], []

    async def attach(self, backend, state):
        sid = state["sandbox_id"]
        self.got.append((backend, sid))
        if sid not in self.boxes:
            raise SandboxNotFoundError("nope")

        async def destroy():
            self.deleted.append(sid)
        raw = SimpleNamespace(state="started", labels={"harbor.session_id": self.boxes[sid]})
        return SimpleNamespace(destroy=destroy, _sandbox=SimpleNamespace(_session=SimpleNamespace(_sandbox=raw)))


async def test_cleanup_deletes_only_pending_matching_ids(tmp_path):
    trial = tmp_path / "job" / "trial"
    trial.mkdir(parents=True)
    rows = [{"id": i, "name": n, "session_id": "s1", "backend": "daytona", "state": {"sandbox_id": i}}
            for i, n in [("hub", "hub"), ("a1", "agent-1"), ("a2", "agent-2"), ("a3", "agent-3")]]
    rows.append({"deleted": "a1"})
    (trial / "sandbox-ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    old = tmp_path / "job" / "old"  # entries with no backend or state
    old.mkdir()
    (old / "sandbox-ledger.jsonl").write_text(json.dumps({"id": "o1", "name": "hub", "session_id": "s0"}) + "\n")
    boxes = Boxes({"hub": "s1", "a1": "s1", "a2": "other", "other": "s1", "o1": "s0"})  # a3 already gone

    dry = await cleanup(tmp_path, boxes.attach, dry_run=True)
    assert boxes.deleted == [] and ("hub", "hub", "would delete") in dry

    out = {sid: action for sid, _, action in await cleanup(tmp_path, boxes.attach)}
    assert sorted(boxes.deleted) == ["hub", "o1"]
    assert out["a2"].startswith("SKIPPED") and out["a3"] == "already gone" and "a1" not in out
    assert ("daytona", "o1") in boxes.got and all(sid != "other" for _, sid in boxes.got)
