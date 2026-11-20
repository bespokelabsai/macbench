"""Which CLI (harness) and model each agent sandbox runs.

By default every agent runs --ak harness with --ak model. --ak team gives agents different ones:
a list of groups, handed out to agent-1..N in order.

    --ak 'team=[{"harness": "codex", "model": "gpt-6-astra", "count": 4},
                {"harness": "claude-code", "model": "claude-opus-5-5"}]'

Here agent-1..4 run Codex on gpt-6-astra and the rest run Claude Code. A group has a harness
(default --ak harness), a model, a count and a cli_version. One group may leave out its count and
takes the agents left over. A group's model and cli_version default to --ak model and
--ak cli_version when it runs --ak harness, and otherwise to that harness's own default.

Harbor parses --ak values as JSON, so the list arrives parsed; a JSON string is parsed here. TeamAgent
checks the groups when it is built. The environment reads the same kwargs from the trial config and
hands them out to its agents before any sandbox exists, so counts that don't fit n_agents fail first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

RUNNERS = ("claude-code", "codex", "antigravity")  # harnesses TeamAgent knows how to configure and launch
# Cheap and priced in pricing.json; claude-code uses its CLI default. agy model names are its own
# slugs (agy models): gemini-3.8-flash-low calls gemini-3.8-flash at low thinking.
DEFAULT_MODEL = {"codex": "gpt-6-luna", "antigravity": "gemini-3.8-flash-low"}
KEYS = ("harness", "model", "count", "cli_version", "effort")
MIXED = "mixed"  # the team's harness label when its agents run different harnesses


@dataclass(frozen=True)
class Group:
    harness: str
    model: str | None  # as given, maybe with a provider/ prefix; None is the CLI's own default
    cli_version: str | None = None
    count: int | None = None  # None: the agents the other groups leave
    effort: str | None = None  # this group's reasoning effort; None: --ak effort, where the harness takes it

    @property
    def short_model(self) -> str | None:
        return self.model.split("/")[-1] if self.model else None


def groups(team: Any, harness: str = "codex", model: str | None = None,
           cli_version: str | None = None) -> list[Group]:
    """The team's groups: --ak team, or one group of every agent on harness and model."""
    def default(h: str, given: str | None, fallback: str | None) -> str | None:
        return (given or fallback) if h == harness else fallback

    if team in (None, "", []):
        return [Group(harness, model or DEFAULT_MODEL.get(harness), cli_version)]
    if isinstance(team, str):
        try:
            team = json.loads(team)
        except ValueError:
            raise ValueError("team must be a JSON list of groups, e.g. "
                             '[{"harness": "codex", "count": 2}, {"harness": "claude-code"}]') from None
    if not isinstance(team, list) or not team:
        raise ValueError("team must be a nonempty list of groups")
    out = []
    for i, g in enumerate(team, 1):
        if not isinstance(g, dict):
            raise ValueError(f"team group {i} must be an object with {', '.join(KEYS)}")
        if extra := sorted(set(g) - set(KEYS)):
            raise ValueError(f"team group {i}: unknown keys {extra}; a group has {', '.join(KEYS)}")
        h = g.get("harness") or harness
        if not isinstance(h, str):
            raise ValueError(f"team group {i}: harness must be a string")
        count = g.get("count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
            raise ValueError(f"team group {i}: count must be an integer >= 1")
        for k in ("model", "cli_version", "effort"):
            if g.get(k) is not None and not isinstance(g[k], str):
                raise ValueError(f"team group {i}: {k} must be a string")
        out.append(Group(h, g.get("model") or default(h, model, DEFAULT_MODEL.get(h)),
                         g.get("cli_version") or default(h, cli_version, None), count, g.get("effort") or None))
    if sum(g.count is None for g in out) > 1:
        raise ValueError("team: only one group may leave out its count")
    return out


def assign(groups: list[Group], agents: list[str]) -> dict[str, Group]:
    """Agent sandbox -> its group, in order: the first group's count gets the first agents."""
    fixed = sum(g.count or 0 for g in groups)
    rest = len(agents) - fixed
    if any(g.count is None for g in groups):
        if rest < 1:
            raise ValueError(f"team: the counts add up to {fixed}, which leaves none of the {len(agents)} "
                             "agents for the group without a count")
    elif rest:
        raise ValueError(f"team: the counts add up to {fixed}, but the team has {len(agents)} agents (n_agents)")
    names = iter(agents)
    return {next(names): g for g in groups for _ in range(rest if g.count is None else g.count)}


def harnesses(kwargs: dict[str, Any] | None, agents: list[str]) -> dict[str, str]:
    """Agent sandbox -> harness, from TeamAgent's kwargs (the trial config's agent kwargs)."""
    kwargs = kwargs or {}
    plan = groups(kwargs.get("team"), kwargs.get("harness") or "codex")
    return {name: g.harness for name, g in assign(plan, agents).items()}


def label(harnesses: Iterable[str]) -> str:
    """The team's harness: the one every agent runs, or "mixed"."""
    found = set(harnesses)
    return found.pop() if len(found) == 1 else MIXED


def describe(seats: dict[str, Group]) -> str:
    """e.g. "agent-1..agent-4 codex (gpt-6-astra), agent-5..agent-8 claude-code (CLI default model)"."""
    runs: list[tuple[list[str], Group]] = []
    for name, g in seats.items():
        if runs and runs[-1][1] == g:
            runs[-1][0].append(name)
        else:
            runs.append(([name], g))
    return ", ".join(f"{names[0] if len(names) == 1 else names[0] + '..' + names[-1]} {g.harness} "
                     f"({g.short_model or 'CLI default model'})" for names, g in runs)
