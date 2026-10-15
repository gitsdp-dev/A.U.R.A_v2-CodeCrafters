# A.U.R.A integrations guide

This project uses a local configuration file at `config/api_keys.json` and never prints tokens or secrets in logs. Google services share Google OAuth credentials; Outlook and OneDrive share a Microsoft app registration; Spotify and Telegram use their own credentials.

## Add credentials in A.U.R.A

1. Open **PLUGIN SETTINGS** from the settings menu.
2. Find the separate service section (Gmail, Google Docs, Google Sheets, Google Slides, Google Drive, Google Calendar, Outlook, OneDrive, Spotify, or Telegram Chat Brain).
3. Enter the app credential fields for that provider. Write-capable Google/Microsoft permissions and Telegram sending are opt-in toggles, off by default.
4. Press **CONNECT / INITIALIZE** in that section and complete the browser/device sign-in. For a credential-only check, press that section's **TEST CONNECTION** button.
5. Repeat only for the services you intend to use. The status line below each section reports the result.

Credentials entered in these sections are stored in the ignored `config/api_keys.json`; they are not included in the model's tool arguments. Google, Microsoft, and Spotify connections request read access unless the section's write toggle is enabled.

## Service setup summary

| Service | Free route | Notes |
| --- | --- | --- |
| Gmail / Google Workspace APIs | Google Cloud project + Desktop OAuth client | OAuth refresh token, not an API key. Use a Desktop app and keep the Google consent screen in production for long-lived tokens. |
| Google Docs / Sheets / Slides / Drive / Calendar | Same Google Cloud project, same Desktop OAuth client | Use the service-specific APIs and sign in once with the user's Google account. |
| Outlook / OneDrive | Microsoft Entra app registration with public client flow | Grants delegated Graph permissions for Mail, Files and offline access. |
| Spotify | Spotify Developer dashboard + Client ID with PKCE | Requires Premium for the app owner for some development-mode features and playback control; fallback is to open spotify: URIs or use OS media keys. |
| Telegram chat brain | BotFather bot token | The bot token is the only plain API key here. Privacy mode and admin rights matter for groups and channels. |

## Storage layout

The config file is already ignored by `.gitignore` and should contain values under namespaces such as:

```json
{
  "gemini_api_key": "...",
  "google": { "client_id": "", "client_secret": "", "refresh_token": "" },
  "microsoft": { "client_id": "", "tenant": "common", "refresh_token": "" },
  "spotify": { "client_id": "", "refresh_token": "" },
  "telegram_chats": { "bot_token": "" }
}
```

## Example voice commands

- "Summarise my unread mail."
- "Put the invoice from Priya into my Budget sheet."
- "Add a slide about it."
- "Find the latest file in my OneDrive called invoice."
- "Send this draft to the project group."
- "Watch my email for important messages."

## Honest free-tier caveats

- Google and Microsoft APIs are free to use for personal experimentation but require a small OAuth setup and a real user account.
- Spotify is not guaranteed free for every user. Development Mode requires the app owner to have active Premium; playback control also needs a Premium listener.
- The app should degrade gracefully when credentials or subscriptions are missing, instead of pretending a service is fully enabled.
