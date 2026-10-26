"""Why the server program skipped a mod map, found by making its own checks on the .map.

Its console says only `Skipped map package <path>: <reason>`, and one reason covers many
faults: "its tag index is damaged" is any of seven. check() makes the program's checks
(0.9.21's src/mcc_mod.rs, read from its build on 2026-10-20) in the program's order, so
the first that fails is the one that skipped the map, and explain() says what it is in
words a host can pass on to the map's author. A map that passes them all was replaced
after the server looked (a newer version), or the program checks more than this module
knows; explain() says so rather than guess.

A Halo 3 map as MCC ships it (cache version 13), all little-endian:

  0x0000  "head" ("daeh" on disk), the version, the length the file should be
  0x00C0  the map's name; 0x00E0 its scenario's path
  0x0020  the tag names: count, buffer address, buffer size, index address (32-bit, in the
          debug section)
  0x02E0  the tag data's address in memory (64-bit): the tag base
  0x02E8  the tag index header's address (64-bit)
  0x04CC  four section offsets, then at 0x04DC four sections {address, size}: debug,
          resources, tags, localization. A section starts in the file at its address plus
          its offset (32-bit): the tags at 0x4EC + 0x4D4, 0x4F0 bytes long
  0x3FFC  "foot" ("toof"): the header is 16 KB
  the tag index header (32 bytes): group count, groups' address, tag count, tags' address
  a group (16 bytes): its four letters first ("scnr" is "rncs" on disk)
  a tag (8 bytes): its group's number (16-bit), a salt, its data's address / 4 (32-bit)

The program reads the header, the tag index header, the group table, the tag table and
the first 16 bytes of the first scenario tag. Each read must lie inside the tag data (from
the base, as long as the tags section) and inside the file: outside the tag data is "its
tag index is damaged", past the end of the file "its cache is damaged". A few hundred KB
are read at most, whatever the map's size.
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

HEADER = 0x4000
VERSION = 13  # Halo 3 as MCC ships it
MAX_GROUPS = 1024
MAX_TAGS = 32768
SCENARIO = b"rncs"  # the scnr group's letters as they lie on disk
RECEIPT = "reclaimer-workshop-version.json"  # the server program's note of the Workshop version it installed

# The program's reasons for a map it can't read (others, such as a missing .mapinfo, aren't about the file).
NOT_HALO3 = "it is not a Halo 3 cache"
CUT_SHORT = "its cache is damaged"
DAMAGED_INDEX = "its tag index is damaged"
NO_SCENARIO = "it has no scenario"
CACHE_REASONS = (NOT_HALO3, CUT_SHORT, DAMAGED_INDEX, NO_SCENARIO)


@dataclass(frozen=True)
class Verdict:
    """What the program's checks find in one .map. reason: what its console says ("" when the map passes them all);
    detail: what's wrong, or for a map that passes, what it holds."""

    reason: str
    detail: str
    title: str = ""  # the map's own name, from its header
    map_type: int | None = None  # its scenario's: 0 campaign, 1 multiplayer, 2 main menu (None: not read)
    map_id: int | None = None  # the map ID its scenario declares (0: none; the server makes one from its file name)


