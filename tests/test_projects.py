"""Tests for stargate.projects — chat-to-project directory mapping."""


import pytest

import stargate.projects


@pytest.fixture(autouse=True)
def _isolate_projects(tmp_path, monkeypatch):
    """Redirect CHAT_PROJECTS_FILE and WORKING_DIR to temp paths."""
    projects_file = tmp_path / "chat_projects.json"
    working_dir = str(tmp_path / "Developer")
    (tmp_path / "Developer").mkdir()

    monkeypatch.setattr(stargate.projects, "CHAT_PROJECTS_FILE", projects_file)
    monkeypatch.setattr(stargate.projects, "WORKING_DIR", working_dir)


# ---------------------------------------------------------------------------
# _parse_project_entry
# ---------------------------------------------------------------------------


class TestParseProjectEntry:
    def test_string_entry(self):
        assert stargate.projects._parse_project_entry("Fanta") == ("Fanta", None)

    def test_dict_with_path_and_agent(self):
        entry = {"path": "Fanta", "agent": "iron-temple"}
        assert stargate.projects._parse_project_entry(entry) == ("Fanta", "iron-temple")

    def test_dict_with_only_path(self):
        entry = {"path": "Fanta"}
        assert stargate.projects._parse_project_entry(entry) == ("Fanta", None)

    def test_dict_with_only_agent(self):
        entry = {"agent": "iron-temple"}
        assert stargate.projects._parse_project_entry(entry) == (None, "iron-temple")

    def test_none_entry(self):
        assert stargate.projects._parse_project_entry(None) == (None, None)

    def test_integer_entry(self):
        assert stargate.projects._parse_project_entry(42) == (None, None)

    def test_empty_string(self):
        assert stargate.projects._parse_project_entry("") == ("", None)

    def test_empty_dict(self):
        assert stargate.projects._parse_project_entry({}) == (None, None)

    def test_list_entry(self):
        assert stargate.projects._parse_project_entry(["a", "b"]) == (None, None)


# ---------------------------------------------------------------------------
# _load_chat_projects / _save_chat_projects
# ---------------------------------------------------------------------------


class TestLoadSaveChatProjects:
    def test_load_missing_file_returns_empty(self):
        result = stargate.projects._load_chat_projects()
        assert result == {}

    def test_load_corrupt_json_returns_empty(self):
        stargate.projects.CHAT_PROJECTS_FILE.write_text("not valid json{{{")
        result = stargate.projects._load_chat_projects()
        assert result == {}

    def test_save_and_load_round_trip(self):
        data = {"chat_1": "Fanta", "chat_2": "stargate"}
        stargate.projects._save_chat_projects(data)
        loaded = stargate.projects._load_chat_projects()
        assert loaded == data

    def test_multiple_entry_types(self):
        data = {
            "chat_str": "Fanta",
            "chat_dict": {"path": "stargate", "agent": "magpie"},
        }
        stargate.projects._save_chat_projects(data)
        loaded = stargate.projects._load_chat_projects()
        assert loaded == data


# ---------------------------------------------------------------------------
# get_chat_working_dir
# ---------------------------------------------------------------------------


class TestGetChatWorkingDir:
    def test_with_project_set(self):
        stargate.projects._save_chat_projects({"chat_1": "Fanta"})
        result = stargate.projects.get_chat_working_dir("chat_1")
        working = stargate.projects.WORKING_DIR
        assert result == f"{working}/Fanta"

    def test_without_project_returns_default(self):
        stargate.projects._save_chat_projects({})
        result = stargate.projects.get_chat_working_dir("unknown_chat")
        assert result == stargate.projects.WORKING_DIR

    def test_with_dict_entry(self):
        stargate.projects._save_chat_projects(
            {"chat_1": {"path": "stargate", "agent": "plotter"}}
        )
        result = stargate.projects.get_chat_working_dir("chat_1")
        working = stargate.projects.WORKING_DIR
        assert result == f"{working}/stargate"


# ---------------------------------------------------------------------------
# get_chat_agent
# ---------------------------------------------------------------------------


