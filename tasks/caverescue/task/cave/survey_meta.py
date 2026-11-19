"""Per-survey metadata from the .svx sources: title, date, team, and the surveyors' notes.

    survey_meta.py CAVE_DIR CAVE.json   (adds "surveys" to the cave json and tags passages)

Notes come from comments and the question-mark (QM) lists, which describe
passage character and unexplored leads in the surveyors' own words.
"""
import json, os, re, sys

WET = re.compile(r"\b(sump|sumps|duck|ducks|stream|streamway|river|pool|pools|wet|water|flood|flooded|lake|canal|drip|waterfall|inlet)\b", re.I)

def parse(path):
    meta = {"file": os.path.basename(path), "title": "", "date": "", "team": [], "notes": [], "qms": []}
    in_qm = False
    for raw in open(path, encoding="utf-8", errors="replace"):
        ln = raw.rstrip("\n")
        s = ln.strip()
        m = re.match(r"\*title\s+\"?([^\"]+)\"?", s, re.I)
        if m: meta["title"] = m.group(1).strip()
        m = re.match(r"\*date\s+(\S+)", s, re.I)
        if m and not meta["date"]: meta["date"] = m.group(1)
        m = re.match(r"\*team\s+\"([^\"]+)\"", s, re.I)
        if m: meta["team"].append(m.group(1))
        if s.startswith(";"):
            c = s.lstrip(";").strip()
            if re.search(r"question mark list", c, re.I): in_qm = True; continue
            if not c or c.startswith("-") or re.match(r"(if using|repeat stations|new \*data|LRUDs|recorded station|Station Left|\(NP=|the #number|stations linked|Centreline|Cave:|Area in cave)", c, re.I): continue
            if in_qm and re.match(r"(The nearest|QM doesn|nearest-station|resolution|been checked|include any|Serial number|grade)", c, re.I): continue
            if re.match(r"^Q?M?\d|^\d{4}-|^QM", c) and in_qm: meta["qms"].append(c)
            elif len(c) > 12: meta["notes"].append(c)
    meta["wet"] = bool(WET.search(" ".join(meta["notes"] + meta["qms"] + [meta["title"]])))
    return meta

def main(cave_dir, cave_json):
    g = json.load(open(cave_json))
    surveys, begins = {}, {}
    for root, _, files in os.walk(cave_dir):
        for f in sorted(files):
            if f.endswith(".svx"):
                rel = os.path.relpath(os.path.join(root, f[:-4]), cave_dir).lower()
                surveys[rel] = parse(os.path.join(root, f))
                for ln in open(os.path.join(root, f), encoding="utf-8", errors="replace"):
                    m = re.match(r"\s*\*begin\s+(\S+)", ln, re.I)
                    if m: begins.setdefault(m.group(1).lower(), rel)
    used = {}
    for p in g["passages"]:
        parts = p["survey"].lower().split(".")[1:]
        key = ""
        for n in range(len(parts), 0, -1):
            if "/".join(parts[:n]) in surveys: key = "/".join(parts[:n]); break
        if not key:
            for part in reversed(parts):
                if part in begins: key = begins[part]; break
        p["survey_key"] = key if key in surveys else ""
        if key in surveys: used[key] = surveys[key]
    g["surveys"] = used
    json.dump(g, open(cave_json, "w"))
    years = {}
    for k, v in used.items(): years[v["date"][:4]] = years.get(v["date"][:4], 0) + 1
    print(f"surveys {len(used)}  wet {sum(v['wet'] for v in used.values())}  qms {sum(len(v['qms']) for v in used.values())}  years {dict(sorted(years.items()))}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
