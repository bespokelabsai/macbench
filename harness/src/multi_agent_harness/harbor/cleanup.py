"""multi-agent-harness-cleanup: delete the sandboxes a run's id ledger says were created and never deleted.

    multi-agent-harness-cleanup <run, job or trial dir, or a sandbox-ledger.jsonl> [--dry-run]

Only ids from the ledger are touched, each reattached by its own id through the SDK's resume().
Where the backend shows labels (Daytona), a sandbox whose harbor.session_id label differs from
the ledger's is skipped. It never lists sandboxes and never touches snapshots. The backend's API
key comes from the environment. The ledger is read, never written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from bespokelabs.sandbox import SandboxNotFoundError

from .sandbox import DEFAULT_BACKEND, LEDGER_NAME, provider_handle, resume

Resume = Callable[[str, dict], Awaitable[Any]]


def find_ledgers(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.rglob(LEDGER_NAME))


def pending(ledger: Path) -> list[dict]:
    """Created entries (first per id) with no `deleted` line."""
    created: dict[str, dict] = {}
    deleted: set[str] = set()
    for line in ledger.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("deleted"):
            deleted.add(e["deleted"])
        elif e.get("id") and e["id"] not in created:
            created[e["id"]] = e
    return [e for i, e in created.items() if i not in deleted]


async def cleanup(path: Path, attach: Resume = resume, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Rows of (id, name, action). `attach(backend, state)` reattaches one sandbox by id."""
    rows = []
    for ledger in find_ledgers(path):
        for e in pending(ledger):
            sid, name = e["id"], e.get("name", "?")
            backend = e.get("backend", DEFAULT_BACKEND)
            try:
                sb = await attach(backend, e.get("state") or {"sandbox_id": sid})
            except SandboxNotFoundError:
                rows.append((sid, name, "already gone"))
                continue
            raw = provider_handle(sb)
            state = str(getattr(raw, "state", "") or "")
            if "destroy" in state.lower():
                rows.append((sid, name, f"already gone ({state})"))
                continue
            labels = getattr(raw, "labels", None)
            if labels is not None and labels.get("harbor.session_id") != e.get("session_id"):
                rows.append((sid, name, f"SKIPPED: session {labels.get('harbor.session_id')!r} != "
                                        f"ledger {e.get('session_id')!r}"))
                continue
            note = "" if labels is not None else " (labels not readable on this backend)"
            if dry_run:
                rows.append((sid, name, "would delete" + note))
                continue
            try:
                await sb.destroy()
                rows.append((sid, name, "deleted" + note))
            except SandboxNotFoundError:
                rows.append((sid, name, "already gone"))
            except Exception as ex:  # noqa: BLE001
                rows.append((sid, name, f"DELETE FAILED: {type(ex).__name__}: {str(ex)[:200]}"))
    return rows


async def _main(path: Path, dry_run: bool) -> int:
    if not find_ledgers(path):
        print(f"no {LEDGER_NAME} under {path}")
        return 1
    rows = await cleanup(path, dry_run=dry_run)
    print(f"{'id':<38} {'name':<10} action")
    for sid, name, action in rows:
        print(f"{sid:<38} {name:<10} {action}")
    if not rows:
        print("nothing pending: every ledger id has a deleted line")
    return 1 if any(a.startswith(("SKIPPED", "DELETE FAILED")) for _, _, a in rows) else 0


def main() -> None:
    p = argparse.ArgumentParser(prog="multi-agent-harness-cleanup", description=__doc__.splitlines()[0])
    p.add_argument("path", type=Path)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    raise SystemExit(asyncio.run(_main(a.path, a.dry_run)))


if __name__ == "__main__":
    main()
