#!/usr/bin/env python3
"""LEGION match harness: the same match code the grader uses.

Petra is not installed in this sandbox. Matches against Petra are played on the team's hub by the practice
office, from one pool shared by the whole team: use the board tools practice_info, practice_play,
practice_result and practice_status (see MATCH.md). This script helps with those and scores their results;
matches between two LEGION AIs (sparring) run here, as many as you like.

  legion.py match --opponent hard --map mainland --seed 3   # print one match spec for practice_play
  legion.py gauntlet --config my_matches.json --limit 10    # print a list of match specs
  legion.py score results.json                              # score saved practice records
  legion.py spar --opponent-bot /tmp/old_bot --games 6      # local, not counted

Only the Python standard library is needed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import pwd
import random
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARENA = HERE / "arena"
PUBLIC_ZIP = Path("/usr/share/games/0ad/mods/public/public.zip")
PYROGENESIS = "/usr/games/pyrogenesis"
GAME_VERSION = "0.28.0"
DEFAULT_BOT = Path("/app/legion")
DEFAULT_LADDER = HERE / "ladder.json"
DEFAULT_TIME_LIMIT_MIN = 60
DEFAULT_WALL_LIMIT_SEC = 600
MEDIUM = 3  # the submitted AI always plays at Medium: no handicap either way
LEVELS = {"sandbox": 0, "very_easy": 1, "easy": 2, "medium": 3, "hard": 4, "very_hard": 5,
          "very_hard_2x": 6, "very_hard_2.5x": 7, "very_hard_3x": 8,
          "elite_1": 9, "elite_2": 10, "elite_3": 11, "elite_4": 12}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}
# Levels above Very Hard: Petra plays at Very Hard but gathers `rate` times as fast
# as your AI (Very Hard itself is 1.56). The Elite levels also build and train in
# `time` of the usual time and give every unit and structure `strength` times the
# health and attack.
LEVEL_BOOST = {6: {"rate": 2.0}, 7: {"rate": 2.5}, 8: {"rate": 3.0},
               9: {"rate": 3.0, "time": 0.8, "strength": 1.15},
               10: {"rate": 4.0, "time": 0.6, "strength": 1.25},
               11: {"rate": 5.0, "time": 0.55, "strength": 1.35},
               12: {"rate": 6.0, "time": 0.5, "strength": 1.5}}
BEHAVIORS = ("defensive", "balanced", "aggressive")  # Petra's personalities
DEFAULT_BEHAVIOR = "balanced"


def _set_boost(level, player: int, rates: dict, boosts: dict) -> None:
    b = LEVEL_BOOST.get(int(level))
    if b:
        rates[player] = b["rate"]
        if "time" in b or "strength" in b:
            boosts[player] = {"time": b.get("time", 1.0), "strength": b.get("strength", 1.0)}


def petra_difficulty(level: int) -> int:
    """The difficulty Petra itself is started with (its scripts know 0-5)."""
    return min(int(level), 5)


def opponent_key(match: dict) -> str:
    """Rating key of the Petra a match is played against, e.g. "4:aggressive"."""
    return f"{int(match['opponent'])}:{match.get('behavior') or DEFAULT_BEHAVIOR}"
CIVS = ["athen", "brit", "cart", "gaul", "germ", "han", "iber", "kush", "mace", "maur", "pers", "ptol",
        "rome", "sele", "spart"]
# A command logged at game time T was posted during the AI turn whose state.timeElapsed is T - offset.
REPLAY_OFFSET_MS = 0
UNPRIVILEGED = "legion"  # the game refuses to run as root

LINE = re.compile(r"^WARNING: (LEGION_\w+) ?(.*)$")
CMD = re.compile(r"^(\d+) (\d+) (.*)$")
BOT_ERROR = re.compile(r"simulation/ai/legion/bot/")


# --- arena mod -------------------------------------------------------------------

RANDOM_HELPER = re.compile(r"(?<![\w.$])(pickRandom|randFloat|randIntInclusive|randIntExclusive|randBool|shuffleArray)\(")
RNG_IMPORT = 'import { LegionRNG } from "simulation/ai/legion_rng/rng.js";\n'


def _use_legion_rng(source: str) -> str:
    """Route a module's random draws through the per-player LEGION streams."""
    patched = RANDOM_HELPER.sub(r"LegionRNG.\1(", source).replace("Math.random()", "LegionRNG.random()")
    return patched if patched == source else RNG_IMPORT + patched


PETRA_STUB = 'throw new Error("LEGION: Petra cannot be imported by a submitted AI");\n'


