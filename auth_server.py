"""Sign in with Apple web server for Telegram bridge authentication.

Serves the Apple sign-in page and handles the OIDC callback.
Runs alongside the Telegram bridge on a configurable port.

Apple Developer setup required:
1. Create a Services ID at developer.apple.com/account/resources/identifiers
2. Enable "Sign in with Apple" for the Services ID
3. Register domain and redirect URL
4. Create a private key for Sign in with Apple

Environment variables:
    APPLE_SERVICE_ID     - Your Services ID (e.g. dev.kj6.auth)
    APPLE_TEAM_ID        - Your Apple Developer Team ID
    APPLE_KEY_ID         - Key ID for Sign in with Apple private key
    APPLE_PRIVATE_KEY_PATH - Path to .p8 private key file
    AUTH_BASE_URL         - Public URL (e.g. https://auth.kj6.dev)
    AUTH_PORT             - Port to listen on (default: 8443)
    APPLE_SUBJECT_ALLOWLIST - Comma-separated Apple user IDs allowed to auth
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx
import jwt as pyjwt
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

import auth

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("bridge.auth_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

APPLE_SERVICE_ID = os.environ.get("APPLE_SERVICE_ID", "")
APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", "")
APPLE_KEY_ID = os.environ.get("APPLE_KEY_ID", "")
APPLE_PRIVATE_KEY_PATH = os.environ.get("APPLE_PRIVATE_KEY_PATH", "")
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "https://auth.kj6.dev")
AUTH_PORT = int(os.environ.get("AUTH_PORT", "8443"))
APPLE_SUBJECT_ALLOWLIST: set[str] = set()
_pp_subj = subprocess.run(["pass-cli", "item", "view", "--vault-name", "Developer Secrets", "--item-title", "apple-subject-allowlist", "--field", "note"], capture_output=True, text=True)
if _pp_subj.returncode == 0 and _pp_subj.stdout.strip():
    _raw_subjects = _pp_subj.stdout.strip()
else:
    _kc_subj = subprocess.run(["security", "find-generic-password", "-a", "bryancostanza", "-s", "apple-subject-allowlist", "-w"], capture_output=True, text=True)
    _raw_subjects = _kc_subj.stdout.strip() if _kc_subj.returncode == 0 and _kc_subj.stdout.strip() else os.environ.get("APPLE_SUBJECT_ALLOWLIST", "")
if _raw_subjects.strip():
    APPLE_SUBJECT_ALLOWLIST = {s.strip() for s in _raw_subjects.split(",") if s.strip()}

# Apple's public keys URL for JWT verification
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"

# Cache Apple's public keys
_apple_keys_cache: dict = {}
_apple_keys_fetched: float = 0

app = FastAPI(title="Claude Bridge Auth")


def _get_apple_client_secret() -> str:
    """Generate a client secret JWT for Apple's token endpoint.

    Apple requires a JWT signed with your private key instead of a
    traditional client secret. Valid for 6 months max.
    """
    key_path = Path(APPLE_PRIVATE_KEY_PATH)
    if not key_path.exists():
        raise FileNotFoundError(f"Apple private key not found: {key_path}")

    private_key = key_path.read_text()
    now = int(time.time())

    payload = {
        "iss": APPLE_TEAM_ID,
        "iat": now,
        "exp": now + 86400 * 180,  # 6 months
        "aud": "https://appleid.apple.com",
        "sub": APPLE_SERVICE_ID,
    }

    return pyjwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": APPLE_KEY_ID},
    )


async def _fetch_apple_keys() -> dict:
    """Fetch Apple's public keys for JWT verification. Cached for 1 hour."""
    global _apple_keys_cache, _apple_keys_fetched
    if _apple_keys_cache and time.time() - _apple_keys_fetched < 3600:
        return _apple_keys_cache

    async with httpx.AsyncClient() as client:
        resp = await client.get(APPLE_KEYS_URL)
        resp.raise_for_status()
        _apple_keys_cache = resp.json()
        _apple_keys_fetched = time.time()
        return _apple_keys_cache


async def _verify_apple_id_token(id_token: str) -> dict:
    """Verify and decode an Apple ID token.

    Validates the JWT signature against Apple's public keys,
    checks audience matches our Service ID, and returns claims.
    """
    keys_data = await _fetch_apple_keys()

    # Get the key ID from the token header
    unverified_header = pyjwt.get_unverified_header(id_token)
    kid = unverified_header.get("kid")

    # Find the matching key
    matching_key = None
    for key in keys_data.get("keys", []):
        if key.get("kid") == kid:
            matching_key = key
            break

    if not matching_key:
        raise ValueError(f"No matching Apple public key for kid={kid}")

    public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(matching_key))

    claims = pyjwt.decode(
        id_token,
        public_key,
        algorithms=["RS256"],
        audience=APPLE_SERVICE_ID,
        issuer="https://appleid.apple.com",
    )
    return claims


