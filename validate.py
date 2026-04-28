#!/usr/bin/env python3
"""Pre-flight validation for bridge.py.

Self-contained — no top-level imports from bridge.py so this script
works even when bridge.py is completely broken.

Exit codes:
    0  all checks passed
    1  syntax error in bridge.py
    2  import error (bridge.py fails to load)
    3  parser smoke-test failure
"""

import importlib
import py_compile
import sys
from pathlib import Path

BRIDGE_PATH = Path(__file__).parent / "bridge.py"

# ---------------------------------------------------------------------------
# Fixtures — real examples of Claude CLI output formats
# ---------------------------------------------------------------------------

FIXTURE_JSON_ARRAY = (
    '[{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}},'
    '{"type":"result","result":"hello","session_id":"abc123","is_error":false}]'
)

FIXTURE_NDJSON = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
    '{"type":"result","result":"hello","session_id":"abc123","is_error":false}'
)

FIXTURE_SINGLE_RESULT = '{"type":"result","result":"done","session_id":"xyz","is_error":false}'

FIXTURE_ERROR_RESULT = '{"type":"result","result":"error occurred","session_id":"xyz","is_error":true}'

FIXTURE_EMPTY = ""

FIXTURE_GARBAGE = "not json at all\nstill not json"


def check_syntax() -> bool:
    """Check that bridge.py has valid Python syntax."""
    try:
        py_compile.compile(str(BRIDGE_PATH), doraise=True)
        return True
    except py_compile.PyCompileError as exc:
        print(f"SYNTAX ERROR: {exc}")
        return False


def check_import():
    """Import bridge module and return it, or None on failure."""
    import logging

    # Ensure the bridge directory is on sys.path
    bridge_dir = str(BRIDGE_PATH.parent)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)

    # Suppress bridge's logging during validation
    logging.disable(logging.CRITICAL)
    try:
        # Remove cached module if present (allows re-validation)
        sys.modules.pop("bridge", None)
        mod = importlib.import_module("bridge")
        return mod
    except Exception as exc:
        print(f"IMPORT ERROR: {type(exc).__name__}: {exc}")
        return None
    finally:
        logging.disable(logging.NOTSET)


def check_parser(mod) -> bool:
    """Run smoke tests against _parse_events and parse_claude_response."""
    import logging

    ok = True

    parse_events = getattr(mod, "_parse_events", None)
    parse_response = getattr(mod, "parse_claude_response", None)

    if parse_events is None:
        print("SMOKE TEST FAIL: _parse_events not found in bridge module")
        return False
    if parse_response is None:
        print("SMOKE TEST FAIL: parse_claude_response not found in bridge module")
        return False

    # Prevent side effects: stub out session saving
    original_save = getattr(mod, "save_session_id", None)
    mod.save_session_id = lambda *a, **kw: None

    logging.disable(logging.CRITICAL)
    try:
        # --- _parse_events tests ---
        cases = [
            ("JSON array", FIXTURE_JSON_ARRAY, 2),
            ("NDJSON", FIXTURE_NDJSON, 2),
            ("single object", FIXTURE_SINGLE_RESULT, 1),
            ("empty", FIXTURE_EMPTY, 0),
            ("garbage", FIXTURE_GARBAGE, 0),
        ]
        for label, fixture, expected_len in cases:
            result = parse_events(fixture)
            if not isinstance(result, list):
                print(f"SMOKE TEST FAIL: _parse_events({label}) returned {type(result).__name__}, expected list")
                ok = False
                continue
            if len(result) != expected_len:
                print(f"SMOKE TEST FAIL: _parse_events({label}) returned {len(result)} items, expected {expected_len}")
                ok = False
                continue
            # Every element must be a dict
            for i, item in enumerate(result):
                if not isinstance(item, dict):
                    print(f"SMOKE TEST FAIL: _parse_events({label})[{i}] is {type(item).__name__}, expected dict")
                    ok = False

        # --- parse_claude_response tests ---
        dummy_key = "__validate__"

        r = parse_response(FIXTURE_JSON_ARRAY, dummy_key)
        if r != "hello":
            print(f"SMOKE TEST FAIL: parse_claude_response(JSON array) returned {r!r}, expected 'hello'")
            ok = False

        r = parse_response(FIXTURE_NDJSON, dummy_key)
        if r != "hello":
            print(f"SMOKE TEST FAIL: parse_claude_response(NDJSON) returned {r!r}, expected 'hello'")
            ok = False

        r = parse_response(FIXTURE_ERROR_RESULT, dummy_key)
        if r != "error occurred":
            print(f"SMOKE TEST FAIL: parse_claude_response(error result) returned {r!r}, expected 'error occurred'")
            ok = False

        r = parse_response(FIXTURE_GARBAGE, dummy_key)
        if not isinstance(r, str):
            print(f"SMOKE TEST FAIL: parse_claude_response(garbage) returned {type(r).__name__}, expected str")
            ok = False

        r = parse_response(FIXTURE_EMPTY, dummy_key)
        if not isinstance(r, str):
            print(f"SMOKE TEST FAIL: parse_claude_response(empty) returned {type(r).__name__}, expected str")
            ok = False

    finally:
        logging.disable(logging.NOTSET)
        # Restore original save function
        if original_save is not None:
            mod.save_session_id = original_save

    return ok


