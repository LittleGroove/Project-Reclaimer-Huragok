"""Add this version's notes to huragok/templates/notes-history.json; with --tags, those of released versions too.

The history lists every note line a version of Huragok has written in a host's files (settings.toml, commands.toml,
automod.txt, names.txt, the intervalmessages.txt files), by the thing each was written above, so that a later
version knows its own lines from the host's and brings them up to date (huragok/updates/notes.py). It only ever
grows: a line written once is Huragok's for good. Run it after changing a note (tests/updates/test_notes.py fails
until you do):

    python tools/notes_history.py

The history began with the releases since 0.4.0, the first that went out to hosts (a release's commands.toml is
written by its own code, run from the tag):

    python tools/notes_history.py --tags v0.4.0 v0.4.1 v0.4.2 v0.4.3 v0.4.4
"""
import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from huragok import __version__  # noqa: E402
from huragok.updates import notes  # noqa: E402

OUT = ENGINE / "huragok" / notes.HISTORY
ABOUT = ("Every note line Huragok has written in a host's files, by the thing it was written above: "
         "tools/notes_history.py adds a version's. Never take a line out: hosts' files still have it.")
RENDER = """\
import sys
sys.path.insert(0, sys.argv[1])
try:
    from huragok.core.commandsfile import render_commands_toml
except ImportError:  # 0.4.x
    from huragok.permissions import render_commands_toml
sys.stdout.buffer.write(render_commands_toml().encode("utf-8"))
"""


def released(tag: str) -> dict[str, str]:
    """Each file as that release's `init` wrote it."""
    texts: dict[str, str] = {}
    for kind, template in notes.TEMPLATES.items():
        shown = subprocess.run(["git", "show", f"{tag}:engine/huragok/{template}"], cwd=ENGINE, capture_output=True)
        if shown.returncode == 0:
            texts[kind] = notes.render(kind, shown.stdout.decode("utf-8"))
    archive = subprocess.run(["git", "archive", tag, "huragok"], cwd=ENGINE, capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as folder:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(folder, filter="data")
        done = subprocess.run([sys.executable, "-I", "-c", RENDER, folder], capture_output=True, check=True)
    texts[notes.COMMANDS] = done.stdout.decode("utf-8")
    return texts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tags", nargs="*", default=[], help="released versions to add (git tags, like v0.4.0)")
    args = parser.parse_args()
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    files = data.get("files", {})
    versions = data.get("versions", [])
    sources = [(tag.removeprefix("v"), released(tag)) for tag in args.tags]
    sources.append((__version__, {kind: notes.template_text(kind) for kind in notes.KINDS}))
    for version, texts in sources:
        grew = [kind for kind, text in texts.items() if notes.merged(files, kind, notes.collect(kind, text))]
        if version not in versions:
            versions.append(version)
        print(f"{version}: " + (f"new lines in {', '.join(grew)}" if grew else "nothing new"))
    OUT.write_text(json.dumps({"about": ABOUT, "versions": versions, "files": files}, indent=1, ensure_ascii=False)
                   + "\n", encoding="utf-8")
    print(f"Written: {OUT.relative_to(ENGINE)}")


if __name__ == "__main__":
    main()
