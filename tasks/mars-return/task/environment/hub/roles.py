"""Subsystem roles for the program office. Standard library only: it runs inside the board.

Switch: MARSFLIGHT_ROLES=1 turns roles on in program_plugin.py. Off by default, and then nothing here runs.

The rules:
- There are six subsystems. An agent claims one with claim_role. One agent may hold at most
  ceil(6 / team size) roles, so a team of one holds all six, and a team of six holds one each.
- A part belongs to the subsystem whose folder holds the SysML file that defines it: a `part def` in
  design/propulsion/*.sysml belongs to propulsion. A part usage takes the owner of its type. Files outside
  the six folders (such as design/vehicle.sysml) are open: they may place elements, but may not redefine
  values inside an owned part, and may not define a part that an owned folder also defines.
- Only a subsystem's current owner may order or cancel a test on its parts, and a finished test's result
  goes only to the current owner. So a measured value has to be shared to reach anyone else.
- An owner may hand a role over or release it. If the owner has done nothing for 30 minutes, any agent
  under the cap may take the role over.
- Writes to design/<subsystem>/ are allowed only for the owner. path_rules() writes the rules the hub's
  git server needs; push_violations() is the same check in Python.
"""
import math
import re

SUBSYSTEMS = ("launcher", "propulsion", "entry_landing", "surface", "ascent_return", "earth_entry")
TAKEOVER_S = 30 * 60.0


class RoleError(Exception):
    pass


def cap(team_size: int) -> int:
    return max(1, math.ceil(len(SUBSYSTEMS) / max(1, int(team_size))))


def file_owner(path: str):
    """The subsystem that owns a design file, or None for an open file."""
    parts = str(path).replace("\\", "/").lstrip("./").split("/")
    if len(parts) >= 3 and parts[0] == "design" and parts[1] in SUBSYSTEMS:
        return parts[1]
    return None


# ---- reading SysML just enough to find who defines what

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_DEF = re.compile(r"\bpart\s+def\s+([A-Za-z_]\w*)")
_USAGE = re.compile(r"\bpart\s+(?!def\b)([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*:\s*([A-Za-z_][\w:]*)")


def _strip(text: str) -> str:
    return _COMMENT.sub(" ", text or "")


def _blocks(text: str, start: int) -> str:
    """The body between the first '{' at or after start and its matching '}' ('' if none before ';')."""
    i = start
    while i < len(text) and text[i] not in "{;":
        i += 1
    if i >= len(text) or text[i] == ";":
        return ""
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
        j += 1
    return text[i + 1:]


def ownership(files: dict) -> dict:
    """files maps 'design/...sysml' to its text. Returns {"defs": {name: subsystem}, "usages": {name: type},
    "conflicts": [...]}: who owns each part def, and each part usage's type."""
    defs, usages, conflicts = {}, {}, []
    for path in sorted(files):
        owner = file_owner(path)
        text = _strip(files[path])
        for m in _DEF.finditer(text):
            name = m.group(1)
            if owner is None:
                continue
            if name in defs and defs[name] != owner:
                conflicts.append(f"part def {name} is defined in both {defs[name]} and {owner}")
                continue
            defs[name] = owner
        for m in _USAGE.finditer(text):
            usages.setdefault(m.group(1), m.group(2).split("::")[-1])
    return {"defs": defs, "usages": usages, "conflicts": conflicts}


def target_owner(target: str, files: dict):
    """The subsystem that owns a test target such as "vehicle.S1.mainEngines", or None if it is open.
    The deepest segment with an owner decides: a usage is owned through its type."""
    own = ownership(files)
    for seg in reversed([s for s in str(target).split(".") if s]):
        if seg in own["defs"]:
            return own["defs"][seg]
        typ = own["usages"].get(seg)
        if typ and typ in own["defs"]:
            return own["defs"][typ]
    return None


def open_file_violations(files: dict) -> list:
    """What an open file does that it may not: define a part an owned folder defines, or redefine values
    (':>>') inside a usage of an owned part."""
    own = ownership(files)
    out = list(own["conflicts"])
    for path in sorted(files):
        if file_owner(path) is not None:
            continue
        text = _strip(files[path])
        for m in _DEF.finditer(text):
            if m.group(1) in own["defs"]:
                out.append(f"{path}: part def {m.group(1)} belongs to {own['defs'][m.group(1)]}; define it only in "
                           f"design/{own['defs'][m.group(1)]}/")
        for m in _USAGE.finditer(text):
            typ = m.group(2).split("::")[-1]
            sub = own["defs"].get(typ)
            if sub and ":>>" in _blocks(text, m.end()):
                out.append(f"{path}: part {m.group(1)} redefines values of {typ}, which {sub} owns; change it in "
                           f"design/{sub}/")
    return out


