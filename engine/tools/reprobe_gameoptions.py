"""Re-probe the server program's game options in the format of tests/data/game-options-probe.txt.

From engine/, after a Project Reclaimer release:
    python tools/reprobe_gameoptions.py <project-reclaimer.exe> <a server folder> tests/data/game-options-probe.txt new.txt
then compare new.txt with the old file, move it over tests/data/game-options-probe.txt and run
tools/gen_gameoptions.py. The server folder needs a dedicated.toml and Guardian in its game folder (any folder made
by `dedicated init --from` does); the probe writes only probe-go/ and playlists/probe-go.playlist.json there, and
takes them out again. About 20 seconds.

It repeats every probe the old file holds (mode-check with the option at 99999 gives its label and range; playlist
show on games naming each number gives each number's label), tries the option keys the program's strings name that
the old file doesn't, and keeps the old file's order, so the window's lists keep theirs. 0.9.20's probe found the
same keys and ranges as 0.9.18's, a new top-level `odst` in saved custom games, and two choice labels renamed in
0.9.19 ("Vanilla characters" is "Halo 3", "On with firebombs" is "On, with firebombs").
"""
import json
import shutil
import re
import subprocess
import sys
from pathlib import Path

EXE, FOLDER, OLD, NEW = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
VERSION = subprocess.run([EXE, "--version"], capture_output=True, text=True).stdout.split()[-1]
EMPTY_SCRIPT = {"version": 1, "name": "check", "initial": [], "on_spawn": [], "on_kill": [], "on_death": []}
WORK = FOLDER / "probe-go"
WORK.mkdir(exist_ok=True)
MODES = ["Slayer", "CaptureTheFlag", "Oddball", "KingOfTheHill", "Vip", "Juggernaut", "Territories", "Assault",
         "Infection", "EscalationSlayer"]


def mode_check(mode: str, options: dict | None = None, extra: dict | None = None) -> str:
    data = {"version": 1, "name": "probe", "mode": mode, "rules": {}, "options": options or {}, "script": EMPTY_SCRIPT}
    data.update(extra or {})
    path = WORK / "probe.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    out = subprocess.run([EXE, "mode-check", str(path)], capture_output=True, text=True, cwd=FOLDER)
    lines = [l.strip() for l in (out.stdout + out.stderr).splitlines() if l.strip()]
    return lines[-1] if lines else "(no output)"


old = OLD.read_text(encoding="utf-8").splitlines()
generic, traits, per_set, mode_keys, enum_keys = [], [], [], [], []
section = ""
for line in old:
    if line.startswith("### ") or line.startswith("## "):
        section = line
        continue
    if "generic keys" in section and (m := re.match(r"^(\S+)\s+Error", line)):
        generic.append(m.group(1))
    elif "trait keys" in section and (m := re.match(r"^(\S+)\s+Error", line)):
        traits.append(m.group(1))
    elif "one trait" in section and (m := re.match(r"^(\S+)\s+\[(\w+)\] Error", line)):
        per_set.append((m.group(2), m.group(1)))
    elif "mode.* keys" in section and (m := re.match(r"^(\w+)\s+(mode\.\w+)\s+Error", line)):
        mode_keys.append((m.group(1), m.group(2)))
    elif "playlist show" in section and (m := re.match(r"^(\w+) (\S+) \[", line)):
        enum_keys.append((m.group(1), m.group(2)))

# Keys the program's strings name that the old probe never tried.
blob = Path(EXE).read_bytes()
named = set(re.findall(rb"\b((?:rounds|respawn|powerups|map|teams|player|mode)\.[a-z][a-z_]{2,40})", blob))
named = {n.decode() for n in named}
known = set(generic) | set(traits) | {k for _, k in mode_keys}
candidates = sorted(n for n in named if n not in known)

out = [f"# Probe output behind plans/game-variants.md (re-probed against server program {VERSION})", "",
       "## mode-check: one candidate option per game type at 99999 -> label and range, or unknown", "",
       "### generic keys (mode Slayer)"]
for key in generic:
    out.append(f"{key:<40} {mode_check('Slayer', {key: 99999})}")
out.append("### trait keys on player.* (Slayer)")
for key in traits:
    out.append(f"{key:<40} {mode_check('Slayer', {key: 99999})}")
out.append("### one trait (speed) on every set, per a mode where it applies")
for mode, key in per_set:
    out.append(f"{key:<40} [{mode}] {mode_check(mode, {key: 99999})}")
