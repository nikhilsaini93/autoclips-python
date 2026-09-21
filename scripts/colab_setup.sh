#!/usr/bin/env bash
# One-shot setup for a fresh Colab VM.
#
# Usage (from the project folder, in the SAME shell you run the app from):
#   source scripts/colab_setup.sh          # setup only
#   source scripts/colab_setup.sh --run    # setup, then start uvicorn
#
# Must be `source`d (not `bash script.sh`) so PATH / env vars stay in your shell.
# Do NOT add `set -e` or `exit` here: when sourced they would kill your shell.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
[ -d "$PROJECT_DIR/app" ] || PROJECT_DIR="/content/autoclips-python"
cd "$PROJECT_DIR" || return 1
COOKIES="$PROJECT_DIR/cookies.txt"

echo "==> Project: $PROJECT_DIR"

# 0) System packages (skipped when everything is already installed)
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 || ! command -v nano >/dev/null 2>&1; then
  echo "==> Installing system packages (ffmpeg, git, nano)..."
  apt update -qq && apt install -y -qq ffmpeg git nano >/dev/null 2>&1
fi

# 1) Deno: JS runtime yt-dlp needs to solve YouTube challenges
if [ ! -x "$HOME/.deno/bin/deno" ]; then
  echo "==> Installing Deno..."
  curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1
fi
export PATH="$HOME/.deno/bin:$PATH"

# 2) yt-dlp + challenge-solver scripts
echo "==> Installing/updating yt-dlp[default]..."
pip install -q -U "yt-dlp[default]" >/dev/null 2>&1

# 3) Python deps of the project (skip quietly if no requirements.txt)
if [ -f requirements.txt ]; then
  echo "==> Installing requirements.txt..."
  pip install -q -r requirements.txt >/dev/null 2>&1
fi

# 4) Cookies: clean a pasted/edited file, then verify it has login cookies
if [ -f "$COOKIES" ]; then
  cp "$COOKIES" "$COOKIES.bak"
  # keep only valid 7-column, tab-separated cookie lines; rebuild the header
  { echo "# Netscape HTTP Cookie File"; awk -F'\t' 'NF==7' "$COOKIES.bak"; } > "$COOKIES"
  n_all=$(awk -F'\t' 'NF==7' "$COOKIES" | wc -l)
  n_login=$(grep -cE "LOGIN_INFO|SAPISID|__Secure-[13]PSID" "$COOKIES")
  export YOUTUBE_COOKIES_FILE="$COOKIES"
  if [ "$n_all" -eq 0 ]; then
    echo "!! cookies.txt has 0 valid lines (tabs lost?). Drag the original file into the Files panel."
  elif [ "$n_login" -eq 0 ]; then
    echo "!! cookies.txt has no LOGIN cookies (exported while logged out?). Re-export from a logged-in incognito window."
  else
    echo "==> cookies: OK ($n_all cookies, $n_login login cookies)"
  fi
else
  unset YOUTUBE_COOKIES_FILE
  echo "!! cookies.txt not found. Drag it into $PROJECT_DIR via the Files panel, then re-run this script."
fi

# 5) Remove leftovers from failed downloads (partial *.part / *.fNNN.mp4 files)
if [ -d storage/downloads ]; then
  find storage/downloads -type f \( -name "*.part" -o -name "*.ytdl" -o -name "*.f[0-9]*.*" \) -delete 2>/dev/null
fi

# 6) Summary
echo "==> deno:   $(deno --version 2>/dev/null | head -1)"
echo "==> yt-dlp: $(yt-dlp --version 2>/dev/null)"
echo "==> YOUTUBE_COOKIES_FILE=${YOUTUBE_COOKIES_FILE:-<not set>}"

# 7) Optional: start the app
if [ "$1" = "--run" ]; then
  echo "==> Starting uvicorn..."
  uvicorn app.main:app --reload --port 8000
else
  echo "==> Ready. Start the app with:  uvicorn app.main:app --reload --port 8000"
fi