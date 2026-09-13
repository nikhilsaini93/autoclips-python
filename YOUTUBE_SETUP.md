# YouTube Upload Setup — Full Flow

Get `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, and `YT_REFRESH_TOKEN` so the
Telegram bot's **Approve** button can upload clips as **Unlisted**.
One-time setup, ~10 minutes. You only ever publish/schedule from YouTube Studio.

> Cost/quota note: one upload ≈ 1,600 quota units. A default project gets
> 10,000 units/day → roughly **6 uploads/day**. Enough for testing and light use.

---

## Step 1 — Create a Google Cloud project

1. Go to <https://console.cloud.google.com/> and sign in with the Google
   account that **owns the YouTube channel** you want to upload to.
2. Top-left project picker → **New Project** → name it e.g. `autoclips-uploader` → **Create**.
3. Make sure the new project is selected in the picker.

## Step 2 — Enable the YouTube Data API v3

1. Go to <https://console.cloud.google.com/apis/library> (or search
   "YouTube Data API v3" in the top search bar).
2. Click **YouTube Data API v3** → **Enable**.
3. Wait for the "API enabled" confirmation.

## Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
   (<https://console.cloud.google.com/apis/credentials/consent>).
2. **User type:** choose **External** → **Create**.
3. Fill the required fields:
   - App name: `AutoClips Uploader` (anything is fine)
   - User support email: your email
   - Developer contact email: your email
4. **Save and Continue** through Scopes (add nothing here) to **Test users**.
5. Under **Test users** click **Add users** → add **your own Gmail address**
   (the channel owner's email) → **Save**.
   - While the app is in *Testing* mode, only test users can sign in — this is you, so it's fine.

## Step 4 — Create the OAuth client (this gives CLIENT_ID + CLIENT_SECRET)

1. Go to **APIs & Services → Credentials**
   (<https://console.cloud.google.com/apis/credentials>).
2. **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app** → name it e.g. `autoclips-desktop` → **Create**.
4. A popup shows your **Client ID** and **Client secret**. Copy both somewhere safe.
   (You can also download the JSON and re-view them later under Credentials.)
   - `YT_CLIENT_ID` ← Client ID (ends with `.apps.googleusercontent.com`)
   - `YT_CLIENT_SECRET` ← Client secret

## Step 5 — Get the REFRESH_TOKEN (run the helper)

On the machine where the bot runs:

```bash
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt

# Option A — you downloaded the client JSON:
python get_youtube_token.py --secrets client_secrets.json

# Option B — you only have the ID + secret:
set YT_CLIENT_ID=your-id.apps.googleusercontent.com
set YT_CLIENT_SECRET=your-secret
python get_youtube_token.py
```

What happens:
1. A browser window opens → pick the **channel-owner Google account**.
2. Google warns "Google hasn't verified this app" — this is normal, it's your own
   app in Testing mode → **Advanced → Go to AutoClips Uploader (unsafe)** → **Allow**.
   (It only asks for permission to *upload videos* — scope `youtube.upload`.)
3. If you have multiple YouTube channels/Brand Accounts, pick the right channel.
4. The script prints:

```text
YT_REFRESH_TOKEN=1//0g-very-long-token...
```

## Step 6 — Put all three in `.env` and restart

```env
YT_CLIENT_ID=your-id.apps.googleusercontent.com
YT_CLIENT_SECRET=your-secret
YT_REFRESH_TOKEN=1//0g-very-long-token...
# Optional: 22 = People & Blogs (default), 24 = Entertainment, 28 = Science & Tech
YT_CATEGORY_ID=22
```

Then restart the bot (`uvicorn main:app --reload --port 8000` or restart your process).
`GET /health` won't show YouTube status, but the first **Approve** tap will prove it works.

## Step 7 — Test end-to-end

1. In Telegram: `/start` → Create Shorts → Timestamp Clip → send a video URL →
   send a short range (e.g. `00:00 - 00:20`) → **Native/English** subtitles.
2. When the clip arrives, tap **✅ Approve → YouTube (Unlisted)**.
3. Bot replies `✅ Uploaded as Unlisted` with a `youtu.be` link.
4. Open **YouTube Studio → Content** → the video is there as **Unlisted**.
   Publish or schedule it whenever you want.
5. Tap **❌ Deny** on another clip → bot deletes the MP4 and reports freed space.

---

## Troubleshooting

| Symptom | Cause → Fix |
|---|---|
| `Access blocked: app's request is invalid` / Error 400 redirect_uri | Client type isn't **Desktop**, or you used a Web client without a redirect. Recreate as **Desktop app**. |
| `invalid_grant` / "token invalid, re-run helper" | Refresh token revoked or pasted wrong. Re-run `get_youtube_token.py` and update `.env`. |
| `quotaExceeded` (≈6 uploads/day) | Default 10k/day quota spent. Wait until next day, or request a quota increase in Cloud Console. |
| Sign-in works for a week then stops | Testing-mode refresh tokens for test users can expire after ~7 days of inactivity. Just re-run the helper. (Publishing the OAuth app to Production removes this, but requires Google verification.) |
| Wrong channel got the upload | The token belongs to whichever channel you picked at consent. Re-run the helper and pick the correct Brand Account/channel. |
| `YouTube OAuth not configured` in bot | One of the three `.env` values is empty and the bot wasn't restarted after editing `.env`. |

## Security notes

- These three values are **passwords for your channel**. Never commit `.env`
  (it's already in `.gitignore`), never paste them in chat, never share screenshots of them.
- The helper's scope is limited to `youtube.upload` — it cannot delete videos,
  read analytics, or manage your account.
- To revoke access anytime: <https://myaccount.google.com/permissions> →
  remove "AutoClips Uploader".
