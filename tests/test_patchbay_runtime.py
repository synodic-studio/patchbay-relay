"""Smoke test: patchbay.runtime exposes the symbols command handlers depend on."""

import patchbay.runtime as runtime


def test_runtime_exposes_bridge_started_at():
    assert isinstance(runtime.BRIDGE_STARTED_AT, float)


def test_runtime_exposes_send_response():
    assert callable(runtime.send_response)


def test_runtime_exposes_session_helpers():
    assert callable(runtime.get_session_id)
    assert callable(runtime.set_session_id)


def test_runtime_exposes_sessions_dict():
    """The bridge's process-wide session registry, keyed by session_key."""
    assert isinstance(runtime.sessions, dict)


def test_runtime_exposes_iter_active_sessions():
    """Backend-agnostic snapshot used by /ping, stall detector, /restart."""
    assert callable(runtime.iter_active_sessions)
    result = runtime.iter_active_sessions()
    assert isinstance(result, list)
