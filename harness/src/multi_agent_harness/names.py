"""Agent-facing names: what the board, git and the prompts call each agent.

Sandboxes keep their slot names (agent-1..N) internally. Agents see word names such as
`mango-fern-humming` instead, and every list of teammates comes in an order of their own,
because a number in the name or a fixed order was enough to make the first agent the lead. The
words are picked from a seed (the trial's session id), so a rerun of the same trial gets the same
names. The lists hold plain, neutral words: no rank, order, size, role or mission words, and none
starting with "lead" (examples/policies/hierarchy.py treats those as leads). `numbered` keeps the
slot names and ascending lists.
"""

from __future__ import annotations

import hashlib
import random
from typing import Iterable, Iterator

WORDS = "words"
NUMBERED = "numbered"
MODES = (WORDS, NUMBERED)
ALIASES = {"hashed": WORDS}  # the first release's name for the default

# 64 nouns and 32 verbs, so one byte of a hash picks each word without bias.
NOUNS = (
    "almond", "apple", "basil", "birch", "cedar", "clover", "cocoa", "coral",
    "cotton", "daisy", "dune", "ember", "feather", "fern", "fig", "flint",
    "garnet", "ginger", "harbor", "hazel", "honey", "ivy", "jade", "juniper",
    "kelp", "kiwi", "lemon", "lilac", "linen", "lotus", "mango", "maple",
    "marble", "meadow", "mint", "moss", "nutmeg", "oak", "olive", "onyx",
    "opal", "orchid", "pebble", "pepper", "pine", "plum", "poppy", "quartz",
    "reed", "river", "saffron", "sand", "shell", "slate", "sorrel", "spruce",
    "tulip", "velvet", "walnut", "willow", "yarrow", "zinnia", "acorn", "thistle",
)
VERBS = (
    "baking", "biking", "blooming", "bouncing", "drifting", "drumming", "floating", "folding",
    "gliding", "hiking", "humming", "juggling", "knitting", "painting", "pouring", "rolling",
    "rowing", "running", "sailing", "singing", "skating", "sketching", "spinning", "splashing",
    "swaying", "swimming", "tapping", "twirling", "wandering", "weaving", "whistling", "yodeling",
)


def check_mode(mode: str | None) -> str:
    m = (mode or WORDS).strip() if isinstance(mode, str) else WORDS
    m = ALIASES.get(m, m)
    if m not in MODES:
        raise ValueError(f"agent_names={mode!r}: use one of {', '.join(MODES)}")
    return m


def candidates(seed: str) -> Iterator[str]:
    """noun-noun-verb names in a fixed order for `seed`, never repeating a noun within a name."""
    k = 0
    while True:
        h = hashlib.sha256(f"{seed}:{k}".encode()).digest()
        k += 1
        a, b = NOUNS[h[0] % len(NOUNS)], NOUNS[h[1] % len(NOUNS)]
        if a != b:
            yield f"{a}-{b}-{VERBS[h[2] % len(VERBS)]}"


def fresh(source: Iterator[str], taken: Iterable[str]) -> str:
    """The next name from `source` that isn't taken."""
    taken = set(taken)
    return next(n for n in source if n not in taken)


def assign(slots: list[str], seed: str, mode: str = WORDS) -> dict[str, str]:
    """{slot: agent-facing name}, unique within the team."""
    if check_mode(mode) == NUMBERED:
        return {s: s for s in slots}
    source, out = candidates(seed), {}
    for s in slots:
        out[s] = fresh(source, out.values())
    return out


def ordered(names: Iterable[str], seed: str, viewer: str, mode: str = WORDS) -> list[str]:
    """`names` in the order `viewer` sees them: its own shuffle, or as given when numbered."""
    out = list(names)
    if check_mode(mode) == WORDS:
        random.Random(f"{seed}:{viewer}").shuffle(out)
    return out