class _Fault(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def check(path: Path) -> Verdict:
    """The program's checks on one .map, in its order: the first that fails, or a Verdict with no reason.
    OSError: the file couldn't be opened or read."""
    with path.open("rb") as file:
        return _Map(file, os.fstat(file.fileno()).st_size).check()


def explain(path: Path, said: str, *, updates: bool = True) -> str:
    """Why the server skipped this map and what to do, for the log and the window. said: the server's reason;
    updates: the server installs new Workshop versions by itself."""
    upload = upload_text(path)
    if said not in CACHE_REASONS:  # about how the item is packed (its .mapinfo…), not the map file
        return f"the item's own packaging{upload}; ask its author"
    try:
        verdict = check(path)
    except OSError as error:
        return f"Huragok couldn't read the file to see more: {error.strerror or error}"
    if not verdict.reason:
        return (f"Huragok finds nothing wrong with the file there now ({verdict.detail}): it was replaced after the "
                "server looked (a newer version?), or the server checks more than Huragok knows; its next start "
                "shows which")
    detail = verdict.detail
    if verdict.reason != said:
        detail += f" (the server said {said!r}: the file has changed since)"
    if verdict.reason == CUT_SHORT:
        return (f"{detail}. Fetch or copy it again; if it comes back the same, the upload itself is short: ask its "
                f"author{upload} to upload it again")
    fixed = "; Huragok installs the fixed version when it comes out" if upload and updates else ""
    return f"{detail}. The map file itself is broken, not your setup: ask its author{upload} for a fixed upload{fixed}"


def upload_text(path: Path) -> str:
    """' (Workshop 123, the version uploaded 20 Oct 2026 05:17 UTC)' for a map in content/Mods/Workshop/<item>/;
    '' for any other map."""
    for folder in path.parents:
        if folder.name.isdecimal() and folder.parent.name.lower() == "workshop":
            try:
                updated = json.loads((folder / RECEIPT).read_text(encoding="utf-8")).get("updated")
            except (OSError, ValueError, AttributeError):
                updated = None
            when = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(updated)) if isinstance(updated, int) else ""
            return f" (Workshop {folder.name}, the version uploaded {when})" if when else f" (Workshop {folder.name})"
    return ""


