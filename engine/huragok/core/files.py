"""Files and folders, as every part of Huragok handles them."""

from __future__ import annotations

import itertools
import os
import stat
from pathlib import Path

_temp_names = itertools.count()


def mtime(path: Path) -> float:
    """When the file last changed, or 0.0 when it can't be looked at (gone, or not there yet)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def write_atomic(path: Path, text: str, *, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Replace a file in one step: written whole to a hidden file beside it (`.<name>.<pid>-<n>.tmp`, which a
    program watching the folder never takes for one of its files), then swapped in with os.replace. A reader never
    sees it half written, a crash part way leaves the old one, and a chmod 600 file stays private. `newline` as
    open() takes it: "" writes the text's own line ends as they are."""
    temp = path.with_name(f".{path.name}.{os.getpid()}-{next(_temp_names)}.tmp")
    try:
        with open(temp, "w", encoding=encoding, newline=newline) as file:
            file.write(text)
        if os.name != "nt" and path.exists():
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def folder_size(path: Path) -> int:
    """Bytes in a folder and everything under it (0 when it isn't there)."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += folder_size(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def clean_path(text: str) -> str:
    """A path as typed or pasted: spaces and the quotes Explorer's "Copy as path" adds taken off."""
    text = str(text).strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def read_text(path: Path, raising: type[Exception] = ValueError) -> str:
    """A text file Huragok reads (a word list, the banned names, the interval messages) as Windows editors save it:
    UTF-8 with or without a BOM, or Notepad's "Unicode" (UTF-16 with a BOM). Anything else (ANSI) raises `raising`
    (the caller's own error: automod's, the interval messages'), saying to save it as UTF-8. OSError passes through."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            raise raising(f"{path.name} starts like UTF-16 but isn't: save it as UTF-8") from None
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise raising(f"{path.name} isn't saved as UTF-8 (byte {error.start + 1} is {data[error.start]:#04x}; "
                            "ANSI, perhaps): save it as UTF-8 in your editor") from None
        if "\x00" in text:
            raise raising(f"{path.name} looks like UTF-16 without a BOM: save it as UTF-8 in your editor")
    return text.replace("\r\n", "\n").replace("\r", "\n")  # as read_text() reads them: Windows line ends too
