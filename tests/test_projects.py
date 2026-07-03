"""Tests for patchbay.projects — chat-to-project directory mapping."""


import pytest

import patchbay.projects


@pytest.fixture(autouse=True)
def _isolate_projects(tmp_path, monkeypatch):
    """Redirect CHAT_PROJECTS_FILE and WORKING_DIR to temp paths."""
    projects_file = tmp_path / "chat_projects.json"
    working_dir = str(tmp_path / "Developer")
    (tmp_path / "Developer").mkdir()

    monkeypatch.setattr(patchbay.projects, "CHAT_PROJECTS_FILE", projects_file)
    monkeypatch.setattr(patchbay.projects, "WORKING_DIR", working_dir)


# ---------------------------------------------------------------------------
# _parse_project_entry
# ---------------------------------------------------------------------------


class TestParseProjectEntry:
    def test_string_entry(self):
        assert patchbay.projects._parse_project_entry("Fanta") == ("Fanta", None)

    def test_dict_with_path_and_agent(self):
        entry = {"path": "Fanta", "agent": "iron-temple"}
        assert patchbay.projects._parse_project_entry(entry) == ("Fanta", "iron-temple")

    def test_dict_with_only_path(self):
        entry = {"path": "Fanta"}
        assert patchbay.projects._parse_project_entry(entry) == ("Fanta", None)

    def test_dict_with_only_agent(self):
        entry = {"agent": "iron-temple"}
        assert patchbay.projects._parse_project_entry(entry) == (None, "iron-temple")

    def test_none_entry(self):
        assert patchbay.projects._parse_project_entry(None) == (None, None)

    def test_integer_entry(self):
        assert patchbay.projects._parse_project_entry(42) == (None, None)

    def test_empty_string(self):
        assert patchbay.projects._parse_project_entry("") == ("", None)

    def test_empty_dict(self):
        assert patchbay.projects._parse_project_entry({}) == (None, None)

    def test_list_entry(self):
        assert patchbay.projects._parse_project_entry(["a", "b"]) == (None, None)


# ---------------------------------------------------------------------------
# _load_chat_projects / _save_chat_projects
# ---------------------------------------------------------------------------


class TestLoadSaveChatProjects:
    def test_load_missing_file_returns_empty(self):
        result = patchbay.projects._load_chat_projects()
        assert result == {}

    def test_load_corrupt_json_returns_empty(self):
        patchbay.projects.CHAT_PROJECTS_FILE.write_text("not valid json{{{")
        result = patchbay.projects._load_chat_projects()
        assert result == {}

    def test_save_and_load_round_trip(self):
        data = {"chat_1": "Fanta", "chat_2": "patchbay-relay"}
        patchbay.projects._save_chat_projects(data)
        loaded = patchbay.projects._load_chat_projects()
        assert loaded == data

    def test_multiple_entry_types(self):
        data = {
            "chat_str": "Fanta",
            "chat_dict": {"path": "patchbay-relay", "agent": "magpie"},
        }
        patchbay.projects._save_chat_projects(data)
        loaded = patchbay.projects._load_chat_projects()
        assert loaded == data


# ---------------------------------------------------------------------------
# get_chat_working_dir
# ---------------------------------------------------------------------------


class TestGetChatWorkingDir:
    def test_with_project_set(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        result = patchbay.projects.get_chat_working_dir("chat_1")
        working = patchbay.projects.WORKING_DIR
        assert result == f"{working}/Fanta"

    def test_without_project_returns_default(self):
        patchbay.projects._save_chat_projects({})
        result = patchbay.projects.get_chat_working_dir("unknown_chat")
        assert result == patchbay.projects.WORKING_DIR

    def test_with_dict_entry(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "patchbay-relay", "agent": "plotter"}}
        )
        result = patchbay.projects.get_chat_working_dir("chat_1")
        working = patchbay.projects.WORKING_DIR
        assert result == f"{working}/patchbay-relay"


# ---------------------------------------------------------------------------
# get_chat_agent
# ---------------------------------------------------------------------------


