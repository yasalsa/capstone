#!/usr/bin/env python3
"""
Local OAuth helper for Gmail API.

This script:
1) opens browser consent screen,
2) captures OAuth code on localhost redirect,
3) exchanges code for tokens,
4) prints refresh token,
5) writes GOOGLE_REFRESH_TOKEN into  .env.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from queue import Queue
from typing import Dict, Optional


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"


class OAuthCodeHandler(BaseHTTPRequestHandler):
    code_queue: "Queue[str]" = Queue()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        error = query.get("error", [None])[0]
        if code:
            self.code_queue.put(code)
            body = (
                "OAuth completed. You can close this tab and return to your terminal.\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        if error:
            body = f"OAuth failed: {error}\n"
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        self.send_response(400)
        self.end_headers()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Get Gmail API refresh token")
    parser.add_argument(
        "--client-id",
        default=os.getenv("GOOGLE_CLIENT_ID", ""),
        help="Google OAuth client id (or set GOOGLE_CLIENT_ID).",
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        help="Google OAuth client secret (or set GOOGLE_CLIENT_SECRET).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file to update (default: .env).",
    )
    parser.add_argument(
        "--timeout-seconds",
        default=300,
        type=int,
        help="Wait timeout for OAuth redirect (default: 300).",
    )
    return parser.parse_args()


def build_auth_url(client_id: str, redirect_uri: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(
    client_id: str, client_secret: str, redirect_uri: str, code: str
) -> Dict[str, str]:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        method="POST",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body


def upsert_env_var(path: Path, key: str, value: str) -> None:
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    output = []
    for line in lines:
        if line.startswith(f"{key}="):
            output.append(f"{key}={value}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    load_env_file(Path(".env"))
    args = parse_args()
    client_id = args.client_id.strip()
    client_secret = args.client_secret.strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing client credentials. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (or pass --client-id/--client-secret)."
        )

    server = HTTPServer(("127.0.0.1", 0), OAuthCodeHandler)
    host, port = server.server_address
    redirect_uri = f"http://{host}:{port}/oauth2callback"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        auth_url = build_auth_url(client_id=client_id, redirect_uri=redirect_uri)
        print("Open this URL in your browser to authorize Gmail API:")
        print(auth_url)
        webbrowser.open(auth_url, new=2)

        code: Optional[str] = None
        try:
            code = OAuthCodeHandler.code_queue.get(timeout=args.timeout_seconds)
        except Exception as exc:
            raise SystemExit(
                f"Timed out waiting for OAuth redirect after {args.timeout_seconds}s."
            ) from exc

        token_response = exchange_code_for_tokens(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code=code,
        )
        refresh_token = token_response.get("refresh_token")
        if not refresh_token:
            raise SystemExit(
                "No refresh_token returned. Ensure OAuth consent included "
                "`prompt=consent` and app type supports offline access."
            )
        print("\nRefresh token:")
        print(refresh_token)

        env_path = Path(args.env_file)
        upsert_env_var(env_path, "GOOGLE_REFRESH_TOKEN", refresh_token)
        print(f"\nWrote GOOGLE_REFRESH_TOKEN to {env_path.resolve()}")
    finally:
        server.shutdown()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
