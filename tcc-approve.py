#!/usr/bin/env python3
"""Trigger all common macOS TCC permission prompts at once.

Run this with the bridge's Python so permissions map to the correct binary:
    ~/Developer/venvs/claude-telegram-bridge/bin/python3 tcc-approve.py

macOS TCC permissions are per-binary-path. When Homebrew upgrades Python,
Node, or Claude Code, the Cellar path changes and all permissions reset.
Re-run this script after any `brew upgrade python`.
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


def trigger_calendar_reminders():
    """Trigger Calendar and Reminders TCC prompts via EventKit-style access."""
    # osascript-based access already covers these via Apple Events
    # but direct framework access is a separate TCC category
    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return name of every process whose background only is false'],
            capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        pass


def check_node_claude():
    """Show Node and Claude paths that also need TCC approval."""
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
    print()
    print("Claude Code runs as a child of this Python process.")
    print("When it accesses protected resources, macOS may prompt for")
    print("BOTH Python (this binary) and Node/Claude separately.")
    print()
    print("To pre-approve Node/Claude, run Claude Code interactively:")
    print("  claude -p 'access ~/Documents, ~/Desktop, ~/Downloads'")


def main():
    print("=== TCC Permission Pre-Approval ===")
    print("Click 'Allow' on each macOS dialog that appears.\n")
    resolve_binary()

    print("Filesystem access:")
    trigger_filesystem()
    print()

    print("Apple Events (Automation):")
    trigger_apple_events()
    print()

    trigger_calendar_reminders()
    check_node_claude()

    print("\nDone. These permissions persist until:")
    print(f"  - `brew upgrade python` changes the Cellar path")
    print(f"  - You reset TCC via System Settings")
    print(f"\nRe-run this script after Python upgrades.")


if __name__ == "__main__":
    main()
