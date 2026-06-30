"""Tests for patchbay.reply_store — sent-message text caching."""

from __future__ import annotations


from patchbay import reply_store


def test_record_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    reply_store.record(100, 42, "hello world")
    assert reply_store.lookup(100, 42) == "hello world"


def test_lookup_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    assert reply_store.lookup(999, 999) is None


def test_different_chats_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    reply_store.record(1, 10, "chat one")
    reply_store.record(2, 10, "chat two")
    assert reply_store.lookup(1, 10) == "chat one"
    assert reply_store.lookup(2, 10) == "chat two"


def test_text_truncated_to_max_len(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    long_text = "x" * 5000
    reply_store.record(1, 1, long_text)
    result = reply_store.lookup(1, 1)
    assert result is not None
    assert len(result) == reply_store.MAX_TEXT_LEN


def test_empty_text_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    reply_store.record(1, 1, "")
    reply_store.record(1, 2, "   ")
    assert reply_store.lookup(1, 1) is None
    assert reply_store.lookup(1, 2) is None


def test_cap_trims_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    monkeypatch.setattr(reply_store, "MAX_ENTRIES", 3)
    reply_store.record(1, 1, "first")
    reply_store.record(1, 2, "second")
    reply_store.record(1, 3, "third")
    reply_store.record(1, 4, "fourth")  # should evict entry 1
    assert reply_store.lookup(1, 1) is None
    assert reply_store.lookup(1, 4) == "fourth"


def test_corrupt_file_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "reply_store.json"
    path.write_text("not json")
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", path)
    assert reply_store.lookup(1, 1) is None


def test_overwrite_existing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(reply_store, "REPLY_STORE_FILE", tmp_path / "reply_store.json")
    reply_store.record(1, 1, "original")
    reply_store.record(1, 1, "updated")
    assert reply_store.lookup(1, 1) == "updated"
