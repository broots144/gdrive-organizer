"""Drive API helpers: OAuth (one token per scope) and a retrying executor.

Scopes are least privilege per phase:
  `index-drive`  -> drive.metadata.readonly  (cannot read content, cannot write)
  `peek`   -> drive.readonly           (can read content, cannot write)
  `apply`        -> drive                    (needed to move files the app did not create)

Setup (about 20 minutes, once): Google Cloud console -> new project -> enable Drive API ->
OAuth consent screen (External, Testing, add yourself as test user) -> Credentials ->
OAuth client ID -> Desktop app -> download JSON as private/client_secret.json.
Note: apps left in Testing get refresh tokens that expire after 7 days; fine for a one-off.

pip install google-api-python-client google-auth-oauthlib
"""
from __future__ import annotations

import os
import random
import time

SCOPES = {
    "meta": "https://www.googleapis.com/auth/drive.metadata.readonly",
    "read": "https://www.googleapis.com/auth/drive.readonly",
    "write": "https://www.googleapis.com/auth/drive",
}

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}


def service(scope_key: str, client_secret: str, token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scope = [SCOPES[scope_key]]
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scope)
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as ex:  # e.g. invalid_grant: Testing-mode tokens die after 7 days
            print(f"stored token could not be refreshed ({type(ex).__name__}); "
                  "signing in again", flush=True)
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, scope)
        creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(os.path.abspath(token_path)), exist_ok=True)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())
        os.chmod(token_path, 0o600)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _reason(err) -> str:
    try:
        import json
        body = json.loads(err.content.decode("utf-8"))
        return body["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


def call(req, tries: int = 8):
    """Execute a googleapiclient request with exponential backoff on rate limits and 5xx."""
    from googleapiclient.errors import HttpError

    for attempt in range(tries):
        try:
            return req.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status in RETRY_STATUS or (status == 403 and _reason(e) in RETRY_REASONS):
                if attempt == tries - 1:
                    raise
                time.sleep(min(64, 2 ** attempt) + random.random())
                continue
            raise
