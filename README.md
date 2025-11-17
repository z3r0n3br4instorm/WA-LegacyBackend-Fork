# WhatsAppX Matrix Bridge Backend

This repository now ships a Python backend that exposes the same HTTP + TCP APIs consumed by the classic WhatsAppX iOS client, but proxies every action through a Matrix account (tested with Beeper). The legacy Node/WhatsApp Web implementation is kept for reference, yet the Python bridge is the supported path.

## Prerequisites

- Python 3.11+
- `ffmpeg` available on `$PATH` (used for audio/video conversion)
- Matrix account with an access token (Beeper works great)

## Configuration

1. Copy the sample Matrix config and edit it with your credentials:

   ```bash
   cp matrix.config.example.json matrix.config.json
   $EDITOR matrix.config.json
   ```

   Required fields:
   - `homeserver`: e.g. `https://matrix-client.beeper.com`
   - `user_id`: your full Matrix ID (`@user:domain`)
   - `device_id`: device label used for syncing
   - `access_token`: access token issued for the account
   - `store_path`, `media_cache_dir`: directories where the bridge can persist state

2. Adjust `config.json` if you need to change the TCP or HTTP listen ports. The default tokens match the iOS client expectations—only change them if you also update the client.

## Installing dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running the bridge

```bash
source .venv/bin/activate
python -m python_server.main
```

This launches:

- the Matrix sync worker
- the TCP notification bridge on `HOST:PORT` from `config.json`
- the FastAPI HTTP server on `HOST:HTTP_PORT`

Point the WhatsAppX client at the HTTP server (`wspl-b-address`) and TCP port (`wspl-a-port`). `/qr` will immediately return `Success` once the Matrix connection is ready, so setup can finish without scanning anything.

## Endpoints & compatibility

All original routes implemented by the Node server are provided (`/getChats`, `/getChatMessages`, `/sendMessage`, media download routes, status/mute/block management, etc.). TCP notifications mirror the previous format (`NEW_MESSAGE_NOTI`, `ACK_MESSAGE`, `CONTACT_CHANGE_STATE`, `REVOKE_MESSAGE`). The bridge keeps WA-shaped payloads so the legacy iOS UI remains untouched.

### Media handling

- Audio is transcoded to MP3 for legacy playback (`/getAudioData`)
- Videos are transcoded to QuickTime (`/getMediaData`) and thumbnails generated with FFmpeg
- Voice notes uploaded from the client are converted to Opus + Matrix voice messages

### Notes & limitations

- Broadcast lists are returned empty because Matrix does not expose an equivalent feature.
- Blocking/unblocking uses the Matrix ignored user list.
- Deleting a chat only clears cached history; it will not leave/forget rooms unless you use the dedicated `/leaveGroup` endpoint.

## Development tips

- Logs are emitted via the standard Python logger; set `LOGLEVEL=DEBUG` for verbose traces.
- `python_server/storage` is ignored by Git so you can safely persist Matrix sync state locally.