def check_helpers(mod) -> bool:
    """Test helper functions that are in the critical message-handling path."""
    import logging

    ok = True

    # --- _parse_project_entry: handles both str and dict entries ---
    parse_project_entry = getattr(mod, "_parse_project_entry", None)
    if parse_project_entry is None:
        print("SMOKE TEST FAIL: _parse_project_entry not found")
        return False

    cases = [
        ("string entry", "Fanta", ("Fanta", None)),
        ("dict entry", {"path": "Fanta", "agent": "gadget"}, ("Fanta", "gadget")),
        ("dict no path", {"agent": "gadget"}, (None, "gadget")),
        ("None", None, (None, None)),
        ("int", 42, (None, None)),
    ]
    for label, inp, expected in cases:
        result = parse_project_entry(inp)
        if result != expected:
            print(f"SMOKE TEST FAIL: _parse_project_entry({label}) = {result!r}, expected {expected!r}")
            ok = False

    # --- get_chat_working_dir: must return a string, never crash ---
    get_cwd = getattr(mod, "get_chat_working_dir", None)
    if get_cwd is None:
        print("SMOKE TEST FAIL: get_chat_working_dir not found")
        return False

    logging.disable(logging.CRITICAL)
    try:
        result = get_cwd("__nonexistent_key__")
        if not isinstance(result, str):
            print(f"SMOKE TEST FAIL: get_chat_working_dir returned {type(result).__name__}, expected str")
            ok = False
    except Exception as exc:
        print(f"SMOKE TEST FAIL: get_chat_working_dir raised {type(exc).__name__}: {exc}")
        ok = False

    # --- session helpers: round-trip save/load ---
    save_sid = getattr(mod, "save_session_id", None)
    get_sid = getattr(mod, "get_session_id", None)
    clear_sid = getattr(mod, "clear_session", None)
    session_dir = getattr(mod, "SESSION_DIR", None)

    if all(x is not None for x in [save_sid, get_sid, clear_sid, session_dir]):
        test_key = "__validate_session_test__"
        try:
            save_sid(test_key, "test-session-id-123")
            got = get_sid(test_key)
            if got != "test-session-id-123":
                print(f"SMOKE TEST FAIL: session round-trip: got {got!r}")
                ok = False
            clear_sid(test_key)
            got_after = get_sid(test_key)
            if got_after is not None:
                print(f"SMOKE TEST FAIL: clear_session didn't clear: got {got_after!r}")
                ok = False
        except Exception as exc:
            print(f"SMOKE TEST FAIL: session helpers raised {type(exc).__name__}: {exc}")
            ok = False
        finally:
            # Clean up test file
            test_file = session_dir / f"{test_key}.json"
            test_file.unlink(missing_ok=True)

    logging.disable(logging.NOTSET)
    return ok


def check_package_modules() -> bool:
    """Verify all patchbay package modules can be imported."""
    import logging

    logging.disable(logging.CRITICAL)
    ok = True
    modules = [
        "patchbay",
        "patchbay.config",
        "patchbay.sessions",
        "patchbay.parser",
        "patchbay.quota",
        "patchbay.activity",
        "patchbay.projects",
    ]
    for name in modules:
        try:
            sys.modules.pop(name, None)
            importlib.import_module(name)
        except Exception as exc:
            print(f"PACKAGE IMPORT FAIL: {name}: {type(exc).__name__}: {exc}")
            ok = False
    logging.disable(logging.NOTSET)

    if ok:
        # Cross-module integration: verify parser can call save_session_id
        try:
            from patchbay.parser import _parse_events

            events = _parse_events('{"type":"result","result":"ok","session_id":"v","is_error":false}')
            if len(events) != 1:
                print("PACKAGE TEST FAIL: _parse_events returned unexpected result")
                ok = False
        except Exception as exc:
            print(f"PACKAGE TEST FAIL: cross-module test: {type(exc).__name__}: {exc}")
            ok = False

    return ok


def main() -> int:
    print("Validating bridge.py...")

    # 1. Syntax
    if not check_syntax():
        return 1

    # 2. Package modules
    if not check_package_modules():
        return 2

    # 3. Import bridge (depends on package modules)
    mod = check_import()
    if mod is None:
        return 2

    # 4. Parser smoke tests
    if not check_parser(mod):
        return 3

    # 5. Critical-path helper tests
    if not check_helpers(mod):
        return 3

    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
