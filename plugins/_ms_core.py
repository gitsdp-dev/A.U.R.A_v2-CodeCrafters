"""Microsoft Graph helpers for A.U.R.A plugins."""
from __future__ import annotations

import time
from typing import Any

import requests

from ._keystore import get_service_config, set_service_config, safe_error

MS_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def ms_config() -> dict:
    return get_service_config("microsoft")


def device_code_request(service: str = "outlook", *, allow_writes: bool = False) -> dict:
    cfg = ms_config()
    client_id = str(cfg.get("client_id", "")).strip()
    if not client_id:
        raise RuntimeError("Microsoft client ID is missing. Register a public-client app in Microsoft Entra and paste the Application ID.")
    if service == "onedrive":
        scopes = ["User.Read", "offline_access", "Files.ReadWrite" if allow_writes else "Files.Read"]
    else:
        scopes = ["User.Read", "offline_access"]
        scopes.extend(
            ["Mail.ReadWrite", "Mail.Send"] if allow_writes else ["Mail.Read"]
        )
    payload = {
        "client_id": client_id,
        "scope": " ".join(scopes),
    }
    resp = requests.post(MS_AUTH_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Microsoft sign-in request failed: {safe_error(resp.text)}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Microsoft device-code flow returned an unexpected response.")
    return data


def exchange_ms_code(device_code: str, client_id: str) -> dict:
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }
    resp = requests.post(MS_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        text = resp.text
        if "authorization_pending" in text:
            raise RuntimeError("Microsoft sign-in is still pending. Please keep the browser open and retry a moment later.")
        if "slow_down" in text:
            raise RuntimeError("Microsoft sign-in is polling too quickly. Please retry in a moment.")
        raise RuntimeError(f"Microsoft token exchange failed: {safe_error(text)}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Microsoft token exchange returned an unexpected response.")
    return data


def refresh_ms_access_token() -> dict:
    cfg = ms_config()
    client_id = str(cfg.get("client_id", "")).strip()
    refresh_token = str(cfg.get("refresh_token", "")).strip()
    if not client_id or not refresh_token:
        raise RuntimeError("Microsoft is not connected yet. Use the connect flow for Outlook or OneDrive.")
    payload = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": " ".join(cfg.get("scopes") or [
            "User.Read", "Mail.Read", "Files.Read", "offline_access"
        ]),
    }
    resp = requests.post(MS_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Microsoft refresh failed: {safe_error(resp.text)}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Microsoft refresh returned an unexpected payload.")
    if "access_token" in data:
        cfg["access_token"] = data["access_token"]
        cfg["token_expiry"] = int(time.time()) + int(data.get("expires_in", 3600))
        set_service_config("microsoft", cfg)
    return data


def _legacy_ms_headers() -> dict[str, str]:
    cfg = ms_config()
    token = str(cfg.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Microsoft is not connected yet. Please complete the device-code sign-in first.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _request_headers() -> dict[str, str]:
    token = str(ms_config().get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Microsoft is not connected yet. Please complete the device-code sign-in first.")
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def ms_headers() -> dict[str, str]:
    return _request_headers()


def ms_request(endpoint: str, *, method: str = "GET", params: dict | None = None, payload: Any = None, timeout: int = 30) -> Any:
    headers = _request_headers()
    url = f"{MS_GRAPH_BASE}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
            if resp.status_code == 401:
                refresh_ms_access_token()
                headers = _request_headers()
                resp = requests.request(method, url, headers=headers, params=params, json=payload, timeout=timeout)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Microsoft Graph request failed ({resp.status_code}): {safe_error(resp.text)}")
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except RuntimeError:
            raise
    raise RuntimeError("Microsoft Graph request failed without a detailed error.")


__all__ = ["ms_config", "device_code_request", "exchange_ms_code", "refresh_ms_access_token", "ms_headers", "ms_request"]
