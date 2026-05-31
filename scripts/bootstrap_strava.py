"""One-time Strava OAuth helper.

Run this locally on your Mac to get a long-lived refresh token.

Steps
-----
1. Create a Strava API app at https://www.strava.com/settings/api
   - Authorization Callback Domain: localhost
2. Export your client ID and secret:
       export STRAVA_CLIENT_ID=<your client id>
       export STRAVA_CLIENT_SECRET=<your client secret>
3. Run:
       python3 scripts/bootstrap_strava.py
4. Browser opens, you approve. Script prints the refresh token.
5. Paste STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
   into the repo's GitHub Secrets (Settings -> Secrets and variables -> Actions).
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import urllib.parse
import webbrowser

import requests

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"
SCOPE = "activity:read,activity:read_all"

captured: dict[str, str] = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"No code in callback")
            return

        captured["code"] = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Got it.</h1>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *_args, **_kwargs) -> None:
        pass


def main() -> int:
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in the environment.",
            file=sys.stderr,
        )
        return 1

    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={client_id}"
        "&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        "&approval_prompt=auto"
        f"&scope={SCOPE}"
    )

    print(f"Opening browser to authorize...\n  {auth_url}\n")
    webbrowser.open(auth_url)

    with socketserver.TCPServer(("localhost", PORT), CallbackHandler) as httpd:
        print(f"Waiting for callback on http://localhost:{PORT}/callback ...")
        while "code" not in captured:
            httpd.handle_request()

    print("Code captured. Exchanging for tokens...")

    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": captured["code"],
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()

    athlete = tokens.get("athlete", {})
    print()
    print("=" * 64)
    print("SUCCESS. Add the following three values to GitHub Secrets:")
    print("=" * 64)
    print(f"  STRAVA_CLIENT_ID:     {client_id}")
    print("  STRAVA_CLIENT_SECRET: (the secret you exported)")
    print(f"  STRAVA_REFRESH_TOKEN: {tokens['refresh_token']}")
    print("=" * 64)
    print(
        f"Authorized for: {athlete.get('firstname', '')} "
        f"{athlete.get('lastname', '')} (id {athlete.get('id', '?')})"
    )
    print()
    print("Repo Settings -> Secrets and variables -> Actions -> New repository secret")
    return 0


if __name__ == "__main__":
    sys.exit(main())
