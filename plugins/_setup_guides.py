"""Short, spoken setup instructions for A.U.R.A integration services."""
from __future__ import annotations

GUIDES = {
    "gmail": {
        "title": "Gmail",
        "text": "Google OAuth is required, not an API key. Create a Google Cloud project, enable the Gmail API, and create a Desktop OAuth client. Paste its Client ID and Secret into A.U.R.A's Gmail settings, then press Connect / Initialize and approve the browser sign-in. Set the consent screen to In production; Testing refresh tokens expire after seven days.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_docs": {
        "title": "Google Docs",
        "text": "This uses the same Google Desktop OAuth flow as Gmail. The user must create a Google Cloud project, enable the Google Docs API, and sign in once. The plugin stores a refresh token and reuses it for reads and writes.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_sheets": {
        "title": "Google Sheets",
        "text": "Use the same OAuth desktop client as other Google services. Enable the Google Sheets API and sign in once. The user may then create or update sheets without a secret in the app.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_slides": {
        "title": "Google Slides",
        "text": "Enable the Google Slides API in the same Google Cloud project, then sign in once with the Desktop OAuth app. This is a delegated Google account connection, not an API key.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_drive": {
        "title": "Google Drive",
        "text": "Create a Google Cloud project, enable the Drive API, and sign in with a Desktop OAuth client. The user can then create folders, upload files, and share links using their own Google account.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_calendar": {
        "title": "Google Calendar",
        "text": "Use the same Google OAuth desktop app. Enable the Calendar API and keep the Google consent screen in Production for stable refresh tokens. The plugin can list, create, and update events after sign-in.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "outlook": {
        "title": "Outlook / Microsoft 365",
        "text": "Create an app registration in Microsoft Entra for organizational and personal Microsoft accounts, enable public client flows, and enter only the Application ID in A.U.R.A. Sign-in starts with User.Read, Mail.Read and offline_access; turn on the local write-permission switch only if you need Mail.ReadWrite and Mail.Send.",
        "url": "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
    },
    "onedrive": {
        "title": "OneDrive",
        "text": "Register a public-client app in Microsoft Entra for organizational and personal Microsoft accounts, enable public client flows, and enter only the Application ID in A.U.R.A. Sign-in starts with User.Read, Files.Read and offline_access; turn on the local write-permission switch only if you need Files.ReadWrite.",
        "url": "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
    },
    "spotify": {
        "title": "Spotify",
        "text": "Open Spotify for Developers, register an app, copy the Client ID, and use Authorization Code with PKCE. Register http://127.0.0.1:8888/callback exactly as the redirect URI. Spotify Development Mode requires the app owner to have an active Premium subscription and playback control also needs a Premium listener. Without that, the app can open spotify: URIs and use desktop media controls but not full playback control.",
        "url": "https://developer.spotify.com/dashboard",
    },
    "telegram_chats": {
        "title": "Telegram chat brain",
        "text": "Create a BotFather bot, copy the bot token, and store it in the telegram_chats namespace in config/api_keys.json. The bot must have privacy mode disabled for group messaging or be an admin in that chat. Channels and groups require admin rights to post.",
        "url": "https://t.me/BotFather",
    },
    "integrations": {
        "title": "Connected services",
        "text": "Use the integrations plugin to check which services are connected, print the current guide, or start the setup flow for any service. No service works without its own credentials and sign-in flow.",
        "url": "https://aistudio.google.com/apikey",
    },
}


def get_setup_guide(service: str) -> str:
    info = GUIDES.get((service or "").strip().lower(), GUIDES["integrations"])
    return f"{info['title']} setup: {info['text']} URL: {info['url']}"


__all__ = ["GUIDES", "get_setup_guide"]