def _patched_game_files(public_zip: Path, petra_dir: str) -> dict[str, str]:
    """Patched copies of game files: Commands.js logs every command; Petra runs
    from simulation/ai/<petra_dir>/ (a random name per match) without its own
    handicap (the referee applies it), and every module at the usual
    simulation/ai/petra/ path is replaced by a stub that throws, so a submitted
    AI cannot import Petra under any syntax; Petra and common-api draw
    randomness from per-player streams."""
    files = {}
    with zipfile.ZipFile(public_zip) as z:
        commands = z.read("simulation/helpers/Commands.js").decode()
        for name in z.namelist():
            if name.startswith("simulation/ai/petra/") and not name.endswith("/"):
                base = name[len("simulation/ai/petra/"):]
                text = z.read(name).decode()
                if name.endswith(".js"):
                    files[name] = PETRA_STUB
                    text = _use_legion_rng(text).replace("simulation/ai/petra/", f"simulation/ai/{petra_dir}/")
                files[f"simulation/ai/{petra_dir}/{base}"] = text
            elif name == "simulation/ai/common-api/gamestate.js":
                files[name] = _use_legion_rng(z.read(name).decode())
    config_path = f"simulation/ai/{petra_dir}/config.js"
    config = files.get(config_path)  # None where Petra is not installed (the agents' container)
    head = "function ProcessCommand(player, cmd)\n{\n"
    if head not in commands:
        raise SystemExit("unexpected Commands.js: ProcessCommand not found")
    # Log only the commands players post. Some handlers issue follow-up commands
    # through ProcessCommand (construct with autorepair issues a repair); those are
    # the engine's, and a replay of the posted commands regenerates them.
    commands = commands.replace(head, (
        "function LegionLogCommand(player, cmd)\n{\n"
        "\twarn(\"LEGION_CMD \" + Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() + \" \" + player + \" \" + JSON.stringify(cmd));\n"
        "}\n\n"
        "var g_LegionCommandDepth = 0;\n\n"
        "function ProcessCommand(player, cmd)\n{\n"
        "\tif (!g_LegionCommandDepth)\n\t\tLegionLogCommand(player, cmd);\n"
        "\t++g_LegionCommandDepth;\n"
        "\ttry\n\t{\n\t\treturn LegionProcessPostedCommand(player, cmd);\n\t}\n"
        "\tfinally\n\t{\n\t\t--g_LegionCommandDepth;\n\t}\n}\n\n"
        "function LegionProcessPostedCommand(player, cmd)\n{\n"
        # Autostart games run with cheats enabled, and besides the cheat command (Cheat.js) three command types
        # are gated only on that: control-all (then delete-entities, garrison and the rest act on the enemy's
        # units), reveal-map and promote. They are refused for every player, logged like a blocked cheat; the
        # replay of the logged commands refuses them the same way.
        "\tif ([\"control-all\", \"reveal-map\", \"promote\"].indexOf(cmd.type) != -1)\n\t{\n"
        "\t\twarn(\"LEGION_CHEAT_BLOCKED player=\" + player + \" action=\" + cmd.type);\n\t\treturn;\n\t}\n"), 1)
    if config is not None:
        cheat = re.compile(r"Config\.prototype\.Cheat = function\(gameState\)\n\{.*?\n\};\n", re.S)
        if not cheat.search(config):
            raise SystemExit("unexpected petra/config.js: Cheat not found")
        files[config_path] = cheat.sub(
            "Config.prototype.Cheat = function(gameState)\n{\n\t// LEGION: the referee applies this handicap.\n};\n", config, count=1)
    files["simulation/helpers/Commands.js"] = commands
    return files


def bot_files(bot_dir: Path) -> list[Path]:
    return sorted(p for p in bot_dir.rglob("*") if p.is_file() and p.suffix in (".js", ".json"))


