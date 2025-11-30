#!/usr/bin/env bash
set -euo pipefail

# Configurable: change if 3000 is busy
PORT="${PORT:-3000}"
AVATAR_BASE="${AVATAR_BASE:-http://localhost:$PORT}"
VOICE="${OB_VOICE:-onyx}"
TTS_ATEMPO="${OB_TTS_ATEMPO:-}"   # optional speed-up, e.g., 1.15

# Start Node bridge
cd "$(dirname "$0")/proj_patched"
PORT="$PORT" node server.js >/tmp/orderbuddy-node.log 2>&1 &
NODE_PID=$!

# Prompt to ensure the web page is loaded and unmuted before Python sends the greeting
echo "Open $AVATAR_BASE in your browser, click once to allow audio, then press Enter to start Python..."
read -r _

# Return to repo root and run Python (pointing to the bridge)
cd ..
AVATAR_BASE="$AVATAR_BASE" OB_VOICE="$VOICE" OB_TTS_ATEMPO="$TTS_ATEMPO" \
python3 orderbuddy_talk_2.py --stt-model small --device auto --voice "$VOICE"

# Clean up Node when Python exits
kill "$NODE_PID" 2>/dev/null || true
