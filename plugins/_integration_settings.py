"""Settings and connection actions shared by the service plugins.

WHY THIS EXISTS
    The Plugin Settings window renders the schemas exposed by loaded plugins.
    This module gives every integration its own credential section and the same
    explicit Connect and Test actions.

HOW IT WORKS
    Credentials are loaded from and saved to the existing service namespaces in
    api_keys.json. OAuth flows run in the settings worker thread and report
    progress through the UI-provided callback.

SECURITY
    Secrets stay in local config, are never returned by a connection action, and
    provider exceptions are converted to short, scrubbed messages.

WHAT IT DOES NOT DO
    It does not start background polling or silently grant write permissions.
"""
from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from ._keystore import get_service_config, safe_error, set_service_config

_GOOGLE_SERVICES = {
    "gmail", "google_docs", "google_sheets", "google_slides",
    "google_drive", "google_calendar",
}
_MICROSOFT_SERVICES = {"outlook", "onedrive"}
_SERVICE_NAMESPACES = {
    **{name: "google" for name in _GOOGLE_SERVICES},
    **{name: "microsoft" for name in _MICROSOFT_SERVICES},
    "spotify": "spotify",
    "telegram_chats": "telegram_chats",
}
_GOOGLE_LABELS = {
    "gmail": "Gmail",
    "google_docs": "Google Docs",
    "google_sheets": "Google Sheets",
    "google_slides": "Google Slides",
    "google_drive": "Google Drive",
    "google_calendar": "Google Calendar",
    "outlook": "Outlook",
    "onedrive": "OneDrive",
    "spotify": "Spotify",
    "telegram_chats": "Telegram chat brain",
}


def _credential_keys(service: str) -> tuple[str, ...]:
    if service in _GOOGLE_SERVICES:
        return ("client_id", "client_secret", "allow_writes")
    if service in _MICROSOFT_SERVICES:
        return ("client_id", "allow_writes")
    if service == "spotify":
        return ("client_id", "allow_writes")
    if service == "telegram_chats":
        return (
            "bot_token", "allow_send", "start_on_launch",
            "skip_picker_on_exact_match", "mode_b_default",
        )
    return ()


def load_service_values(service: str) -> dict[str, Any]:
    namespace = _SERVICE_NAMESPACES.get(service, service)
    config = get_service_config(namespace)
    values = {}
    for key in _credential_keys(service):
        stored_key = f"{key}_{service}" if key == "allow_writes" else key
        values[key] = config.get(
            stored_key,
            False if key in {"allow_writes", "allow_send", "start_on_launch",
                             "skip_picker_on_exact_match"} else "off" if key == "mode_b_default" else "",
        )
    return values


def save_service_values(service: str, values: dict[str, Any]) -> None:
    namespace = _SERVICE_NAMESPACES.get(service)
    if not namespace:
        raise ValueError("Unknown integration service.")
    saved = {
        f"{key}_{service}" if key == "allow_writes" else key: values[key]
        for key in _credential_keys(service) if key in values
    }
    set_service_config(namespace, saved)