out.append("### mode.* keys on every mode (only accepted ones printed)")
mode_candidates = sorted({k for _, k in mode_keys} | {c for c in candidates if c.startswith("mode.")})
for mode in MODES:
    first = [k for m, k in mode_keys if m == mode]  # the old file's order (the screens' order), then the rest
    for key in first + [k for k in mode_candidates if k not in first]:
        answer = mode_check(mode, {key: 99999})
        if "must be" in answer:
            out.append(f"{mode:<18} {key:<33} {answer}")
out.append("### top-level extras")
for line in old:
    if line.startswith(("world+author+hosting:", "minutes 999:", "score_to_win 501:")):
        out.append(line + "   (carried over from the earlier probe)")
out.insert(len(out) - 1, "unknown top-level key: " + mode_check("Slayer", extra={"bogus_key": 1}))
out.append("")
new_keys = []
for key in candidates:
    if key.startswith("mode."):
        continue
    answer = mode_check("Slayer", {key: 99999})
    if "Unknown game option" not in answer:
        new_keys.append(f"{key:<40} {answer}")
out.append("### keys named in the program that the old probe didn't try (Slayer)")
out.extend(new_keys or ["(none accepted)"])

# Ranges as they are now, for the playlist pass.
ranges = {}
for line in out:
    m = re.match(r"^(\S+)\s+(?:\[(\w+)\] )?Error: (.+?) must be (-?\d+)\.\.=(-?\d+)$", line)
    if m:
        ranges[("Slayer" if not m.group(2) else m.group(2), m.group(1))] = (m.group(3), int(m.group(4)), int(m.group(5)))
    m = re.match(r"^(\w+)\s+(mode\.\w+)\s+Error: (.+?) must be (-?\d+)\.\.=(-?\d+)$", line)
    if m:
        ranges[(m.group(1), m.group(2))] = (m.group(3), int(m.group(4)), int(m.group(5)))

games, wanted = [], []
GAME_NAMES = {"Slayer": "Slayer", "CaptureTheFlag": "Capture the Flag", "Oddball": "Oddball",
              "KingOfTheHill": "King of the Hill", "Vip": "VIP", "Juggernaut": "Juggernaut",
              "Territories": "Territories", "Assault": "Assault", "Infection": "Infection"}
for mode, key in enum_keys:
    lookup = (mode, key) if (mode, key) in ranges else ("Slayer", key)
    if key.startswith("leader."):
        lookup = ("Slayer", "player." + key.split(".", 1)[1])
    if lookup not in ranges:
        out.append(f"# no range for {mode} {key}")
        continue
    label, low, high = ranges[lookup]
    wanted.append((mode, key, label, low, high))
    for value in range(low, high + 1):
        games.append((mode, key, value))

labels: dict[tuple[str, str], dict[int, str]] = {}
for start in range(0, len(games), 120):
    chunk = games[start:start + 120]
    while len(chunk) < 3:
        chunk.append(chunk[-1])
    playlist = {"name": "probe", "games": [{"map": "Guardian", "game": GAME_NAMES[m], "options": {k: v}}
                                           for m, k, v in chunk]}
    path = FOLDER / "playlists" / "probe-go.playlist.json"
    path.write_text(json.dumps(playlist), encoding="utf-8")
    shown = subprocess.run([EXE, "dedicated", "playlist", "show", str(path)], capture_output=True, text=True,
                           cwd=FOLDER).stdout.splitlines()
    index = -1
    for line in shown:
        if re.match(r"^\s+\d+\. ", line):
            index = int(line.split(".")[0])
            continue
        if index > 0 and line.startswith("        ") and not line.strip().startswith("PROBLEM"):
            mode, key, value = chunk[index - 1]
            text = line.strip()
            label = next(w[2] for w in wanted if w[0] == mode and w[1] == key)
            if text.startswith(label + ": "):
                labels.setdefault((mode, key), {})[value] = text[len(label) + 2:]
            else:
                labels.setdefault((mode, key), {})[value] = "?" + text
path.unlink()
shutil.rmtree(WORK, ignore_errors=True)

out += ["", "## playlist show: numbers -> labels for every enum option", "", f"games: {len(games)}", ""]
for mode, key, label, low, high in wanted:
    found = labels.get((mode, key), {})
    parts = " | ".join(f"{v}={found.get(v, '?')}" for v in range(low, high + 1))
    out.append(f"{mode} {key} [{label}] {low}..{high}: {parts}")
    out.append("")
NEW.write_text("\n".join(out), encoding="utf-8")
print(f"{len(out)} lines; {len(candidates)} candidate keys, {len(new_keys)} new accepted")