class _Map:
    def __init__(self, file: BinaryIO, size: int) -> None:
        self.file = file
        self.size = size
        self.title = ""
        self.declared = 0  # the length its header says the file is
        self.base = 0
        self.tags_size = 0
        self.tags_file = 0

    def check(self) -> Verdict:
        try:
            return self._check()
        except _Fault as fault:
            return Verdict(fault.reason, fault.detail, self.title)

    def _check(self) -> Verdict:
        if self.size < HEADER:
            raise _Fault(CUT_SHORT, f"the file is only {self.size:,} bytes, less than a map's 16 KB header: it was "
                                    "cut short, or isn't a map")
        head = self._read(0, HEADER, "its header")
        magic, version, self.declared = struct.unpack_from("<4sII", head, 0)
        self.title = _text(head[0xC0:0xE0])
        if magic == b"head":
            raise _Fault(NOT_HALO3, "it is an Xbox 360 map (its bytes run the other way): the server takes MCC's")
        if magic != b"daeh":
            raise _Fault(NOT_HALO3, f"it doesn't start as a Halo 3 map does: its first four bytes are {_shown(magic)}, "
                                    "where a map has 'daeh' ('head' backwards)")
        if version != VERSION:
            raise _Fault(NOT_HALO3, f"it is a version {version} map; the server takes version {VERSION}, Halo 3 as "
                                    "MCC ships it (this one was built for another game or another MCC release)")
        if head[-4:] != b"toof":
            raise _Fault(NOT_HALO3, "its 16 KB header doesn't end with 'foot' as a Halo 3 map's does: the header is "
                                    "damaged")
        self.base, index = struct.unpack_from("<QQ", head, 0x2E0)
        tags_address, self.tags_size = struct.unpack_from("<II", head, 0x4EC)
        self.tags_file = (tags_address + struct.unpack_from("<I", head, 0x4D4)[0]) & 0xFFFFFFFF
        if not self.tags_size:
            raise _Fault(DAMAGED_INDEX, "its tags section is empty, so it has no tag index: it isn't a playable map "
                                        "(Halo 3's shared.map is like this), or it wasn't built in full")
        header = self._tags(index, 32, "its tag index header")
        groups, groups_at, count, tags_at = struct.unpack_from("<I4xQI4xQ", header)
        if groups > MAX_GROUPS or count > MAX_TAGS:
            listed = [f"{count:,} tags (the server takes at most {MAX_TAGS:,})"] if count > MAX_TAGS else []
            listed += [f"{groups:,} tag groups (at most {MAX_GROUPS:,})"] if groups > MAX_GROUPS else []
            raise _Fault(DAMAGED_INDEX, "its tag index lists " + " and ".join(listed))
        what = f"its tag group table ({groups:,} groups)"
        if not any(header):
            what = "its tag index header is all zeros (the tag data is missing), so its tag group table"
        table = self._tags(groups_at, groups * 16, what)
        scenario = next((n for n in range(groups) if table[16 * n:16 * n + 4] == SCENARIO), None)
        if scenario is None:
            raise _Fault(NO_SCENARIO, f"none of its {groups:,} tag groups is a scenario ('scnr'): there's no level "
                                      "in it")
        entries = self._tags(tags_at, count * 8, f"its tag table ({count:,} tags)")
        number = next((n for n in range(count) if struct.unpack_from("<h", entries, 8 * n)[0] == scenario), None)
        if number is None:
            raise _Fault(NO_SCENARIO, f"none of its {count:,} tags is a scenario ('scnr'): there's no level in it")
        name = self._tag_name(head, number)
        start = self._tags(struct.unpack_from("<I", entries, 8 * number + 4)[0] * 4, 16,
                           f"its scenario tag (tag {number}{', ' + name if name else ''})")
        map_type, map_id = struct.unpack_from("<h", start, 0)[0], struct.unpack_from("<i", start, 0xC)[0]
        return Verdict("", f"{count:,} tags, and its scenario reads", self.title, map_type, map_id)

    def _read(self, offset: int, length: int, what: str) -> bytes:
        """length bytes at offset; a read past the end of the file is the program's 'its cache is damaged'."""
        if offset + length > self.size:
            raise _Fault(CUT_SHORT, self._short(what, offset + length))
        self.file.seek(offset)
        data = self.file.read(length)
        if len(data) < length:  # it shrank while being read
            raise _Fault(CUT_SHORT, self._short(what, offset + length))
        return data

    def _tags(self, address: int, length: int, what: str) -> bytes:
        """length bytes of tag data at a memory address: outside the tag data is 'its tag index is damaged'."""
        offset = address - self.base
        end = self.base + self.tags_size
        if offset < 0:
            raise _Fault(DAMAGED_INDEX, f"{what} is at {address:#x}, before the map's tag data, which starts at "
                                        f"{self.base:#x}")
        if offset + length > self.tags_size:
            where = f"starts at {address:#x}, past" if offset >= self.tags_size else f"runs to {address + length:#x}, past"
            raise _Fault(DAMAGED_INDEX, f"{what} {where} the end of the map's tag data at {end:#x}")
        return self._read(self.tags_file + offset, length, what)

    def _short(self, what: str, end: int) -> str:
        text = f"the file ends after {self.size:,} bytes but {what} runs to byte {end:,}"
        if self.declared > self.size:
            text += (f"; its header says it is {self.declared:,} bytes, so the file was cut short (a download, copy or "
                     "upload that didn't finish)")
        return text

    def _tag_name(self, head: bytes, number: int) -> str:
        """A tag's path (levels\\multi\\…\\name), from the debug section; '' when it can't be read."""
        try:
            count, names_at, names_size, index_at = struct.unpack_from("<IIII", head, 0x20)
            debug = struct.unpack_from("<I", head, 0x4CC)[0]  # the debug section's offset: file = address + offset
            if number >= count:
                return ""
            where = (index_at + debug) & 0xFFFFFFFF
            start = struct.unpack_from("<I", self._read(where + 4 * number, 4, "a tag name"), 0)[0]
            if start >= names_size:
                return ""
            at = (names_at + debug) & 0xFFFFFFFF
            return _text(self._read(at + start, min(256, names_size - start), "a tag name"))
        except (_Fault, struct.error):
            return ""


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _shown(raw: bytes) -> str:
    """Four bytes as the letters they spell, or in hex when they aren't letters."""
    return repr(raw.decode("ascii")) if all(32 <= b < 127 for b in raw) else raw.hex(" ")