def build_plugin_settings(service: str) -> dict[str, Any]:
    """Return a Plugin Settings schema for one integration."""
    label = _GOOGLE_LABELS[service]
    namespace = f"integration_{service}"
    if service in _GOOGLE_SERVICES:
        fields = [
            {"key": "client_id", "label": "Google OAuth Desktop Client ID", "type": "text",
             "placeholder": "Paste the OAuth Client ID from Google Cloud"},
            {"key": "client_secret", "label": "Google OAuth Desktop Client Secret", "type": "password",
             "placeholder": "Paste the OAuth Client Secret from Google Cloud"},
            {"key": "allow_writes", "label": f"Allow {label} to make changes",
             "type": "toggle", "default": False},
        ]
    elif service in _MICROSOFT_SERVICES:
        fields = [
            {"key": "client_id", "label": "Microsoft Application (client) ID", "type": "text",
             "placeholder": "No client secret is needed for public-client sign-in"},
            {"key": "allow_writes", "label": f"Allow {label} to make changes",
             "type": "toggle", "default": False},
        ]
    elif service == "spotify":
        fields = [
            {"key": "client_id", "label": "Spotify Client ID", "type": "text",
             "placeholder": "Client ID from the Spotify Developer dashboard"},
            {"key": "allow_writes", "label": "Allow playlist, library and playback changes",
             "type": "toggle", "default": False},
        ]
    elif service == "telegram_chats":
        fields = [
            {"key": "bot_token", "label": "BotFather bot token", "type": "password",
             "placeholder": "Token from @BotFather"},
            {"key": "allow_send", "label": "Allow AURA to send messages as my bot",
             "type": "toggle", "default": False},
        ]
    else:
        raise ValueError("Unknown integration service.")

    return {
        "namespace": namespace,
        "title": f"{label.upper()} — CREDENTIALS & CONNECTION",
        "fields": fields,
        "load": lambda s=service: load_service_values(s),
        "save": lambda values, s=service: save_service_values(s, values),
        "actions": [
            {
                "label": "CONNECT / INITIALIZE",
                "run": lambda values, progress=None, s=service:
                    connect_service(s, values, progress=progress),
            },
            {
                "label": "TEST CONNECTION",
                "run": lambda values, progress=None, s=service:
                    test_service(s, values, progress=progress),
            },
        ],
    }


