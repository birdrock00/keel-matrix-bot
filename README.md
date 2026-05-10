# Keel Matrix Bot

Matrix bot for viewing and approving pending [Keel](https://keel.sh/) approvals.

## Build

```sh
docker build -t keel-matrix-bot:local .
```

## Run

```sh
docker run --rm \
  --name keel-matrix-bot \
  -p 8080:8080 \
  -v keel-matrix-bot-memory:/mem \
  -e MATRIX_HOMESERVER="https://matrix.example.com" \
  -e KEEL_MATRIX_ROOM_ID="!roomid:matrix.example.com" \
  -e KEEL_URL="https://keel.example.com" \
  -e KEEL_USERNAME="keel-basic-auth-user" \
  -e KEEL_PASSWORD="keel-basic-auth-password" \
  -e MATRIX_USERNAME="keelbot" \
  -e MATRIX_PASSWORD="matrix-password" \
  -e KEEL_MATRIX_BOT_PUBLIC_URL="https://keel-matrix-bot.example.com" \
  -e KEEL_MATRIX_BOT_STATE_FILE="/mem/keel_matrix_bot_state.json" \
  -e KEEL_MATRIX_BOT_HTTP_PORT="8080" \
  ghcr.io/birdrock00/keel-matrix-bot:latest
```

The `/mem` volume stores persistent bot state, including release-note URLs and
already-seen approvals.