@app.get("/login", response_class=HTMLResponse)
async def login_page(token: str = ""):
    """Serve the Sign in with Apple page.

    The token parameter links this auth flow back to the Telegram user
    who requested authentication.
    """
    if not token:
        return HTMLResponse("<h1>Missing auth token</h1><p>Use /auth in Telegram to get a login link.</p>", status_code=400)

    # Verify token exists without consuming it -- consumption happens in /callback
    if auth.check_auth_token(token) is None:
        return HTMLResponse(
            "<h1>Link expired</h1><p>Auth token expired or invalid. Run /auth in Telegram again.</p>",
            status_code=400,
        )

    redirect_uri = f"{AUTH_BASE_URL}/callback"
    state = token  # Pass our auth token as OAuth state

    page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Claude Bridge Auth</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #1a1a1a;
            color: #fff;
        }}
        .container {{
            text-align: center;
            padding: 2rem;
        }}
        h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
        p {{ color: #aaa; margin-bottom: 2rem; }}
        .apple-btn {{
            display: inline-block;
            background: #fff;
            color: #000;
            padding: 12px 24px;
            border-radius: 8px;
            text-decoration: none;
            font-size: 1.1rem;
            font-weight: 500;
        }}
        .apple-btn:hover {{ background: #e0e0e0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Claude Telegram Bridge</h1>
        <p>Sign in to authenticate your session</p>
        <a class="apple-btn" href="{APPLE_AUTH_URL}?client_id={APPLE_SERVICE_ID}&redirect_uri={redirect_uri}&response_type=code%20id_token&scope=name%20email&response_mode=form_post&state={state}">
            Sign in with Apple
        </a>
    </div>
</body>
</html>"""
    return HTMLResponse(page)


@app.post("/callback")
async def apple_callback(
    request: Request,
    code: str = Form(default=""),
    id_token: str = Form(default=""),
    state: str = Form(default=""),
    error: str = Form(default=""),
):
    """Handle Apple's OIDC callback (form_post response mode).

    Apple POSTs the authorization code, id_token, and our state token.
    We verify the id_token, check the Apple subject, and create an auth session.
    """
    if error:
        logger.warning("Apple auth error: %s", error)
        return HTMLResponse(f"<h1>Authentication failed</h1><p>{error}</p>", status_code=400)

    if not id_token or not state:
        return HTMLResponse("<h1>Invalid callback</h1><p>Missing required parameters.</p>", status_code=400)

    # Consume the auth token to get Telegram user ID
    telegram_user_id = auth.consume_auth_token(state)
    if telegram_user_id is None:
        return HTMLResponse(
            "<h1>Link expired</h1><p>Auth token expired or already used. Run /auth in Telegram again.</p>",
            status_code=400,
        )

    # Check rate limit before proceeding
    if auth.is_rate_limited(telegram_user_id):
        logger.warning("Rate-limited user %d attempted auth", telegram_user_id)
        return HTMLResponse(
            "<h1>Too many attempts</h1><p>Account temporarily locked. Try again later.</p>",
            status_code=429,
        )

    # Verify the Apple ID token
    try:
        claims = await _verify_apple_id_token(id_token)
    except Exception as e:
        logger.error("Apple token verification failed: %s", e)
        auth.record_failed_attempt(telegram_user_id)
        return HTMLResponse(f"<h1>Verification failed</h1><p>{e}</p>", status_code=400)

    apple_subject = claims.get("sub", "")

    # Check allowlist if configured
    if APPLE_SUBJECT_ALLOWLIST and apple_subject not in APPLE_SUBJECT_ALLOWLIST:
        logger.warning("Apple subject %s not in allowlist", apple_subject)
        auth.record_failed_attempt(telegram_user_id)
        auth._log_event("denied", telegram_user_id, f"apple_sub={apple_subject} not in allowlist")
        auth._notify("denied", telegram_user_id, f"Apple subject {apple_subject} not in allowlist")
        return HTMLResponse("<h1>Access denied</h1><p>Your Apple ID is not authorized.</p>", status_code=403)

    # Get client IP
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    # Create authenticated session
    auth.create_session(telegram_user_id, apple_subject, client_ip)

    logger.info("Authenticated Telegram user %d (Apple sub: %s)", telegram_user_id, apple_subject[:12])

    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Authenticated</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #1a1a1a;
            color: #fff;
        }
        .container { text-align: center; padding: 2rem; }
        h1 { color: #4caf50; }
        p { color: #aaa; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Authenticated</h1>
        <p>You can close this page and return to Telegram.</p>
    </div>
</body>
</html>""")


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting auth server on port %d", AUTH_PORT)
    uvicorn.run(app, host="0.0.0.0", port=AUTH_PORT)