def _loopback_authorization(
    authorization_url: str,
    state: str,
    *,
    port: int = 0,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Open a local one-shot OAuth callback and return its code/error."""
    result: dict[str, str] = {}
    ready = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received_state = (query.get("state") or [""])[0]
            if not secrets.compare_digest(received_state, state):
                result["error"] = "OAuth state verification failed. Please reconnect."
            else:
                result["code"] = (query.get("code") or [""])[0]
                result["error"] = (query.get("error_description") or query.get("error") or [""])[0]
            body = b"Sign-in completed. You can close this browser window and return to A.U.R.A."
            self.send_response(200 if result.get("code") else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            ready.set()

        def log_message(self, _format, *_args):
            return

    try:
        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    except OSError as exc:
        if port:
            raise RuntimeError(
                f"Could not start Spotify's sign-in listener on port {port}. "
                "Close the app using that port and try again."
            ) from exc
        raise RuntimeError(
            "Could not start the local sign-in listener. Restart A.U.R.A and try again."
        ) from exc
    server.timeout = 1
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    parsed = urllib.parse.urlparse(authorization_url)
    query = urllib.parse.parse_qs(parsed.query)
    query["redirect_uri"] = [redirect_uri]
    url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
    try:
        if progress:
            progress("Opening sign-in in your browser; waiting up to 2 minutes.")
        import webbrowser
        if not webbrowser.open(url):
            raise RuntimeError("Could not open your browser. Set a default browser and press Connect again.")
        deadline = time.monotonic() + 120
        while not ready.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if not ready.is_set():
            raise RuntimeError("Sign-in timed out. Press Connect and try again.")
        if result.get("error"):
            raise RuntimeError("Sign-in was not completed. Please reconnect and approve the requested access.")
        code = result.get("code", "")
        if not code:
            raise RuntimeError("The sign-in response did not include an authorization code. Please reconnect.")
        return code, redirect_uri
    finally:
        server.server_close()


def _connect_google(service: str, values: dict[str, Any],
                    progress: Callable[[str], None] | None) -> tuple[bool, str]:
    save_service_values(service, values)
    import secrets as _secrets
    from ._google_core import exchange_google_code, google_authorize_url, google_scopes

    state = _secrets.token_urlsafe(32)
    verifier = _secrets.token_urlsafe(48)
    challenge = __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    challenge = __import__("base64").urlsafe_b64encode(challenge).decode("ascii").rstrip("=")
    scopes = google_scopes(service, allow_writes=bool(values.get("allow_writes")))
    auth_url = google_authorize_url(
        service, redirect_uri="http://127.0.0.1:0/callback", state=state,
        code_challenge=challenge, scopes=scopes,
    )
    code, redirect_uri = _loopback_authorization(auth_url, state, progress=progress)
    token_data = exchange_google_code(
        service, code, redirect_uri=redirect_uri, code_verifier=verifier,
    )
    if not token_data.get("refresh_token"):
        raise RuntimeError("Google did not return a refresh token. Reconnect and approve offline access.")
    current = get_service_config("google")
    current.update({
        "refresh_token": token_data["refresh_token"],
        "access_token": token_data.get("access_token", ""),
        "scopes": scopes,
        "token_expiry": int(time.time()) + int(token_data.get("expires_in", 3600)),
    })
    set_service_config("google", current)
    return True, f"{_GOOGLE_LABELS[service]} connected. Sign-in credentials were saved locally."


def _connect_microsoft(service: str, values: dict[str, Any],
                       progress: Callable[[str], None] | None) -> tuple[bool, str]:
    save_service_values(service, values)
    import requests

    from ._ms_core import MS_TOKEN_URL, device_code_request, ms_config

    device = device_code_request(service=service, allow_writes=bool(values.get("allow_writes")))
    if progress:
        progress(str(device.get("message") or "Complete Microsoft sign-in in your browser."))
    deadline = time.monotonic() + int(device.get("expires_in", 900))
    interval = max(1, int(device.get("interval", 5)))
    while time.monotonic() < deadline:
        time.sleep(interval)
        response = requests.post(
            MS_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": ms_config().get("client_id", ""),
                "device_code": device.get("device_code", ""),
            },
            timeout=20,
        )
        data = response.json()
        if response.ok and data.get("access_token"):
            config = get_service_config("microsoft")
            scopes = str(data.get("scope", "")).split()
            if not scopes:
                scopes = ["User.Read", "offline_access"]
                if service == "onedrive":
                    scopes.append("Files.ReadWrite" if values.get("allow_writes") else "Files.Read")
                else:
                    scopes.extend(
                        ["Mail.ReadWrite", "Mail.Send"]
                        if values.get("allow_writes") else ["Mail.Read"]
                    )
            config.update({
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "token_expiry": int(time.time()) + int(data.get("expires_in", 3600)),
                "scopes": scopes,
                "account": data.get("id_token_claims", {}).get("preferred_username", ""),
            })
            set_service_config("microsoft", config)
            return True, f"{_GOOGLE_LABELS[service]} connected. Sign-in credentials were saved locally."
        error = data.get("error")
        if error in {"authorization_pending", "slow_down"}:
            if error == "slow_down":
                interval += 5
            continue
        if error:
            raise RuntimeError("Microsoft sign-in was not completed. Please reconnect and approve the requested access.")
    raise RuntimeError("Microsoft sign-in timed out. Press Connect and try again.")


def _connect_spotify(values: dict[str, Any],
                     progress: Callable[[str], None] | None) -> tuple[bool, str]:
    save_service_values("spotify", values)
    import base64
    import hashlib
    import requests

    client_id = str(values.get("client_id") or "").strip()
    if not client_id:
        raise RuntimeError("Add the Spotify Client ID, then press Connect.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    scope = "user-read-private user-read-playback-state playlist-read-private"
    if values.get("allow_writes"):
        scope += (
            " user-modify-playback-state playlist-modify-private "
            "playlist-modify-public user-library-modify"
        )
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": scope,
    })
    code, redirect_uri = _loopback_authorization(
        auth_url, state, port=8888, progress=progress,
    )
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError("Spotify sign-in failed. Check the Client ID and exact 127.0.0.1 redirect URI, then reconnect.")
    data = response.json()
    config = get_service_config("spotify")
    config.update({
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "token_expiry": int(time.time()) + int(data.get("expires_in", 3600)),
    })
    set_service_config("spotify", config)
    return True, "Spotify connected. Playback controls can still require Premium."


def connect_service(service: str, values: dict[str, Any],
                    progress: Callable[[str], None] | None = None) -> tuple[bool, str]:
    try:
        if service in _GOOGLE_SERVICES:
            return _connect_google(service, values, progress)
        if service in _MICROSOFT_SERVICES:
            return _connect_microsoft(service, values, progress)
        if service == "spotify":
            return _connect_spotify(values, progress)
        if service == "telegram_chats":
            save_service_values(service, values)
            ok, message = _test_telegram(values)
            if not ok:
                return ok, message
            return True, f"{message} The local chat brain is initialized; it has not started polling."
        return False, "Unknown service. Check the selected integration section."
    except Exception as exc:
        return False, safe_error(exc)


def _test_telegram(values: dict[str, Any]) -> tuple[bool, str]:
    import requests

    token = str(values.get("bot_token") or "").strip()
    if not token:
        return False, "Add the BotFather bot token, then press Test Connection."
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        data = response.json()
    except Exception:
        return False, "Could not reach Telegram. Check the bot token and internet connection."
    if not response.ok or not data.get("ok"):
        return False, "Telegram rejected the bot token. Check it with @BotFather and try again."
    bot = data.get("result", {})
    set_service_config("telegram_chats", {
        "bot_username": bot.get("username", ""),
        "bot_id": bot.get("id", ""),
    })
    return True, f"Connected to @{bot.get('username') or 'bot'}; token verified."


def _refresh_spotify(config: dict[str, Any]) -> dict[str, Any]:
    import requests

    client_id = str(config.get("client_id") or "").strip()
    refresh_token = str(config.get("refresh_token") or "").strip()
    if not client_id or not refresh_token:
        raise RuntimeError("Spotify is not signed in. Press Connect / Initialize first.")
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError("Spotify sign-in expired. Press Connect / Initialize again.")
    data = response.json()
    config["access_token"] = data.get("access_token", "")
    config["token_expiry"] = int(time.time()) + int(data.get("expires_in", 3600))
    if data.get("refresh_token"):
        config["refresh_token"] = data["refresh_token"]
    set_service_config("spotify", config)
    return config


def test_service(service: str, values: dict[str, Any],
                 progress: Callable[[str], None] | None = None) -> tuple[bool, str]:
    try:
        save_service_values(service, values)
        if service in _GOOGLE_SERVICES:
            from ._google_core import google_request, refresh_google_access_token
            if service == "gmail":
                google_request(service, "/gmail/v1/users/me/profile")
            elif service == "google_calendar":
                google_request(service, "/calendar/v3/users/me/calendarList")
            elif service == "google_drive":
                google_request(service, "/drive/v3/about", params={"fields": "user"})
            else:
                import requests
                from ._google_core import google_scopes
                token_data = refresh_google_access_token()
                access_token = str(token_data.get("access_token") or "")
                if not access_token:
                    return False, "Google returned no access token. Reconnect this service."
                response = requests.get(
                    "https://oauth2.googleapis.com/tokeninfo",
                    params={"access_token": access_token},
                    timeout=20,
                )
                if not response.ok:
                    return False, "Google sign-in could not be verified. Reconnect and try again."
                granted = set(str(response.json().get("scope", "")).split())
                required = set(google_scopes(
                    service, allow_writes=bool(values.get("allow_writes"))
                ))
                if not required.issubset(granted):
                    return False, (
                        "Google sign-in is missing this service's requested permissions. "
                        "Press Connect / Initialize again and approve access."
                    )
                return True, (
                    f"{_GOOGLE_LABELS[service]} sign-in and permissions verified; "
                    "the API will be checked when you use it."
                )
            return True, f"{_GOOGLE_LABELS[service]} API connection verified."
        if service in _MICROSOFT_SERVICES:
            from ._ms_core import ms_request
            ms_request("/me", params={"$select": "id"})
            return True, f"{_GOOGLE_LABELS[service]} Microsoft sign-in verified."
        if service == "spotify":
            import requests
            config = get_service_config("spotify")
            token = str(config.get("access_token") or "")
            expiry = int(config.get("token_expiry", 0) or 0)
            if not token or expiry < int(time.time()):
                config = _refresh_spotify(config)
                token = str(config.get("access_token") or "")
            response = requests.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            if not response.ok:
                return False, "Spotify sign-in could not be verified. Reconnect and try again."
            return True, "Spotify account connection verified."
        if service == "telegram_chats":
            return _test_telegram(values)
        return False, "Unknown service. Check the selected integration section."
    except Exception as exc:
        return False, safe_error(exc)


__all__ = [
    "build_plugin_settings",
    "connect_service",
    "load_service_values",
    "save_service_values",
    "test_service",
]
