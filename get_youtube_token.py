"""One-time YouTube OAuth helper: prints a refresh token for .env.

Prereqs (5 min, Google Cloud Console):
  1. New project → enable "YouTube Data API v3".
  2. OAuth consent screen → External → add your Gmail as test user.
  3. Credentials → Create Credentials → OAuth client ID → Desktop app.
  4. Download the client JSON or copy the client ID + secret.

Usage:
  # Option A (client JSON file — easiest):
  python get_youtube_token.py --secrets client_secrets.json

  # Option B (env vars):
  set YT_CLIENT_ID=... & set YT_CLIENT_SECRET=... & python get_youtube_token.py

A browser window opens → sign in with the channel-owning Google account →
Allow → paste the code back if asked. The script prints YT_REFRESH_TOKEN;
add all three values to .env. The token is long-lived; re-run only if you
see "invalid_grant" (revoked) errors.
"""

import argparse
import json
import os
import sys

try:
    # Load .env so YT_CLIENT_ID / YT_CLIENT_SECRET set there are picked up.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Get a YouTube upload refresh token.")
    parser.add_argument("--secrets", default="client_secrets.json",
                        help="OAuth client JSON file (default: client_secrets.json)")
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing deps. Run:  pip install -r requirements.txt", file=sys.stderr)
        return 2

    client_id = os.environ.get("YT_CLIENT_ID", "")
    client_secret = os.environ.get("YT_CLIENT_SECRET", "")

    if os.path.exists(args.secrets):
        flow = InstalledAppFlow.from_client_secrets_file(args.secrets, SCOPES)
    elif client_id and client_secret:
        flow = InstalledAppFlow.from_client_config(
            {"installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }},
            SCOPES,
        )
    else:
        print("Provide client_secrets.json or set YT_CLIENT_ID + YT_CLIENT_SECRET.", file=sys.stderr)
        return 2

    creds = flow.run_local_server(port=0, prompt="consent")
    print("\n success — add this to your .env:\n")
    print(f"YT_REFRESH_TOKEN={creds.refresh_token}")
    if client_id:
        print(f"YT_CLIENT_ID={client_id}")
    if client_secret:
        print("YT_CLIENT_SECRET=(already in env)")
    if os.path.exists(args.secrets):
        try:
            data = json.load(open(args.secrets, encoding="utf-8"))
            inst = data.get("installed", {})
            print(f"\n# from {args.secrets}:")
            print(f"YT_CLIENT_ID={inst.get('client_id', '')}")
            print("YT_CLIENT_SECRET=<see client_secret in that file>")
        except OSError:
            pass
    print("\nThen restart the bot. Test with one clip → Approve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


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