"""Asking the web: GitHub's releases (Huragok's own, the server program's), SteamCMD's installer. One way to fetch,
with the same limits and the same errors for all of them."""

from __future__ import annotations

import http.client
import re
import urllib.error
import urllib.request

from .. import __version__


MAX_DOWNLOAD = 50_000_000  # Huragok's own zip
MAX_PROGRAM_DOWNLOAD = 300_000_000  # the server program (about 25 MB in 2026)
# How a pre-release's word ranks among the others (PEP 440's and SemVer's names); FINAL is above them all.
PRE_RELEASES = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "pre": 3, "preview": 3, "rc": 3}
FINAL = 4  # a full release comes after every pre-release of its numbers


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.10.2' -> (0, 10, 2). Anything after the numbers (-beta…) is ignored: version_key() orders those."""
    numbers = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not numbers:
        raise UpdateError(f"'{text}' isn't a version number")
    return tuple(int(n) for n in numbers.group(1).split("."))


def version_key(text: str) -> tuple[tuple[int, ...], int, int]:
    """For ordering Huragok's releases, betas among them: 0.5.0-beta.1 < 0.5.0-beta.2 < 0.5.0-rc.1 < 0.5.0 <
    0.5.1-beta.1. A pre-release comes before the release it leads up to, however it's written (-beta.2, b2, .beta2,
    -BETA-2); one whose word isn't known counts as the earliest kind, and +build is never part of the order."""
    numbers = parse_version(text)
    rest = re.sub(r"^\s*v?\d+(?:\.\d+)*", "", text).split("+", 1)[0].strip().lower()
    if not rest:
        return numbers, FINAL, 0
    stage = re.match(r"[-._]?([a-z]+)[-._]?(\d*)", rest)
    if not stage:
        return numbers, 0, 0
    return numbers, PRE_RELEASES.get(stage.group(1), 0), int(stage.group(2) or 0)


def same_version(one: str, other: str) -> bool:
    """One version however each is spelled: a release's tag (v0.5.1-beta.1) and its __version__ (0.5.1b1)."""
    try:
        return version_key(one) == version_key(other)
    except UpdateError:
        return one.strip() == other.strip()


def is_prerelease(text: str) -> bool:
    """'0.5.1-beta.1' and '0.5.1b1' are; '0.5.1' isn't."""
    return version_key(text)[1] != FINAL


def fetch(url: str, timeout: float, accept: str = "application/vnd.github+json", *, limit: int = MAX_DOWNLOAD,
         size: int | None = None) -> bytes:
    """GitHub's answer, whole: one cut short (fewer bytes than it said, or than `size`) is an UpdateError."""
    request = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"huragok/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            promised = str(response.headers.get("Content-Length") or "").strip()
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub answered {error.code} {error.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(f"couldn't reach GitHub: {reason}") from None
    except http.client.HTTPException as error:  # IncompleteRead: the connection broke mid-answer
        raise UpdateError(f"the download from {url} broke off ({type(error).__name__}); nothing was changed") from None
    if len(data) > limit:
        raise UpdateError(f"{url} is larger than {limit // 1_000_000} MB")
    expected = size if size is not None else int(promised) if promised.isdecimal() else None
    if expected is not None and len(data) != expected:
        raise UpdateError(f"the download from {url} stopped after {len(data)} of {expected} bytes; nothing was "
                          "changed")
    return data
