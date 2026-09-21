"""One-time Meta token helper: exchanges a short-lived User token for a
long-lived Page token and discovers FB_PAGE_ID + IG_USER_ID for .env.

Prereqs (Meta Developers console, ~10 min — see docs/META_SETUP.md):
  1. Meta App with Facebook Login for Business.
  2. Facebook Page linked to an Instagram Professional (Business/Creator).
  3. You have a role on the Page with CREATE_CONTENT task.

Steps:
  1. Get a short-lived User token via Graph API Explorer with scopes:
     pages_show_list, pages_read_engagement, pages_manage_posts,
     instagram_basic, instagram_content_publish.
  2. Run:
       python scripts/get_meta_token.py --user-token <SHORT_TOKEN>
     (or set META_USER_TOKEN / META_APP_ID / META_APP_SECRET in env/.env)

The script prints META_PAGE_TOKEN + FB_PAGE_ID + IG_USER_ID lines to paste
into .env. Page tokens live ~60 days — re-run to refresh.
"""

import argparse
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _fail(msg: str, code: int = 2) -> int:
    print(f"Error: {msg}", file=sys.stderr)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Get Meta Page token + Page/IG IDs.")
    parser.add_argument("--user-token", default=os.environ.get("META_USER_TOKEN", ""),
                        help="Short-lived User token from Graph API Explorer")
    parser.add_argument("--app-id", default=os.environ.get("META_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.environ.get("META_APP_SECRET", ""))
    parser.add_argument("--api-version", default=os.environ.get("META_API_VERSION", "v26.0"))
    args = parser.parse_args()

    try:
        import requests
    except ImportError:
        return _fail("Missing 'requests'. Run: pip install -r requirements.txt")

    if not args.user_token:
        return _fail("Provide --user-token (short-lived User token from Graph API Explorer).")
    if not (args.app_id and args.app_secret):
        return _fail("Provide --app-id + --app-secret (Meta App dashboard) or set META_APP_ID/_SECRET.")

    version = (args.api_version or "v26.0").strip() or "v26.0"

    # 1. Short-lived -> long-lived User token (~60 days).
    try:
        r = requests.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": args.app_id,
                "client_secret": args.app_secret,
                "fb_exchange_token": args.user_token,
            },
            timeout=60,
        )
        data = r.json()
    except Exception as e:
        return _fail(f"token exchange failed (network): {e}")
    long_token = data.get("access_token", "")
    if not long_token:
        print(f"Exchange response: {data}", file=sys.stderr)
        return _fail("exchange failed — check app id/secret and that the user token is fresh (< few hours old).")

    # 2. List Pages this user can post to (+ linked IG account if any).
    try:
        r = requests.get(
            f"https://graph.facebook.com/{version}/me/accounts",
            params={"fields": "id,name,instagram_business_account{id,username}", "access_token": long_token},
            timeout=60,
        )
        pages = r.json()
    except Exception as e:
        return _fail(f"page lookup failed (network): {e}")

    items = pages.get("data") or []
    if not items:
        print(f"Accounts response: {pages}", file=sys.stderr)
        return _fail("no Pages found — you need a role on the Page (CREATE_CONTENT task).")

    print("\nPages found:")
    for p in items:
        ig = (p.get("instagram_business_account") or {})
        print(f"  - {p.get('name')}  FB_PAGE_ID={p.get('id')}"
              + (f"  IG @{ig.get('username')} IG_USER_ID={ig.get('id')}" if ig.get("id") else "  (no linked IG)"))

    first = items[0]
    first_token = first.get("access_token", "")
    first_ig = (first.get("instagram_business_account") or {}).get("id", "")
    if not first_token:
        # Re-query with per-page token field if missing.
        print("\nNote: page access_token missing — re-run Explorer with pages_* scopes.", file=sys.stderr)

    print("\nSuccess — add this to your .env (first page shown; swap IDs if you picked another):\n")
    if first_token:
        print(f"META_PAGE_TOKEN={first_token}")
    else:
        print("# META_PAGE_TOKEN=<see Pages list above — re-query with pages_* scopes>")
    print(f"FB_PAGE_ID={first.get('id', '')}")
    if first_ig:
        print(f"IG_USER_ID={first_ig}")
    else:
        print("# IG_USER_ID=<empty — link an IG Professional account to the Page first>")
    print(f"META_APP_ID={args.app_id}")
    print("META_APP_SECRET=(already in env — keep secret)")
    print("\nThen restart the bot. IG/FB Approve buttons appear once META_PAGE_TOKEN + IDs are set.")
    print("Token lives ~60 days — re-run this script to refresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
