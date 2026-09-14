# Instagram + Facebook Upload Setup — Full Flow

Get `META_PAGE_TOKEN`, `FB_PAGE_ID`, and `IG_USER_ID` so the Telegram bot's
**📸 IG Reel** and **📘 FB Reel** buttons can publish clips. One-time setup,
~10 minutes. Works alongside the existing YouTube Approve (each platform has
its own button + retry on the same clip message).

> Cost/quota notes:
> - Instagram: ~25 API Reels/day per user (enforced server-side).
> - Facebook: **30 API Reels / 24h** on `POST /{page-id}/video_reels`.
> - Page tokens live **~60 days** — re-run the helper to refresh (unlike
>   YouTube's non-expiring refresh token).

---

## Step 1 — Facebook Page + Instagram Professional

1. You need a **Facebook Page** you admin (or have a role with the
   `CREATE_CONTENT` task on).
2. Your Instagram must be a **Professional account** (Business or Creator):
   IG app → Profile → ☰ → Settings → Account type → Switch to Professional.
3. Link them: Facebook Page → Settings → Linked accounts → Instagram →
   **Connect** and sign in as the IG Professional account.
4. Verify: Page Settings shows the IG username. Without this link, `IG_USER_ID`
   lookup returns empty and IG uploads fail.

## Step 2 — Create a Meta App

1. Go to <https://developers.facebook.com/apps/> → **Create App**.
2. Use case: **Other** → type: **Business**.
3. In App Dashboard → **Add Product** → **Facebook Login for Business**.
4. Note `META_APP_ID` + `META_APP_SECRET` (App Settings → Basic).
5. No App Review needed for your own Page/IG while in Development mode — just
   add yourself (and any testers) under **Roles**.

## Step 3 — Get a short-lived User token (Graph API Explorer)

1. Go to <https://developers.facebook.com/tools/explorer/>.
2. Select your **App** (top-right), then **Get Token → Get User Access Token**.
3. Add permissions:
   - `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`
   - `instagram_basic`, `instagram_content_publish`
4. **Generate** → sign in, grant access → **copy** the short token
   (valid only a few hours — use it immediately in Step 4).

## Step 4 — Exchange for a long-lived Page token (run the helper)

```bash
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

python scripts/get_meta_token.py \
  --user-token <SHORT_TOKEN_FROM_EXPLORER> \
  --app-id <META_APP_ID> \
  --app-secret <META_APP_SECRET>
```

What happens:
1. Exchanges the short token for a ~60-day User token.
2. Lists your Pages (`FB_PAGE_ID`) + linked IG accounts (`IG_USER_ID`).
3. Prints `META_PAGE_TOKEN` (a Page token — post as Page, never expires
   with use, but lapses after ~60 days).

## Step 5 — Put all three in `.env` and restart

```env
META_APP_ID=1234567890
META_APP_SECRET=xxxx
META_PAGE_TOKEN=EAAG...  # ~60 days; re-run helper to refresh
FB_PAGE_ID=1234567890
IG_USER_ID=178414...     # empty until IG Professional is linked
# Optional knobs (defaults shown):
# META_API_VERSION=v26.0
# META_IG_SHARE_TO_FEED=true
# META_FB_FALLBACK_TO_VIDEO=true
```

Restart (`uvicorn app.main:app --reload --port 8000` or restart your process).
`GET /health` now shows `instagram_configured` / `facebook_configured: true`.
The clip messages grow IG/FB buttons only when configured — YT-only before that.

## Step 6 — Test end-to-end

1. Telegram: `/start` → Create Shorts → Timestamp Clip → short range
   (e.g. `00:00 - 00:20`) → Native subtitles.
2. When the clip arrives, tap **📸 IG Reel** → bot replies
   `✅ Instagram Reel published` with a link.
3. Tap **📘 FB Reel** on the *same* message → `✅ Facebook reel published`.
   (Clips **>90s** post as a regular Page video when
   `META_FB_FALLBACK_TO_VIDEO=true`, and say so in the message.)
4. Tap **❌ Deny** on another clip → MP4 deleted, sibling IG/FB tokens
   invalidated (`Already processed` if tapped after).

---

## Troubleshooting

| Symptom | Cause → Fix |
|---|---|
| `IG/FB buttons missing` | `META_PAGE_TOKEN` + ID empty or bot not restarted. Check `GET /health`. |
| `invalid/expired token (190/200)` | Page token lapsed (~60d) or pasted wrong. Re-run `get_meta_token.py`. |
| `missing permissions (200)` | Explorer token lacked scopes. Re-do Step 3 with all five permissions; ensure Page role has `CREATE_CONTENT`. |
| `no Pages found` | Wrong FB user or no Page role. Sign in as Page admin in Explorer. |
| `IG_USER_ID empty` | IG not Professional or not linked to Page. Redo Step 1. |
| `duration not supported (1363128)` | FB Reels need 3–90s. Shorten clip or enable `META_FB_FALLBACK_TO_VIDEO=true`. |
| `resolution/frame rejected (1363xxx)` | Need 540x960+ (we render 1080x1920), 24–60fps H.264. Re-render; don't re-encode manually. |
| `rate limit (368/613)` | IG ~25/day, FB 30/24h spent. Wait, then retry via the Retry button (file is kept). |
| `container ERROR/EXPIRED` (IG) | Transient processing or spec violation. Retry once; then try a shorter clip. |

## Security notes

- `META_PAGE_TOKEN` + `META_APP_SECRET` are **passwords for your Page**.
  Never commit `.env` (already in `.gitignore`), never paste them in chat.
- Scope is limited to posting/listing/reading engagement — it cannot delete
  the Page or manage ads beyond the granted roles.
- To revoke: Page Settings → Page access / Business Settings → remove the App,
  or App Dashboard → Roles → remove yourself.