# ---- who holds what

class RoleBook:
    """Role state, kept in a plain dict so the program office can save it with its own state."""

    def __init__(self, state: dict, team: list, now):
        self.s = state
        self.s.setdefault("owners", {})       # subsystem -> agent
        self.s.setdefault("last_seen", {})    # agent -> time
        self.s.setdefault("log", [])
        self.team = list(team)
        self.now = now

    def cap(self) -> int:
        return cap(len(self.team))

    def owner(self, role):
        return self.s["owners"].get(role)

    def held(self, agent) -> list:
        return sorted(r for r, a in self.s["owners"].items() if a == agent)

    def touch(self, agent):
        if agent:
            self.s["last_seen"][agent] = self.now()

    def _check(self, role):
        if role not in SUBSYSTEMS:
            raise RoleError(f"no role {role!r}; the roles are {', '.join(SUBSYSTEMS)}")

    def _log(self, op, role, agent, **kw):
        self.s["log"].append({"t": self.now(), "op": op, "role": role, "agent": agent, **kw})

    def _room(self, agent):
        if len(self.held(agent)) >= self.cap():
            raise RoleError(f"{agent} already holds {len(self.held(agent))} roles, the most one agent may hold "
                            f"in a team of {len(self.team)}")

    def claim(self, agent, role) -> dict:
        self._check(role)
        self.touch(agent)
        cur = self.owner(role)
        if cur == agent:
            return self.view(agent)
        if cur is not None:
            idle = self.now() - self.s["last_seen"].get(cur, 0.0)
            if idle < TAKEOVER_S:
                raise RoleError(f"{role} belongs to {cur}; it can be taken over after {cur} has been idle for "
                                f"30 minutes ({(TAKEOVER_S - idle) / 60:.0f} minutes from now)")
        self._room(agent)
        self.s["owners"][role] = agent
        self._log("takeover" if cur else "claim", role, agent, **({"from": cur} if cur else {}))
        return self.view(agent)

    def release(self, agent, role) -> dict:
        self._check(role)
        self.touch(agent)
        if self.owner(role) != agent:
            raise RoleError(f"{agent} does not hold {role}")
        del self.s["owners"][role]
        self._log("release", role, agent)
        return self.view(agent)

    def hand_over(self, agent, role, to) -> dict:
        self._check(role)
        self.touch(agent)
        if self.owner(role) != agent:
            raise RoleError(f"{agent} does not hold {role}")
        if to not in self.team:
            raise RoleError(f"no agent {to!r}")
        self._room(to)
        self.s["owners"][role] = to
        self._log("hand_over", role, agent, to=to)
        return self.view(agent)

    def require(self, agent, role, what):
        """Raise unless agent may act on role's parts. An open part (role None) is everyone's."""
        if role is None:
            return
        cur = self.owner(role)
        if cur != agent:
            who = cur or "nobody yet (claim it with claim_role)"
            raise RoleError(f"{what} belongs to the {role} subsystem, held by {who}; only its owner may do this")

    def view(self, agent=None) -> dict:
        now = self.now()
        d = {"roles": {r: self.owner(r) for r in SUBSYSTEMS}, "cap_per_agent": self.cap(),
             "idle_minutes": {a: round((now - t) / 60, 1) for a, t in self.s["last_seen"].items()},
             "takeover_after_minutes": TAKEOVER_S / 60}
        if agent:
            d["yours"] = self.held(agent)
        return d


def push_violations(agent, paths, book: RoleBook) -> list:
    """Changed paths that agent may not push: files in a subsystem folder it does not own."""
    out = []
    for p in paths:
        sub = file_owner(p)
        if sub and book.owner(sub) != agent:
            out.append(f"{p}: design/{sub}/ belongs to {book.owner(sub) or 'nobody'}")
    return out


def path_rules(book: RoleBook) -> dict:
    """Per-path push rules in the shape of multi-agent-harness each subsystem folder writable only by
    its owner (by nobody while unowned); everything else open."""
    return {"rules": [{"path": f"design/{r}/", "writers": [book.owner(r)] if book.owner(r) else []}
                      for r in SUBSYSTEMS]}
