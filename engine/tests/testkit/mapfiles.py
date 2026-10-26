"""Halo 3 map files made byte by byte: good ones, and ones broken each way the server program refuses."""

from __future__ import annotations

import struct
from pathlib import Path


BASE = 0x1CA1A0000  # where the tag data is in memory (a real map's)


TAGS_FILE, TAGS_ADDRESS, TAGS_SIZE = 0x4000, 0x77027000, 0x1000


DEBUG_FILE, DEBUG_ADDRESS = 0x5000, 0x7CE47000


TABLE, GROUPS, INDEX, SCENARIO = 0x000, 0x100, 0x200, 0x300  # where each lies in the tag data


NAMES = [b"globals\\globals", b"levels\\multi\\test\\test", b"shaders\\default"]


UPLOADED = 1792475855  # 20 Oct 2026 05:57:35 UTC: the version that put HugeGrass Rebalance right


def halo3_map() -> bytearray:
    """A small Halo 3 map as MCC ships them, which passes every check: three tags, the second its scenario."""
    data = bytearray(DEBUG_FILE + 0x100)
    struct.pack_into("<4sII", data, 0, b"daeh", 13, len(data))
    struct.pack_into("<IIII", data, 0x20, len(NAMES), DEBUG_ADDRESS + 0x10, 0x80, DEBUG_ADDRESS)
    data[0xC0:0xC8] = b"Test Map"
    data[0xE0:0xF5] = b"levels\\multi\\test\\test"
    struct.pack_into("<QQ", data, 0x2E0, BASE, BASE + INDEX)
    struct.pack_into("<I", data, 0x4CC, (DEBUG_FILE - DEBUG_ADDRESS) & 0xFFFFFFFF)
    struct.pack_into("<I", data, 0x4D4, (TAGS_FILE - TAGS_ADDRESS) & 0xFFFFFFFF)
    struct.pack_into("<II", data, 0x4DC, DEBUG_ADDRESS, 0x100)
    struct.pack_into("<II", data, 0x4EC, TAGS_ADDRESS, TAGS_SIZE)
    data[0x3FFC:0x4000] = b"toof"
    for number, group in enumerate((0, 2, 1)):  # matg, scnr, bitm
        struct.pack_into("<hHI", data, TAGS_FILE + TABLE + 8 * number, group, 0xE175 + number,
                         (BASE + SCENARIO) // 4 if group == 2 else (BASE + 0x400) // 4)
    for number, letters in enumerate((b"gtam", b"mtib", b"rncs")):
        data[TAGS_FILE + GROUPS + 16 * number:TAGS_FILE + GROUPS + 16 * number + 4] = letters
    struct.pack_into("<I4xQI4xQ", data, TAGS_FILE + INDEX, 3, BASE + GROUPS, 3, BASE + TABLE)
    struct.pack_into("<h", data, TAGS_FILE + SCENARIO, 1)  # a multiplayer scenario
    at = 0
    for number, name in enumerate(NAMES):
        struct.pack_into("<I", data, DEBUG_FILE + 4 * number, at)
        data[DEBUG_FILE + 0x10 + at:DEBUG_FILE + 0x10 + at + len(name)] = name
        at += len(name) + 1
    return data


def written(tmp_path: Path, data: bytes | bytearray, name: str = "test.map") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


def broken(change: str) -> bytearray:
    data = halo3_map()
    if change == "short":
        return data[:0x1000]
    if change == "cut":
        return data[:TAGS_FILE + 0x180]  # before its tag index header; the header still says the whole length
    if change == "zip":
        data[0:4] = b"PK\x03\x04"
    elif change == "xbox":
        data[0:4] = b"head"
    elif change == "version":
        struct.pack_into("<I", data, 4, 12)
    elif change == "foot":
        data[0x3FFC:0x4000] = bytes(4)
    elif change == "empty":
        struct.pack_into("<I", data, 0x4F0, 0)
    elif change == "index low":
        struct.pack_into("<Q", data, 0x2E8, BASE - 0x1000)
    elif change == "index high":
        struct.pack_into("<Q", data, 0x2E8, BASE + TAGS_SIZE + 0x40)
    elif change == "tags":
        struct.pack_into("<I", data, TAGS_FILE + INDEX + 0x10, 40000)
    elif change == "groups":
        struct.pack_into("<I", data, TAGS_FILE + INDEX, 2000)
    elif change == "table":
        struct.pack_into("<I", data, TAGS_FILE + INDEX + 0x10, 600)  # 600 tags of 8 bytes run past 4 KB of tag data
    elif change == "blank":
        data[TAGS_FILE + INDEX:TAGS_FILE + INDEX + 32] = bytes(32)
    elif change == "no scnr group":
        data[TAGS_FILE + GROUPS + 32:TAGS_FILE + GROUPS + 36] = b"ldom"
    elif change == "no scnr tag":
        struct.pack_into("<h", data, TAGS_FILE + TABLE + 8, 1)
    elif change == "scenario":
        struct.pack_into("<I", data, TAGS_FILE + TABLE + 12, 0)
    return data
