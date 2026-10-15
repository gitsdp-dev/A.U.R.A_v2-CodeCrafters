# A.U.R.A: Required Keys and How to Get Them
 
A.U.R.A works locally first. You only need the keys for the features you want to use. All of these can be obtained for free, with the exceptions noted for Spotify.
 
| Key | Needed for |
|---|---|
| Gemini API key | The assistant itself (required) |
| Telegram bot token | Telegram remote control and Telegram chat plugin |
| Google OAuth Client ID + Secret | Gmail, Docs, Sheets, Slides, Drive, Calendar |
| Microsoft Application (Client) ID | Outlook, OneDrive |
| Spotify Client ID | Spotify (Premium needed, see below) |
 
All keys go in `config/api_keys.json`. You can either paste them into the plugin's panel in A.U.R.A's settings, which saves them for you, or edit the file by hand. Never commit this file to Git or share it.
 
```json
{
  "gemini_api_key": "YOUR_GEMINI_KEY",
  "telegram_chats": { "bot_token": "YOUR_TELEGRAM_BOT_TOKEN" },
  "google":    { "client_id": "", "client_secret": "" },
  "microsoft": { "client_id": "" },
  "spotify":   { "client_id": "" }
}
```
 
The `refresh_token` fields are filled in automatically after you sign in. Don't type them yourself.
 
---
 
## 1) Gemini API key (required)
 
1. Open https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key** and copy it.
3. Paste it into A.U.R.A's key prompt on first launch, or into `config/api_keys.json` as `gemini_api_key`.
The free tier has rate limits, and limits can change, so check AI Studio if you hit one.
 
---
 
## 2) Telegram bot token
 
1. In Telegram, message **@BotFather** and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token BotFather gives you.
4. Paste it into the Telegram plugin's settings panel, then press **TEST CONNECTION**.
For the remote-control plugin, also set a **pairing code** in settings. Start the plugin, and only then send that code to your bot from your phone.
 
If you want the bot to work in groups and channels:
- **Groups:** add the bot to the group. To let it see all messages, send `/setprivacy` to BotFather, choose your bot, and select **Disable**. You can also make the bot a group admin.
- **Channels:** add the bot as a channel **administrator** with permission to post.
- A bot can only message people who have started it first, and it can only see messages sent after it was added.
If you use one bot for remote control and another for chats, give each its own token. Two programs can't poll the same token at the same time.
 
---
 
## 3) Google (Gmail, Docs, Sheets, Slides, Drive, Calendar)
 
Google doesn't allow a plain API key to access your private data. You create a free OAuth client instead, which takes about 10 minutes, and billing isn't required.
 
1. Go to https://console.cloud.google.com and create a new project.
2. Open **APIs & Services → Library** and enable: Gmail API, Google Docs API, Google Sheets API, Google Slides API, Google Drive API and Google Calendar API.
3. Open **Google Auth Platform** (formerly "OAuth consent screen") and click **Get started**. Under **Audience**, choose **External**, fill in the app name and your email, and add your own Google account as a test user.
   - **If External isn't offered:** you are most likely signed in with a school or work (Google Workspace) account, so the project belongs to an organization and only **Internal** is available. Either (a) sign in with a personal `@gmail.com` account (use a private window or another browser profile), create a new project there, and choose External, or (b) if the account you want A.U.R.A to use is that same Workspace account, choose **Internal**. Internal works fine for your own organization's accounts. It has no test-user list, no 7-day token expiry and no unverified-app warning, so you can skip the "In production" step below.
   - **If the project is already set to Internal:** open **Audience** and look for a **Make external** button.
4. **Publish the app** so its status is **In production**. If you leave it in "Testing", your sign-in expires every 7 days. For personal use you don't need Google's verification. You'll just see an "unverified app" warning later.
5. Open **Credentials → Create credentials → OAuth client ID** and choose **Desktop app**. Copy the **Client ID** and **Client secret**.
6. Paste both into the Google section of A.U.R.A's settings and press **CONNECT**.
7. Your browser opens. Sign in, click **Advanced → Go to (app name) (unsafe)** on the warning page, and allow the permissions. A.U.R.A saves the refresh token, so you only do this once.
The menu names in Google Cloud change from time to time, so look for the equivalent if one has moved.
 
---
 
## 4) Microsoft (Outlook, OneDrive)
 
No secret is needed, only the Application (Client) ID. Registration is free.
 
1. Go to https://entra.microsoft.com and sign in. A personal Microsoft account works, and you may be asked to create a free default directory.
2. Open **App registrations → New registration**.
3. Name it, and under account types choose **Accounts in any organizational directory and personal Microsoft accounts**. Leave the redirect URI blank and register.
4. Open **Authentication → Advanced settings** and set **Allow public client flows** to **Yes**. Save.
5. Open **API permissions → Add a permission → Microsoft Graph → Delegated** and add: `Mail.ReadWrite`, `Mail.Send`, `Files.ReadWrite`, `offline_access`, `User.Read`.
6. On the **Overview** page, copy the **Application (client) ID**.
7. Paste it into the Microsoft section of A.U.R.A's settings and press **CONNECT**. A.U.R.A shows a short code and a link. Open the link, enter the code and sign in.
---
 
## 5) Spotify (not guaranteed free)
 
Since February 2026, Spotify requires the owner of a Development Mode app to have an active **Premium** subscription, and controlling playback also needs Premium. Without Premium, A.U.R.A can only open Spotify and use media keys. Check developer.spotify.com for the current rules, since they have changed several times this year.
 
1. Go to https://developer.spotify.com/dashboard and sign in.
2. Click **Create app**, choose **Web API**, and add this exact redirect URI: `http://127.0.0.1:8888/callback`. Spotify doesn't accept `localhost` here.
3. Copy the **Client ID**. No secret is needed.
4. Paste it into the Spotify section of A.U.R.A's settings and press **CONNECT**, then approve the sign-in in your browser.
---
 
## Troubleshooting
 
- **Google sign-in stops working after a week:** your consent screen is still in "Testing". Switch it to "In production" and run CONNECT again.
- **"invalid_grant" or "token expired":** run CONNECT again for that service.
- **Telegram bot ignores a group:** disable privacy mode or make the bot an admin, then send a new message.
- **Telegram reports a conflict:** a second program is polling the same bot token. Stop one, or give each its own bot.
## Security
 
- Keep `config/api_keys.json` in `.gitignore`.
- If a key leaks, revoke it: `/revoke` in BotFather, delete the OAuth client in Google Cloud, delete the app registration in Entra, or remove the app in the Spotify dashboard.
- A.U.R.A never needs your account passwords, only these keys and the sign-in approvals above.