class TestGetChatAgent:
    def test_with_agent(self):
        stargate.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "agent": "iron-temple"}}
        )
        assert stargate.projects.get_chat_agent("chat_1") == "iron-temple"

    def test_without_agent_returns_none(self):
        stargate.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta"}}
        )
        assert stargate.projects.get_chat_agent("chat_1") is None

    def test_string_entry_returns_none(self):
        stargate.projects._save_chat_projects({"chat_1": "Fanta"})
        assert stargate.projects.get_chat_agent("chat_1") is None


# ---------------------------------------------------------------------------
# set_chat_project
# ---------------------------------------------------------------------------


class TestSetChatProject:
    def test_set_and_verify(self):
        stargate.projects.set_chat_project("chat_1", "Fanta")
        loaded = stargate.projects._load_chat_projects()
        assert loaded["chat_1"] == "Fanta"

    def test_clear_project(self):
        stargate.projects.set_chat_project("chat_1", "Fanta")
        stargate.projects.set_chat_project("chat_1", None)
        loaded = stargate.projects._load_chat_projects()
        assert "chat_1" not in loaded

    def test_overwrite_existing(self):
        stargate.projects.set_chat_project("chat_1", "Fanta")
        stargate.projects.set_chat_project("chat_1", "stargate")
        loaded = stargate.projects._load_chat_projects()
        assert loaded["chat_1"] == "stargate"


# ---------------------------------------------------------------------------
# get_all_projects
# ---------------------------------------------------------------------------


class TestGetAllProjects:
    def test_returns_sorted_directories(self, tmp_path):
        dev = tmp_path / "Developer"
        (dev / "Charlie").mkdir()
        (dev / "Alpha").mkdir()
        (dev / "Bravo").mkdir()
        result = stargate.projects.get_all_projects()
        assert result == ["Alpha", "Bravo", "Charlie"]

    def test_excludes_dotfiles_and_underscored(self, tmp_path):
        dev = tmp_path / "Developer"
        (dev / ".hidden").mkdir()
        (dev / "_private").mkdir()
        (dev / "Visible").mkdir()
        result = stargate.projects.get_all_projects()
        assert result == ["Visible"]

    def test_empty_directory(self, tmp_path):
        # Developer dir exists but is empty (created by autouse fixture)
        result = stargate.projects.get_all_projects()
        assert result == []


# ---------------------------------------------------------------------------
# get_chat_harness / set_chat_harness
# ---------------------------------------------------------------------------


class TestChatHarness:
    def test_get_returns_none_when_unset(self):
        assert stargate.projects.get_chat_harness("chat_1") is None

    def test_get_returns_none_for_legacy_string_entry(self):
        stargate.projects._save_chat_projects({"chat_1": "Fanta"})
        assert stargate.projects.get_chat_harness("chat_1") is None

    def test_set_then_get_roundtrip(self):
        stargate.projects.set_chat_harness("chat_1", "cc-sdk")
        assert stargate.projects.get_chat_harness("chat_1") == "cc-sdk"

    def test_set_promotes_string_entry_to_dict_preserving_path(self):
        stargate.projects._save_chat_projects({"chat_1": "Fanta"})
        stargate.projects.set_chat_harness("chat_1", "cc-sdk")
        loaded = stargate.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta", "harness": "cc-sdk"}

    def test_set_lands_alongside_path_and_agent(self):
        stargate.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "agent": "iron-temple"}}
        )
        stargate.projects.set_chat_harness("chat_1", "cc-cli")
        loaded = stargate.projects._load_chat_projects()
        assert loaded["chat_1"] == {
            "path": "Fanta",
            "agent": "iron-temple",
            "harness": "cc-cli",
        }

    def test_set_none_clears_only_harness_key(self):
        stargate.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "harness": "cc-sdk"}}
        )
        stargate.projects.set_chat_harness("chat_1", None)
        loaded = stargate.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta"}

    def test_clearing_harness_on_harness_only_entry_drops_entry(self):
        stargate.projects._save_chat_projects({"chat_1": {"harness": "cc-sdk"}})
        stargate.projects.set_chat_harness("chat_1", None)
        loaded = stargate.projects._load_chat_projects()
        assert "chat_1" not in loaded
