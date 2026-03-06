#!/usr/bin/env python3
"""Bootstrap TOTP authentication for the Telegram bridge.

Run once to generate a TOTP secret, display a QR code for your authenticator
app, and write the secret into auth/totp_secrets.json so the bridge can verify
codes via /totp <code>.

Usage:
    uv run setup_totp.py [--user-id TELEGRAM_USER_ID] [--force]
    uv run setup_totp.py --user-id 123456789
"""

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path


def load_env_user_ids() -> list[int]:
    """Read ALLOWED_USER_IDS from .env (simple key=value parse, no dotenv dep)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return []
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("ALLOWED_USER_IDS="):
            value = line.split("=", 1)[1].strip()
            ids = []
            for part in value.split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
            return ids
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up TOTP for the Telegram bridge.")
    parser.add_argument(
        "--user-id",
        type=int,
        help="Telegram user ID to configure TOTP for (defaults to first ALLOWED_USER_IDS in .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing TOTP secret without prompting",
    )
    args = parser.parse_args()

    try:
        import pyotp
    except ImportError:
        print("ERROR: pyotp is not installed. Run: uv add pyotp")
        return 1

    try:
        import qrcode
    except ImportError:
        print("ERROR: qrcode is not installed. Run: uv add qrcode")
        return 1

    # Resolve user ID
    user_id = args.user_id
    if user_id is None:
        env_ids = load_env_user_ids()
        if not env_ids:
            print("ERROR: No user ID supplied and ALLOWED_USER_IDS not set in .env.")
            print("       Run with: uv run setup_totp.py --user-id <your-telegram-user-id>")
            return 1
        user_id = env_ids[0]
        if len(env_ids) > 1:
            print(f"Note: multiple user IDs in .env; using first: {user_id}")

    # Paths
    auth_dir = Path(__file__).parent / "auth"
    auth_dir.mkdir(exist_ok=True)
    secrets_file = auth_dir / "totp_secrets.json"
    home_file = Path.home() / ".claude-bridge-totp"

    # Load existing secrets
    secrets: dict = {}
    if secrets_file.exists():
        try:
            secrets = json.loads(secrets_file.read_text())
        except (json.JSONDecodeError, TypeError):
            secrets = {}

    # Check for existing TOTP
    key = str(user_id)
    if key in secrets and not args.force:
        print(f"TOTP is already configured for user {user_id}.")
        print("Use --force to generate a new secret (this will invalidate the current one).")
        return 1

    # Generate secret
    secret = pyotp.random_base32()
    secrets[key] = {"secret": secret, "created_at": time.time()}

    # Write auth/totp_secrets.json (used by the bridge)
    secrets_file.write_text(json.dumps(secrets, indent=2) + "\n")

    # Write ~/.claude-bridge-totp (chmod 600)
    home_file.write_text(f"{secret}\n")
    home_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    # Build provisioning URI
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=str(user_id), issuer_name="ClaudeBridge"
    )

    # Display QR code
    print()
    print("=" * 52)
    print("  Claude Bridge — TOTP Setup")
    print("=" * 52)
    print()
    print("Scan this QR code with your authenticator app:")
    print("(Google Authenticator, Authy, 1Password, etc.)")
    print()

    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print()
    print(f"  User ID : {user_id}")
    print(f"  Secret  : {secret}")
    print()
    print("If you can't scan the QR code, enter the secret manually.")
    print()
    print(f"Secret also saved to: {home_file}  (mode 600)")
    print()

    # Verify a code to confirm setup
    print("Verify setup — enter the 6-digit code from your app: ", end="", flush=True)
    try:
        code = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Skipping verification.")
        return 0

    totp = pyotp.TOTP(secret)
    if totp.verify(code, valid_window=1):
        print()
        print("✓ TOTP verified. Setup complete.")
        print()
        print(f"In the Telegram bot, use: /totp {code.replace(code, '<6-digit-code>')}")
        print("(Use a fresh code — each code is only valid once.)")
    else:
        print()
        print("✗ Code did not match. The secret is saved — try /totp <code> in the bot.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