def build_arena(mods_dir: Path, bot_dir: Path, time_limit_ms: int, aiseed: int, petra_dir: str,
                replay: dict | None = None, boost: dict | None = None, public_zip: Path = PUBLIC_ZIP,
                petra_rates: dict | None = None, sparring_bot: Path | None = None,
                petra_boosts: dict | None = None) -> None:
    target = mods_dir / "legion_arena"
    shutil.copytree(ARENA, target, ignore=shutil.ignore_patterns("*.in"))
    referee = target / "maps/scripts/LegionReferee.js"
    text = re.sub(r"const LEGION_TIME_LIMIT_MS = \d+;", f"const LEGION_TIME_LIMIT_MS = {time_limit_ms};",
                  referee.read_text(), count=1)
    if boost is not None:
        text = text.replace("const LEGION_CANDIDATE_BOOST = null;",
                            f"const LEGION_CANDIDATE_BOOST = {json.dumps({'player': int(boost['player']), 'rate': float(boost['rate'])})};", 1)
    text = text.replace('const LEGION_PETRA_AI = "petra";', f"const LEGION_PETRA_AI = {json.dumps(petra_dir)};", 1)
    if petra_rates:
        text = text.replace("const LEGION_PETRA_RATE_OVERRIDE = {};",
                            f"const LEGION_PETRA_RATE_OVERRIDE = {json.dumps({str(int(k)): float(v) for k, v in petra_rates.items()})};", 1)
    if petra_boosts:
        text = text.replace("const LEGION_PETRA_BOOST = {};",
                            f"const LEGION_PETRA_BOOST = {json.dumps({str(int(k)): v for k, v in petra_boosts.items()})};", 1)
    referee.write_text(text)
    rng = (ARENA / "simulation/ai/legion_rng/rng.js.in").read_text().replace("/*SEED*/1", str(int(aiseed) & 0x7FFFFFFF))
    (target / "simulation/ai/legion_rng/rng.js").write_text(rng)
    for rel, text in _patched_game_files(public_zip, petra_dir).items():
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(text)
    bot_target = target / "simulation/ai/legion/bot"
    for path in bot_files(bot_dir):
        dest = bot_target / path.relative_to(bot_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    if sparring_bot is not None:
        # The sparring bot is a second copy of a LEGION AI; its own imports point at its own copy.
        spar_target = target / "simulation/ai/legion_sparring/bot"
        for path in bot_files(sparring_bot):
            dest = spar_target / path.relative_to(sparring_bot)
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(errors="replace")
            if path.suffix == ".js":
                text = text.replace("simulation/ai/legion/bot/", "simulation/ai/legion_sparring/bot/")
            dest.write_text(text)
    if replay is not None:
        template = (ARENA / "simulation/ai/legion_replay/_replay.js.in").read_text()
        code = template.replace("/*COMMANDS*/{}", json.dumps(replay, separators=(",", ":")))
        (target / "simulation/ai/legion_replay/_replay.js").write_text(code)


# --- map settings ------------------------------------------------------------------
# Biome and player placement are otherwise picked at random outside the map seed,
# which would make a match impossible to reproduce. Every match pins both.

def map_options(name: str, public_zip: Path = PUBLIC_ZIP) -> dict:
    with zipfile.ZipFile(public_zip) as z:
        settings = json.loads(z.read(f"maps/random/{name}.json"))["settings"]
        biomes = sorted(n[len("maps/random/rmbiome/"):-5] for n in z.namelist()
                        if n.startswith("maps/random/rmbiome/") and n.endswith(".json"))
    supported = settings.get("SupportedBiomes")
    if isinstance(supported, list):
        options = [b for b in supported if b in biomes]
    elif isinstance(supported, str):
        options = [b for b in biomes if b.startswith(supported)]
    else:
        options = []
    return {"biomes": options, "placements": settings.get("PlayerPlacements") or []}


def pin_map_settings(match: dict) -> dict:
    """Fill in biome and placement deterministically from the match seed."""
    match = dict(match)
    options = map_options(match["map"])
    rng = random.Random(f"{match['map']}:{match['seed']}")
    if "biome" not in match and options["biomes"]:
        match["biome"] = rng.choice(options["biomes"])
    if "placement" not in match and options["placements"]:
        match["placement"] = rng.choice(options["placements"])
    return match


# --- one match -------------------------------------------------------------------

def usable_cpus() -> int:
    """CPUs this container may actually use. os.cpu_count() reports the host's
    cores, so a container with a CPU quota would otherwise run too many games
    at once, slowing each one towards the wall-time limit."""
    counts = [os.cpu_count() or 1]
    try:
        counts.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    try:  # cgroup v2: "max 100000" or "<quota> <period>"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            counts.append(int(quota) // int(period))
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0:
            counts.append(quota // period)
    except (OSError, ValueError):
        pass
    return max(1, min(counts))


def default_jobs() -> int:
    """Parallel games: one per usable CPU, keeping two free for the harness and agent."""
    return max(1, usable_cpus() - 2)


def _run_as():
    """Return (uid, gid) to drop to when running as root."""
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(UNPRIVILEGED)
        return entry.pw_uid, entry.pw_gid
    except KeyError:
        return 65534, 65534


def _chown_tree(path: Path, ids) -> None:
    if not ids:
        return
    for root, dirs, files in os.walk(path):
        os.chown(root, *ids)
        for name in files:
            os.chown(os.path.join(root, name), *ids)


def play(match: dict, bot_dir: Path, time_limit_min: float = DEFAULT_TIME_LIMIT_MIN,
         wall_limit_sec: float = DEFAULT_WALL_LIMIT_SEC, replay: dict | None = None,
         keep_dir: Path | None = None, sparring_bot: Path | None = None) -> dict:
    """Play one match and return its record. `match` has map, seed, aiseed, slot,
    civ, opp_civ, opponent (Petra level 0-12, see LEVELS) and optionally behavior
    (Petra's personality, default balanced)."""
    match = pin_map_settings(match)
    slot = int(match["slot"])
    other = 3 - slot
    ai = "legion_replay" if replay is not None else "legion"
    ai_diff = MEDIUM
    # Calibration only: Petra plays the LEGION slot.
    petra_dir = "petra_" + secrets.token_hex(8)  # unguessable, so no submitted AI can import it
    if sparring_bot is not None:  # bot against bot: no Petra in the match
        behaviors, rates, boosts = {}, {}, {}
        opp_ai, opp_diff = "legion_sparring", MEDIUM
    else:
        behaviors = {other: match.get("behavior") or DEFAULT_BEHAVIOR}
        rates, boosts = {}, {}
        _set_boost(match["opponent"], other, rates, boosts)
        opp_ai, opp_diff = petra_dir, petra_difficulty(match["opponent"])
    if match.get("candidate_petra") is not None and replay is None:
        level = int(match["candidate_petra"])
        ai, ai_diff = petra_dir, petra_difficulty(level)
        behaviors[slot] = match.get("candidate_behavior") or DEFAULT_BEHAVIOR
        _set_boost(level, slot, rates, boosts)
    boost = {"player": slot, "rate": float(match["candidate_boost"])} if match.get("candidate_boost") else None
    ids = _run_as()
    home = Path(tempfile.mkdtemp(prefix="legion-match-"))
    record = {**match, "result": None, "reason": None, "game_ms": None, "wall_sec": None,
              "commands": {}, "cheats_blocked": 0, "guard_blocked": [], "bot_errors": 0, "errors": 0}
    try:
        mods = home / ".local/share/0ad/mods"
        mods.mkdir(parents=True)
        limit_ms = int(time_limit_min * 60000)
        build_arena(mods, bot_dir, limit_ms, int(match["aiseed"]), petra_dir, replay=replay, boost=boost, petra_rates=rates,
                    petra_boosts=boosts,
                    sparring_bot=sparring_bot)
        _chown_tree(home, ids)
        civs = {slot: match["civ"], other: match["opp_civ"]}
        argv = [PYROGENESIS, "-mod=public", "-mod=legion_arena", f"-autostart=random/{match['map']}",
                f"-autostart-seed={match['seed']}", f"-autostart-aiseed={match['aiseed']}", "-autostart-players=2",
                f"-autostart-civ=1:{civs[1]}", f"-autostart-civ=2:{civs[2]}",
                f"-autostart-ai={slot}:{ai}", f"-autostart-aidiff={slot}:{ai_diff}",
                f"-autostart-ai={other}:{opp_ai}", f"-autostart-aidiff={other}:{opp_diff}",
                "-autostart-player=-1", "-autostart-nonvisual", "-autostart-victory=legion_conquest", "-quickstart"]
        argv += [f"-autostart-aibehavior={player}:{behavior}" for player, behavior in sorted(behaviors.items())]
        if match.get("biome"):
            argv.append(f"-autostart-biome={match['biome']}")
        if match.get("placement"):
            argv.append(f"-autostart-placement={match['placement']}")

        def demote():
            os.setsid()
            if ids:
                os.setgroups([])
                os.setgid(ids[1])
                os.setuid(ids[0])

        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache")}
        began = time.monotonic()
        proc = subprocess.Popen(argv, cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, preexec_fn=demote, text=True, errors="replace")
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            _kill(proc)

        timer = threading.Timer(wall_limit_sec, kill)
        timer.start()
        time_limit_hit = False
        tiebreak: dict[int, float] = {}
        bot_ready = False
        sparring_ready = False
        commands: dict[str, list] = {}
        log_tail: list[str] = []
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                m = LINE.match(line)
                if m:
                    kind, rest = m.groups()
                    if kind == "LEGION_CMD":
                        c = CMD.match(rest)
                        # Commands at or after the time limit race the shutdown, so they are not compared.
                        if c and int(c.group(1)) < limit_ms:
                            commands.setdefault(c.group(2), []).append([int(c.group(1)), c.group(3)])
                    elif kind == "LEGION_TIEBREAK":
                        try:
                            tiebreak = {int(k): float(v) for k, v in json.loads(rest).items()}
                        except (ValueError, AttributeError):
                            tiebreak = {}
                    elif kind == "LEGION_TIME_LIMIT":
                        time_limit_hit = True
                        record["game_ms"] = int(rest or 0)
                        _kill(proc)
                    elif kind == "LEGION_BOT_READY":
                        bot_ready = True
                    elif kind == "LEGION_SPARRING_READY":
                        sparring_ready = True
                    elif kind == "LEGION_CHEAT_BLOCKED":
                        record["cheats_blocked"] += 1
                    elif kind == "LEGION_GUARD_BLOCKED" and len(record["guard_blocked"]) < 20:
                        record["guard_blocked"].append(rest)
                    continue
                if line.startswith("ERROR"):
                    record["errors"] += 1
                    if BOT_ERROR.search(line):
                        record["bot_errors"] += 1
                if "JavaScript error" in line or BOT_ERROR.search(line):
                    log_tail.append(line)
                    log_tail = log_tail[-40:]
        finally:
            timer.cancel()
            proc.wait()
        record["wall_sec"] = round(time.monotonic() - began, 1)
        record["commands"] = commands
        record["log_tail"] = log_tail
        if time_limit_hit:
            # Decided on military score: the value of the enemy units each side killed and enemy structures it
            # destroyed. Equal values are a draw.
            ours, theirs = tiebreak.get(slot, 0.0), tiebreak.get(other, 0.0)
            record["tiebreak"] = {"yours": ours, "enemy": theirs}
            record["result"] = "win" if ours > theirs else "loss" if theirs > ours else "draw"
            record["reason"] = "time_limit"
        elif timed_out.is_set():
            record["result"], record["reason"] = "loss", "wall_limit"
        else:
            metas = glob.glob(str(home / ".local/share/0ad/replays" / GAME_VERSION / "*" / "metadata.json"))
            if not metas:
                record["result"], record["reason"] = "loss", f"no_result_exit_{proc.returncode}"
            else:
                meta = json.loads(Path(metas[0]).read_text())
                record["game_ms"] = meta.get("timeElapsed")
                states = [p.get("state") for p in meta.get("playerStates", [])]
                mine = states[slot] if slot < len(states) else None
                theirs = states[other] if other < len(states) else None
                if mine == "won" and theirs == "defeated":
                    record["result"], record["reason"] = "win", "conquest"
                elif theirs == "won":
                    record["result"], record["reason"] = "loss", "conquest"
                else:
                    record["result"], record["reason"] = "loss", f"unclear_states_{states}"
        # A bot that never loaded also stops the game's AI manager, so Petra idles
        # and the match would end as a draw; neither may count in the bot's favour.
        if ai == "legion" and not bot_ready:
            record["result"], record["reason"] = "loss", "bot_failed_to_load"
        elif record["result"] != "loss" and not commands.get(str(other)):
            record["result"], record["reason"] = "loss", "opponent_inactive"
        if sparring_bot is not None and not sparring_ready:
            record["result"], record["reason"] = "no_contest", "sparring_bot_failed_to_load"
        if keep_dir:
            keep_dir.mkdir(parents=True, exist_ok=True)
            for meta in glob.glob(str(home / ".local/share/0ad/replays" / GAME_VERSION / "*" / "*")):
                shutil.copy(meta, keep_dir / Path(meta).name)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return record


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- reproducibility check -----------------------------------------------------------

def replay_commands(record: dict) -> dict:
    """The bot's logged commands keyed by the AI-turn time that must post them."""
    out: dict[int, list] = {}
    for t, text in record["commands"].get(str(record["slot"]), []):
        out.setdefault(t - REPLAY_OFFSET_MS, []).append(json.loads(text))
    return {str(k): v for k, v in sorted(out.items())}


def reproduces(original: dict, bot_dir: Path, time_limit_min: float, wall_limit_sec: float) -> dict:
    """Re-play the match with only the bot's logged commands. An honest bot can
    only influence the game through its commands, so every player's command
    stream and the result must come out identical."""
    keys = ("map", "seed", "aiseed", "slot", "civ", "opp_civ", "opponent", "behavior", "biome", "placement")
    rerun = play({k: original[k] for k in keys if k in original}, bot_dir, time_limit_min, wall_limit_sec,
                 replay=replay_commands(original))
    same_result = rerun["result"] == original["result"]
    diffs = {}
    for player in sorted(set(original["commands"]) | set(rerun["commands"])):
        a, b = original["commands"].get(player, []), rerun["commands"].get(player, [])
        if a != b:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            diffs[player] = {"original": len(a), "rerun": len(b), "first_difference": first,
                             "at_ms": (a[first][0] if first < len(a) else b[first][0] if first < len(b) else None)}
    return {"ok": same_result and not diffs, "rerun_result": rerun["result"], "differences": diffs}


# --- gauntlet and rating ---------------------------------------------------------------

def run_gauntlet(config: dict, bot_dir: Path, jobs: int, verify: bool = True, log=print) -> list[dict]:
    limit = config.get("time_limit_min", DEFAULT_TIME_LIMIT_MIN)
    wall = config.get("wall_limit_sec", DEFAULT_WALL_LIMIT_SEC)
    matches = config["matches"]
    results: list[dict | None] = [None] * len(matches)
    done = 0

    def one(i):
        rec = play(matches[i], bot_dir, limit, wall)
        rec["index"] = i
        if verify and rec["result"] != "loss":
            rec["reproduction"] = reproduces(rec, bot_dir, limit, wall)
            if not rec["reproduction"]["ok"]:
                rec["unverified_result"] = rec["result"]
                rec["result"], rec["reason"] = "loss", "not_reproducible"
        return i, rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for i, rec in pool.map(one, range(len(matches))):
            results[i] = rec
            done += 1
            if log:
                log(f"[{done}/{len(matches)}] {rec['map']} seed={rec['seed']} vs petra-{LEVEL_NAMES[int(rec['opponent'])]}"
                    f"/{rec.get('behavior') or DEFAULT_BEHAVIOR} "
                    f"slot={rec['slot']} {rec['civ']}-v-{rec['opp_civ']}: {rec['result']} ({rec['reason']}, "
                    f"{(rec['game_ms'] or 0) / 60000:.1f} game-min, {rec['wall_sec']}s, bot errors {rec['bot_errors']})")
    return results


def _expected(r, rj):
    return 1.0 / (1.0 + 10 ** ((rj - r) / 400.0))


def fit_rating(games: list[tuple[float, float]], prior: tuple[float, float] | None) -> float:
    """Maximum-likelihood Elo from (opponent rating, score) pairs, with an
    optional prior of `weight` virtual draws against `rating`."""
    data = list(games)
    if prior:
        data += [(prior[0], 0.5)] * int(prior[1])
    if not data:
        return float("nan")
    lo, hi = -4000.0, 8000.0
    for _ in range(200):
        mid = (lo + hi) / 2
        grad = sum(s - _expected(mid, rj) for rj, s in data)
        if grad > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


SCORE_OF = {"win": 1.0, "draw": 0.5, "loss": 0.0}


SCALE_ZERO, SCALE_ONE = 1, 12  # the scale's end points: Very Easy and Elite 4


def level_score(rating: float, scale: dict[int, float]) -> float:
    """Headline score, linear in Elo: playing like Petra Very Easy scores 0 and
    like the strongest opponent, Elite 4, scores 1.0. A bot at 1.0 is
    expected to take about 94% of the points on the hidden gauntlet. Above 1.0
    the line continues; below Very Easy the score is 0. `scale` maps each level
    to its Elo, the average over Petra's three personalities."""
    zero, one = scale[SCALE_ZERO], scale[SCALE_ONE]
    return max(0.0, (rating - zero) / (one - zero))


def score_results(results: list[dict], ladder: dict, bootstrap: int = 500, seed: int = 0) -> dict:
    ratings = {k: float(v) for k, v in ladder["opponents"].items()}
    scale = {int(k): float(v) for k, v in ladder["scale"].items()}
    prior = (ladder["prior"]["rating"], ladder["prior"]["games"]) if ladder.get("prior") else None
    # Sandbox is calibrated as balanced only (practice runs); every other opponent has its own rating.
    games = [(ratings.get(opponent_key(r), ratings.get(f"{int(r['opponent'])}:{DEFAULT_BEHAVIOR}")), SCORE_OF[r["result"]])
             for r in results]

    rating = fit_rating(games, prior)
    rng = random.Random(seed)
    boot = sorted(fit_rating([rng.choice(games) for _ in games], prior) for _ in range(bootstrap)) if games else []

    def interval(values):
        return [round(values[int(0.025 * len(values))], 4), round(values[int(0.975 * len(values)) - 1], 4)] if values else None

    totals = {"wins": 0, "draws": 0, "losses": 0}
    per_level, per_behavior = {}, {}
    for r in results:
        outcome = {"win": "wins", "draw": "draws", "loss": "losses"}[r["result"]]
        for table, key in ((per_level, LEVEL_NAMES[int(r["opponent"])]), (per_behavior, r.get("behavior") or DEFAULT_BEHAVIOR)):
            row = table.setdefault(key, {"games": 0, "wins": 0, "draws": 0, "losses": 0, "points": 0.0})
            row["games"] += 1
            row[outcome] += 1
            row["points"] += SCORE_OF[r["result"]]
        totals[outcome] += 1
    for row in list(per_level.values()) + list(per_behavior.values()):
        row["points_pct"] = round(100 * row["points"] / row["games"], 1)
    points = sum(score for _, score in games)
    return {
        "score": round(level_score(rating, scale), 4),
        "score_ci95": interval([level_score(b, scale) for b in boot]),
        "elo": round(rating, 1),
        "elo_ci95": [round(x, 1) for x in interval(boot)] if boot else None,
        "points": points,
        "points_pct": round(100 * points / len(games), 1) if games else None,
        "record": totals,
        "per_level": {LEVEL_NAMES[l]: per_level[LEVEL_NAMES[l]] for l in sorted(LEVEL_NAMES) if LEVEL_NAMES[l] in per_level},
        "per_behavior": {b: per_behavior[b] for b in BEHAVIORS if b in per_behavior},
        "scale": {LEVEL_NAMES[l]: {"elo": scale[l], "score": round(level_score(scale[l], scale), 2)} for l in sorted(scale) if l >= SCALE_ZERO},
        "not_reproducible": sum(r.get("reason") == "not_reproducible" for r in results),
        "matches": len(results),
    }


def assign_behaviors(matches: list[dict], rng: random.Random) -> None:
    """Give each match a Petra personality: equal shares at every level."""
    by_level: dict[int, list[dict]] = {}
    for m in matches:
        by_level.setdefault(int(m["opponent"]), []).append(m)
    for group in by_level.values():
        cycle = [BEHAVIORS[i % len(BEHAVIORS)] for i in range(len(group))]
        rng.shuffle(cycle)
        for m, behavior in zip(group, cycle):
            m["behavior"] = behavior


def make_gauntlet(seed: int, maps: list[str], counts: dict[int, int], time_limit_min: float = DEFAULT_TIME_LIMIT_MIN) -> dict:
    rng = random.Random(seed)
    matches = []
    for level, n in sorted(counts.items()):
        for i in range(n):
            matches.append({"map": rng.choice(maps), "seed": rng.randrange(1, 10 ** 6), "aiseed": rng.randrange(1, 10 ** 6),
                            "slot": 1 + i % 2, "civ": rng.choice(CIVS), "opp_civ": rng.choice(CIVS), "opponent": level})
    assign_behaviors(matches, rng)
    rng.shuffle(matches)
    return {"time_limit_min": time_limit_min, "wall_limit_sec": DEFAULT_WALL_LIMIT_SEC, "matches": matches}


# --- practice (matches against Petra) ---------------------------------------------------------

PRACTICE_HELP = """Petra is not installed here. Practice matches against Petra are played on the hub, from one pool
shared by the whole team, through the board tools:
  practice_info()                      the pool, the rules and the match format
  practice_play(matches=[...], ref="main", path="legion", verify=True)
                                       plays your AI as committed at ref (a branch or commit you pushed);
                                       returns a job id
  practice_result(job_id)              the job's progress, then every match record and an estimated score
  practice_status()                    the pool and every job the team has ordered
Use `legion.py match ...` or `legion.py gauntlet ...` to print match specs for practice_play."""

MATCH_KEYS = ("map", "seed", "aiseed", "slot", "civ", "opp_civ", "opponent", "behavior", "biome", "placement")


def match_spec(m: dict) -> dict:
    return {k: m[k] for k in MATCH_KEYS if k in m}


def spar(matches: list[dict], bot_dir: Path, opponent_bot: Path, jobs: int,
         time_limit_min: float = DEFAULT_TIME_LIMIT_MIN, log=print) -> list[dict]:
    """Bot-against-bot matches, played here; they do not use the practice pool."""
    for d in (bot_dir, opponent_bot):
        if not (d / "main.js").is_file():
            raise SystemExit(f"{d}/main.js not found")
    results: list[dict | None] = [None] * len(matches)

    def one(i):
        rec = play(matches[i], bot_dir, time_limit_min, DEFAULT_WALL_LIMIT_SEC, sparring_bot=opponent_bot)
        rec["command_counts"] = {k: len(v) for k, v in rec.pop("commands").items()}
        return i, rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for i, rec in pool.map(one, range(len(matches))):
            results[i] = rec
            if log:
                log(f"[{i + 1}/{len(matches)}] {rec['map']} seed={rec['seed']} slot={rec['slot']} "
                    f"{rec['civ']}-v-{rec['opp_civ']}: {rec['result']} ({rec['reason']}, "
                    f"{(rec['game_ms'] or 0) / 60000:.1f} game-min, {rec['wall_sec']}s)")
    return results


# --- command line ------------------------------------------------------------------------

def _strip(results):
    """Drop bulky command logs before saving."""
    return [{k: v for k, v in r.items() if k != "commands"} for r in results]


def _report(results, ladder_path: Path):
    for rec in results:
        print(f"{rec.get('map')} seed={rec.get('seed')} vs petra-{LEVEL_NAMES.get(int(rec.get('opponent') or 0), '?')}"
              f"/{rec.get('behavior') or DEFAULT_BEHAVIOR} slot={rec.get('slot')}: {rec.get('result')} ({rec.get('reason')})")
    scored = [r for r in results if r.get("result") in SCORE_OF]
    if ladder_path.exists() and scored:
        print(json.dumps(score_results(scored, json.loads(ladder_path.read_text())), indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("budget", "selfcheck", "practice"):
        sub.add_parser(name, help="how to play practice matches against Petra (board tools)")
    m = sub.add_parser("match", help="print one match spec against Petra, for practice_play")
    m.add_argument("--opponent", default="medium", choices=[k for k in LEVELS if k != "sandbox"])
    m.add_argument("--behavior", default=DEFAULT_BEHAVIOR, choices=BEHAVIORS, help="Petra's personality")
    m.add_argument("--map", default="mainland")
    m.add_argument("--seed", type=int, default=1)
    m.add_argument("--aiseed", type=int)
    m.add_argument("--slot", type=int, default=1, choices=[1, 2])
    m.add_argument("--civ", default="athen")
    m.add_argument("--opp-civ", default="brit")
    g = sub.add_parser("gauntlet", help="print a list of match specs (default: the dev gauntlet), for practice_play")
    g.add_argument("--config", type=Path, default=HERE / "dev_gauntlet.json")
    g.add_argument("--limit", type=int, help="only the first N matches")
    sp = sub.add_parser("spar", help="your AI against another LEGION AI, played here (not counted)")
    sp.add_argument("--bot", type=Path, default=DEFAULT_BOT)
    sp.add_argument("--opponent-bot", type=Path, required=True, help="directory of the other AI (with main.js)")
    sp.add_argument("--games", type=int, default=1)
    sp.add_argument("--map", default="mainland")
    sp.add_argument("--seed", type=int, default=1, help="first seed; game i uses seed + i")
    sp.add_argument("--civ", default="athen")
    sp.add_argument("--opp-civ", default="brit")
    sp.add_argument("--time-limit-min", type=float, default=DEFAULT_TIME_LIMIT_MIN)
    sp.add_argument("--jobs", type=int, default=default_jobs())
    sp.add_argument("--out", type=Path)
    sc = sub.add_parser("score", help="score saved match records (for example the records practice_result returns)")
    sc.add_argument("results", type=Path)
    sc.add_argument("--ladder", type=Path, default=DEFAULT_LADDER)
    args = p.parse_args()

    if args.cmd in ("budget", "selfcheck", "practice"):
        print(PRACTICE_HELP)
        return
    if args.cmd == "match":
        match = {"map": args.map, "seed": args.seed, "aiseed": args.aiseed or args.seed, "slot": args.slot,
                 "civ": args.civ, "opp_civ": args.opp_civ, "opponent": LEVELS[args.opponent], "behavior": args.behavior}
        map_options(args.map)  # an unknown map fails here, not on the hub
        print(json.dumps(match_spec(match)))
        return
    if args.cmd == "gauntlet":
        config = json.loads(args.config.read_text())
        matches = config["matches"][:args.limit] if args.limit else config["matches"]
        print(json.dumps([match_spec(x) for x in matches]))
        return
    if args.cmd == "score":
        results = json.loads(args.results.read_text())
        if isinstance(results, dict):
            results = results.get("records") or []
        _report(results, args.ladder)
        return
    matches = [{"map": args.map, "seed": args.seed + i, "aiseed": args.seed + i, "slot": 1 + i % 2,
                "civ": args.civ, "opp_civ": args.opp_civ} for i in range(args.games)]
    results = spar(matches, args.bot, args.opponent_bot, args.jobs, args.time_limit_min)
    counts = {k: sum(r["result"] == k for r in results) for k in ("win", "draw", "loss", "no_contest")}
    print(json.dumps({"games": len(results), **counts}, indent=2))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()
