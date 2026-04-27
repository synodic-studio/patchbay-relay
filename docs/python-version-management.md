# Python version management on macOS

This is the recommended Python deployment pattern for Patchbay Relay on macOS. It avoids one specific catastrophic failure mode: silent loss of TCC (Transparency, Consent, and Control) permissions when Homebrew upgrades Python in the background.

## Why Python pinning matters

macOS TCC permissions (Full Disk Access, Calendar, Contacts, etc.) are bound to a specific binary path. When Homebrew upgrades Python from `3.13.0` to `3.13.1`, the binary path under `/opt/homebrew/Cellar/python@3.13/...` changes, and every TCC grant tied to the old path is silently revoked.

For a headless service like Patchbay Relay running unattended on a Mac Mini, this is catastrophic. The bridge stops working because it no longer has filesystem permission. Calendar event creation fails silently. The system looks healthy from the outside but is completely broken.

The fix is not to detect the failure after Homebrew has already changed the binary. It is to never let the upgrade happen unplanned in the first place.

## The pattern

1. **Pin** Python to a specific TCC-approved version on the host. Hold it there. Do not let any package manager auto-upgrade it.
2. **Watch** upstream Python releases on a schedule. For each new release, scan the changelog for things worth upgrading for, like security fixes or features you actually use.
3. **Ignore** releases that do not warrant the cost. Track them so they never re-trigger an alert.
4. **Recommend** an upgrade only when a changelog scan surfaces a real reason. The recommendation includes the specific changelog item that prompted it.
5. **Upgrade deliberately**. When approved, the operator screen-shares (or sshs) into the host, runs the upgrade, re-grants TCC permissions in System Settings, and validates the bridge is back. The system is never silently broken because it never silently changes underneath itself.

## Pinning via Homebrew

Once the bridge is running on a known-good Python, pin it:

```bash
brew pin python@3.13
```

`brew upgrade` will then skip Python while still upgrading everything else. Verify with:

```bash
brew list --pinned
```

To unpin during a planned upgrade:

```bash
brew unpin python@3.13
brew upgrade python@3.13
```

After upgrade, re-grant TCC permissions to the new binary path under System Settings > Privacy & Security, validate the bridge starts cleanly, and pin again.

## Watching releases with `python_check.py`

Patchbay Relay ships a small CLI utility at [`scripts/python_check.py`](../scripts/python_check.py) that automates the watch step.

```bash
PATCHBAY_PYTHON_PIN=3.13.0 python3 scripts/python_check.py
```

The script:

1. Fetches recent CPython releases from the GitHub releases API.
2. Filters out releases at or below the pinned version, plus anything already on the ignore list.
3. Scans each remaining changelog for keywords that warrant an upgrade.
4. Prints either a recommendation with the matching changelog snippet, or a count of releases that were quietly safe to ignore.

### Default keyword list

Out of the box the script looks for: `security`, `CVE`, `vulnerability`, `buffer overflow`, `remote code execution`, `RCE`, `denial of service`, `DoS`. Override with a comma-separated list:

```bash
PATCHBAY_PYTHON_KEYWORDS="security,CVE,asyncio,typing" python3 scripts/python_check.py
```

### Ignore list

Releases that do not match any keyword can be added to a persistent ignore list so they do not re-alert:

```bash
PATCHBAY_PYTHON_PIN=3.13.0 python3 scripts/python_check.py --auto-ignore
```

The ignore list lives at `~/.config/patchbay/python-ignore.json` by default. Override with `PATCHBAY_PYTHON_IGNORE=/path/to/ignore.json`.

### Running on a schedule

Wire the script into whatever scheduler you already use. A daily `cron` job or a `launchd` agent works fine. When the script outputs `Upgrade recommended`, surface the rationale through your normal alerting path (Telegram, email, dashboard). When it outputs anything else, stay quiet.

## Sample output

No new releases worth flagging:

```
Pinned at 3.13.0. 2 newer release(s) found, none match the upgrade keywords.
```

Upgrade recommended:

```
Pinned at 3.13.0. Upgrade recommended.

  v3.13.1 (Python 3.13.1)
  matches 'security': - gh-12345: Fix a buffer overflow in foo() that could allow remote code execution
  matches 'CVE': - CVE-2026-XXXX: see security advisory
  Release notes: https://github.com/python/cpython/releases/tag/v3.13.1
```

## Limitations

- The script only checks CPython upstream. It does not detect Homebrew-side or other-distribution-side changes that might still alter the binary path. Pinning via the package manager is the actual prevention; the script just decides when to plan the next deliberate upgrade.
- Keyword scanning over release notes is heuristic. Some real upgrade-worthy changes will not match the default keyword list, especially feature additions you specifically depend on. Tune `PATCHBAY_PYTHON_KEYWORDS` to your project.
- The script does not perform the upgrade. It only recommends one. The actual upgrade is a deliberate operator action because it has TCC re-granting consequences.