class TestGetChatAgent:
    def test_with_agent(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "agent": "iron-temple"}}
        )
        assert patchbay.projects.get_chat_agent("chat_1") == "iron-temple"

    def test_without_agent_returns_none(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta"}}
        )
        assert patchbay.projects.get_chat_agent("chat_1") is None

    def test_string_entry_returns_none(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        assert patchbay.projects.get_chat_agent("chat_1") is None


# ---------------------------------------------------------------------------
# set_chat_project
# ---------------------------------------------------------------------------


class TestSetChatProject:
    def test_set_and_verify(self):
        patchbay.projects.set_chat_project("chat_1", "Fanta")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == "Fanta"

    def test_clear_project(self):
        patchbay.projects.set_chat_project("chat_1", "Fanta")
        patchbay.projects.set_chat_project("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert "chat_1" not in loaded

    def test_overwrite_existing(self):
        patchbay.projects.set_chat_project("chat_1", "Fanta")
        patchbay.projects.set_chat_project("chat_1", "patchbay-relay")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == "patchbay-relay"


# ---------------------------------------------------------------------------
# get_all_projects
# ---------------------------------------------------------------------------


class TestGetAllProjects:
    def test_returns_sorted_directories(self, tmp_path):
        dev = tmp_path / "Developer"
        (dev / "Charlie").mkdir()
        (dev / "Alpha").mkdir()
        (dev / "Bravo").mkdir()
        result = patchbay.projects.get_all_projects()
        assert result == ["Alpha", "Bravo", "Charlie"]

    def test_excludes_dotfiles_and_underscored(self, tmp_path):
        dev = tmp_path / "Developer"
        (dev / ".hidden").mkdir()
        (dev / "_private").mkdir()
        (dev / "Visible").mkdir()
        result = patchbay.projects.get_all_projects()
        assert result == ["Visible"]

    def test_empty_directory(self, tmp_path):
        # Developer dir exists but is empty (created by autouse fixture)
        result = patchbay.projects.get_all_projects()
        assert result == []


# ---------------------------------------------------------------------------
# get_chat_harness / set_chat_harness
# ---------------------------------------------------------------------------


class TestChatHarness:
    def test_get_returns_none_when_unset(self):
        assert patchbay.projects.get_chat_harness("chat_1") is None

    def test_get_returns_none_for_legacy_string_entry(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        assert patchbay.projects.get_chat_harness("chat_1") is None

    def test_set_then_get_roundtrip(self):
        patchbay.projects.set_chat_harness("chat_1", "pi")
        assert patchbay.projects.get_chat_harness("chat_1") == "pi"

    def test_set_promotes_string_entry_to_dict_preserving_path(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        patchbay.projects.set_chat_harness("chat_1", "pi")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta", "harness": "pi"}

    def test_set_lands_alongside_path_and_agent(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "agent": "iron-temple"}}
        )
        patchbay.projects.set_chat_harness("chat_1", "pi")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {
            "path": "Fanta",
            "agent": "iron-temple",
            "harness": "pi",
        }

    def test_set_none_clears_only_harness_key(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "harness": "pi"}}
        )
        patchbay.projects.set_chat_harness("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta"}

    def test_clearing_harness_on_harness_only_entry_drops_entry(self):
        patchbay.projects._save_chat_projects({"chat_1": {"harness": "pi"}})
        patchbay.projects.set_chat_harness("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert "chat_1" not in loaded


# ---------------------------------------------------------------------------
# get_chat_title / set_chat_title
# ---------------------------------------------------------------------------


class TestChatTitle:
    def test_get_returns_none_when_unset(self):
        assert patchbay.projects.get_chat_title("chat_1") is None

    def test_get_returns_none_for_legacy_string_entry(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        assert patchbay.projects.get_chat_title("chat_1") is None

    def test_set_then_get_roundtrip(self):
        patchbay.projects.set_chat_title("chat_1", "Patchbay Bridge")
        assert patchbay.projects.get_chat_title("chat_1") == "Patchbay Bridge"

    def test_set_promotes_string_entry_to_dict_preserving_path(self):
        patchbay.projects._save_chat_projects({"chat_1": "Fanta"})
        patchbay.projects.set_chat_title("chat_1", "Iron Temple")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta", "title": "Iron Temple"}

    def test_set_lands_alongside_path_agent_harness(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "agent": "ernest", "harness": "pi"}}
        )
        patchbay.projects.set_chat_title("chat_1", "Ernest")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {
            "path": "Fanta",
            "agent": "ernest",
            "harness": "pi",
            "title": "Ernest",
        }

    def test_set_none_clears_only_title_key(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "title": "Old"}}
        )
        patchbay.projects.set_chat_title("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {"path": "Fanta"}

    def test_clearing_title_on_title_only_entry_drops_entry(self):
        patchbay.projects._save_chat_projects({"chat_1": {"title": "Orphan"}})
        patchbay.projects.set_chat_title("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert "chat_1" not in loaded


# ---------------------------------------------------------------------------
# set_chat_project preserves dict-form sibling keys
# ---------------------------------------------------------------------------


class TestSetChatProjectPreservesSiblings:
    def test_changing_path_keeps_agent_harness_title(self):
        patchbay.projects._save_chat_projects(
            {
                "chat_1": {
                    "path": "Fanta",
                    "agent": "ernest",
                    "harness": "pi",
                    "title": "Ernest",
                }
            }
        )
        patchbay.projects.set_chat_project("chat_1", "patchbay-relay")
        loaded = patchbay.projects._load_chat_projects()
        assert loaded["chat_1"] == {
            "path": "patchbay-relay",
            "agent": "ernest",
            "harness": "pi",
            "title": "Ernest",
        }

    def test_clearing_path_removes_entire_entry(self):
        patchbay.projects._save_chat_projects(
            {"chat_1": {"path": "Fanta", "title": "Iron Temple"}}
        )
        patchbay.projects.set_chat_project("chat_1", None)
        loaded = patchbay.projects._load_chat_projects()
        assert "chat_1" not in loaded
