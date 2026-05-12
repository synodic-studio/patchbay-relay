#!/usr/bin/env python3
"""Trigger all common macOS TCC permission prompts at once.

macOS TCC permissions are per-binary-path. When Homebrew (or mise, or uv)
upgrades Python/Node/Claude, the underlying binary path changes and every
TCC permission tied to it silently resets. Re-run this script after any
such upgrade to click through every prompt in one sitting instead of
hitting them one-by-one over the next week.

Invoke with whichever Python you actually want TCC to remember — that
binary's realpath is what TCC keys on:

    /path/to/your/python3 scripts/tcc/approve.py
"""

import os
import subprocess
import sys


def resolve_binary():
    """Show which binary TCC will attribute permissions to."""
    real = os.path.realpath(sys.executable)
    print(f"Python binary (TCC identity): {real}")
    print()


def trigger_filesystem():
    """Trigger folder access TCC prompts."""
    folders = {
        "Documents": os.path.expanduser("~/Documents"),
        "Desktop": os.path.expanduser("~/Desktop"),
        "Downloads": os.path.expanduser("~/Downloads"),
    }
    for name, path in folders.items():
        try:
            os.listdir(path)
            print(f"  {name}: already approved")
        except PermissionError:
            print(f"  {name}: DENIED (approve the dialog that appeared)")


def trigger_apple_events():
    """Trigger Apple Events TCC prompts for common target apps."""
    targets = [
        ("System Events", 'tell application "System Events" to return name of first process'),
        ("Finder", 'tell application "Finder" to return name of home'),
        ("Calendar", 'tell application "Calendar" to return name of first calendar'),
        ("Reminders", 'tell application "Reminders" to return name of first list'),
        ("Mail", 'tell application "Mail" to return count of mailboxes'),
        ("Xcode", 'tell application "Xcode" to return its name'),
        ("Terminal", 'tell application "Terminal" to return its name'),
    ]
    for name, script in targets:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print(f"  {name}: approved")
            else:
                err = result.stderr.strip()
                if "not allowed" in err.lower() or "denied" in err.lower():
                    print(f"  {name}: DENIED (approve the dialog)")
                elif "not running" in err.lower() or "connection is invalid" in err.lower():
                    print(f"  {name}: app not running (skipped, will prompt on first use)")
                else:
                    print(f"  {name}: error ({err[:80]})")
        except subprocess.TimeoutExpired:
            print(f"  {name}: timed out (dialog may still be showing)")


def check_other_binaries():
    """Show Node and Claude paths that may need separate TCC approval.

    Long-running agents like Claude Code run as child processes. macOS
    treats them as a distinct TCC identity from this Python — both may
    need to be approved separately the first time they touch a protected
    resource.
    """
    print("\nOther binaries that may need separate TCC approval:")
    for name, cmd in [("Node", "node"), ("Claude Code", "claude")]:
        path = subprocess.run(
            ["which", cmd], capture_output=True, text=True
        ).stdout.strip()
        if path:
            real = os.path.realpath(path)
            print(f"  {name}: {real}")
        else:
            print(f"  {name}: not found in PATH")


def main():
    print("=== TCC Permission Pre-Approval ===")
    print("Click 'Allow' on each macOS dialog that appears.\n")
    resolve_binary()

    print("Filesystem access:")
    trigger_filesystem()
    print()

    print("Apple Events (Automation):")
    trigger_apple_events()

    check_other_binaries()

    print("\nDone. These permissions persist until the binary path changes")
    print("(e.g. a brew/mise/uv upgrade) or you reset TCC in System Settings.")


if __name__ == "__main__":
    main()
