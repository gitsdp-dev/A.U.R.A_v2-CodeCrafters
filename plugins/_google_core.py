"""Google OAuth and REST helpers for the A.U.R.A plugin family.

WHY THIS EXISTS
    Google services (Gmail, Calendar, Drive, Docs, Sheets, Slides) share a
    single OAuth model and a single refresh-token storage pattern. This module
    centralises it so each plugin behaves the same way and does not rewrite
    credential logic.

HOW IT WORKS
    OAuth is done with a desktop app client ID + secret flow. The refresh
    token is kept in api_keys.json under the service namespace. Requests use
    lazy imports and JSON helpers, with a timeout and retry/backoff policy.

SECURITY
    Tokens are never returned to the model. They are stored locally and all
    HTTP errors are translated into short, plain-English messages.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ._keystore import get_service_config, set_service_config, safe_error

GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_BASE = "https://www.googleapis.com"


def google_config() -> dict:
    return get_service_config("google")


def google_scopes(service: str, *, allow_writes: bool = False) -> list[str]:
    read_scopes = {
        "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
        "google_docs": ["https://www.googleapis.com/auth/documents.readonly"],
        "google_sheets": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
        "google_slides": ["https://www.googleapis.com/auth/presentations.readonly"],
        "google_drive": ["https://www.googleapis.com/auth/drive.readonly"],
        "google_calendar": ["https://www.googleapis.com/auth/calendar.readonly"],
    }
    write_scopes = {
        "gmail": ["https://www.googleapis.com/auth/gmail.modify"],
        "google_docs": ["https://www.googleapis.com/auth/documents"],
        "google_sheets": ["https://www.googleapis.com/auth/spreadsheets"],
        "google_slides": ["https://www.googleapis.com/auth/presentations"],
        "google_drive": ["https://www.googleapis.com/auth/drive.file"],
        "google_calendar": ["https://www.googleapis.com/auth/calendar.events"],
    }
    return read_scopes.get(service, []) + (
        write_scopes.get(service, []) if allow_writes else []
    )


def google_authorize_url(
    service: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    *,
    state: str = "",
    code_challenge: str = "",
    scopes: list[str] | None = None,
) -> str:
    cfg = google_config()
    client_id = str(cfg.get("client_id", "")).strip()
    if not client_id:
        raise RuntimeError("Google OAuth is not configured yet. Use the setup guide to create a Desktop app client ID.")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes or google_scopes(service)),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    import urllib.parse
    return f"{GOOGLE_OAUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_google_code(
    service: str,
    code: str,
    redirect_uri: str = "http://127.0.0.1:8888/callback",
    *,
    code_verifier: str = "",
) -> dict:
    cfg = google_config()
    client_id = str(cfg.get("client_id", "")).strip()
    client_secret = str(cfg.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        raise RuntimeError("Google OAuth is not fully configured. Add client ID and client secret first.")
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Google token exchange failed: {safe_error(resp.text)}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Google token exchange returned an unexpected response.")
    return data


def refresh_google_access_token() -> dict:
    cfg = google_config()
    client_id = str(cfg.get("client_id", "")).strip()
    client_secret = str(cfg.get("client_secret", "")).strip()
    refresh_token = str(cfg.get("refresh_token", "")).strip()
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("Google refresh token is missing. Connect the service again.")
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        if "invalid_grant" in resp.text:
            raise RuntimeError(
                "Google sign-in expired. Set the consent screen to In production "
                "(Testing refresh tokens expire after 7 days), then reconnect."
            )
        raise RuntimeError(f"Google refresh failed: {safe_error(resp.text)}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Google token refresh returned an unexpected payload.")
    if "access_token" in data:
        cfg["access_token"] = data["access_token"]
        cfg["token_expiry"] = int(time.time()) + int(data.get("expires_in", 3600))
        set_service_config("google", cfg)
    return data


def _legacy_google_headers(service: str | None = None) -> dict[str, str]:
    cfg = google_config()
    token = str(cfg.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google is not connected. Please complete the OAuth sign-in first.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _request_headers() -> dict[str, str]:
    token = str(google_config().get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google is not connected. Please complete the OAuth sign-in first.")
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def google_headers(service: str | None = None) -> dict[str, str]:
    return _request_headers()


def google_request(service: str, endpoint: str, *, method: str = "GET", params: dict | None = None, payload: Any = None, timeout: int = 30) -> Any:
    headers = _request_headers()
    url = f"{GOOGLE_BASE}{endpoint}"
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
            if resp.status_code == 401:
                refresh_google_access_token()
                headers = _request_headers()
                resp = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Google API request failed ({resp.status_code}): {safe_error(resp.text)}")
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except RuntimeError:
            raise
        except Exception as exc:  # pragma: no cover
            last_error = exc
            time.sleep(0.5)
    if last_error is not None:
        raise RuntimeError(str(last_error))
    raise RuntimeError("Google request failed without a detailed error.")


__all__ = [
    "google_config",
    "google_scopes",
    "google_authorize_url",
    "exchange_google_code",
    "refresh_google_access_token",
    "google_headers",
    "google_request",
]
