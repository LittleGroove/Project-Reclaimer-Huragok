"""Every version of a host's file that Huragok keeps (core/versions.py)."""

from __future__ import annotations

from pathlib import Path

from huragok.core import versions


def test_each_change_is_kept_once_with_how_it_was_made(tmp_path: Path) -> None:
    records = tmp_path / "records"
    playlist = tmp_path / "SERVERS" / "Alpha" / "playlists" / "slayer.playlist.json"
    playlist.parent.mkdir(parents=True)
    assert versions.seen(records, "playlists", "alpha", playlist) is None  # nothing there yet: nothing kept
    playlist.write_text('{"name": "Slayer"}', encoding="utf-8")
    first = versions.seen(records, "playlists", "alpha", playlist)
    assert first is not None and first.how == versions.FIRST
    assert versions.seen(records, "playlists", "alpha", playlist) is None  # the same file: not kept twice
    playlist.write_text('{"name": "Slayer v2"}', encoding="utf-8")
    saved = versions.keep(records, "playlists", "alpha", playlist, "saved in G", "the version of yesterday")
    assert saved is not None and versions.read(saved) == '{"name": "Slayer v2"}'
    playlist.write_text('{"name": "by hand"}', encoding="utf-8")
    hand = versions.seen(records, "playlists", "alpha", playlist)
    assert hand is not None and hand.how == versions.BY_HAND
    kept = versions.versions(records, "playlists", "alpha", "slayer.playlist.json")
    assert [v.how for v in kept] == [versions.BY_HAND, "saved in G", versions.FIRST]  # newest first
    assert kept[1].source == "the version of yesterday" and len({v.copy.parent for v in kept}) == 3
    assert versions.versions(records, "playlists", "bravo", "slayer.playlist.json") == []  # each server its own


def test_a_draft_left_unsaved_is_kept_but_never_taken_for_the_file(tmp_path: Path) -> None:
    """A draft the server refused, left unsaved, is kept in the history; the file itself didn't change, so the next
    look doesn't take it for a change made by hand."""
    records = tmp_path / "records"
    playlist = tmp_path / "slayer.playlist.json"
    playlist.write_text('{"name": "Slayer"}', encoding="utf-8")
    versions.seen(records, "playlists", "alpha", playlist)
    draft = versions.keep_text(records, "playlists", "alpha", playlist.name, '{"name": "draft"}', "draft, not saved")
    assert draft is not None and draft.draft
    assert versions.seen(records, "playlists", "alpha", playlist) is None  # the file as it was: nothing new
    assert [v.how for v in versions.versions(records, "playlists", "alpha", playlist.name)] == [
        "draft, not saved", versions.FIRST]
