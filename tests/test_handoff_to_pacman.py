"""Tests for _handoff_to_pacman queue file writing."""

import datetime
from pathlib import Path
from unittest.mock import patch

import bridge


def test_handoff_writes_correct_content_and_returns_true(tmp_path: Path) -> None:
    """Verify _handoff_to_pacman writes expected queue file content and returns True."""
    queue_dir = tmp_path / "agents" / "pac-man" / "queue"

    fake_now = datetime.datetime(2026, 3, 10, 14, 30, 0, tzinfo=datetime.timezone.utc)
    fake_today = datetime.date(2026, 3, 10)

    with (
        patch.object(bridge, "PACMAN_QUEUE_DIR", queue_dir),
        patch("bridge.datetime") as mock_dt,
    ):
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.date.today.return_value = fake_today
        mock_dt.timezone = datetime.timezone

        result = bridge._handoff_to_pacman(
            session_key="123_456",
            message="Please fix the login bug",
            chat_id=123,
            thread_id=456,
            session_id="sess-abc-123",
            working_dir="/Users/test/Developer/myproject",
        )

    assert result is True

    # Exactly one queue file should have been created
    queue_files = list(queue_dir.iterdir())
    assert len(queue_files) == 1

    queue_file = queue_files[0]
    assert queue_file.name == "bridge-recovery-123_456.md"

    content = queue_file.read_text()

    # Structural checks
    assert content.startswith("# Bridge Quota Recovery\n")
    assert content.endswith("\n")

    # Date/time fields
    assert "Updated: 2026-03-10" in content
    assert "**Quota hit at:** 2026-03-10 14:30 UTC" in content

    # Session metadata
    assert "**Session key:** 123_456" in content
    assert "**Session ID:** sess-abc-123" in content
    assert "**Working directory:** /Users/test/Developer/myproject" in content
    assert "**Chat ID:** 123" in content
    assert "**Thread ID:** 456" in content

    # Original message preserved in code block
    assert "```\nPlease fix the login bug\n```" in content

    # Priority and task line
    assert "Priority: critical" in content
    assert "Resume interrupted Telegram session 123_456" in content

    # Response routing section
    assert "**Response routing:**" in content
    assert f"chat_id=123" in content
    assert f"message_thread_id=456" in content


def test_handoff_with_no_session_id(tmp_path: Path) -> None:
    """Verify session_id=None renders as 'none (fresh session)'."""
    queue_dir = tmp_path / "agents" / "pac-man" / "queue"

    with (
        patch.object(bridge, "PACMAN_QUEUE_DIR", queue_dir),
        patch("bridge.datetime") as mock_dt,
    ):
        mock_dt.datetime.now.return_value = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        mock_dt.date.today.return_value = datetime.date(2026, 1, 1)
        mock_dt.timezone = datetime.timezone

        result = bridge._handoff_to_pacman(
            session_key="789",
            message="hello",
            chat_id=789,
            thread_id=None,
            session_id=None,
            working_dir="/tmp",
        )

    assert result is True

    content = next(queue_dir.iterdir()).read_text()
    assert "**Session ID:** none (fresh session)" in content
    assert "**Thread ID:** None" in content


def test_handoff_session_key_sanitized_in_filename(tmp_path: Path) -> None:
    """Verify hyphens are stripped and key is truncated to 20 chars in filename."""
    queue_dir = tmp_path / "agents" / "pac-man" / "queue"

    with (
        patch.object(bridge, "PACMAN_QUEUE_DIR", queue_dir),
        patch("bridge.datetime") as mock_dt,
    ):
        mock_dt.datetime.now.return_value = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        mock_dt.date.today.return_value = datetime.date(2026, 1, 1)
        mock_dt.timezone = datetime.timezone

        result = bridge._handoff_to_pacman(
            session_key="aaa-bbb-ccc-ddd-eee-fff-ggg",
            message="test",
            chat_id=1,
            thread_id=None,
            session_id=None,
            working_dir="/tmp",
        )

    assert result is True

    queue_file = next(queue_dir.iterdir())
    # "aaa-bbb-ccc-ddd-eee-fff-ggg" -> strip hyphens -> "aaabbbcccdddeeefffggg" -> [:20] -> "aaabbbcccdddeeeffggg"... wait
    # Actually: "aaa-bbb-ccc-ddd-eee-fff-ggg".replace('-','') = "aaabbbcccdddeeefffggg" (21 chars) -> [:20] = "aaabbbcccdddeeeffgg"... let me just check
    sanitized = "aaa-bbb-ccc-ddd-eee-fff-ggg".replace("-", "")[:20]
    assert queue_file.name == f"bridge-recovery-{sanitized}.md"
