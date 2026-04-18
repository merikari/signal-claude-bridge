# Pairing Signal as a secondary device

One-time setup. After this, `signal-cli-rest-api` can receive and send messages as your account without a new phone number.

## 1. Start the container

```powershell
cd D:\Projects\signal-claude-bridge
docker compose up -d signal-cli-rest-api
docker compose logs -f signal-cli-rest-api
```

Wait until you see `Started Signal-Cli JSON-RPC Daemon`. Ctrl-C out of the log tail (the container keeps running).

## 2. Request a linking QR

Ask the API for a linking URI. The QR image is returned as a PNG.

```powershell
# Save the QR to a file you can open in Windows
curl.exe "http://127.0.0.1:8080/v1/qrcodelink?device_name=signal-claude-bridge" -o qr.png
start qr.png
```

The QR stays valid for ~5 minutes.

## 3. Scan from your phone

On your phone's Signal:

1. Settings → **Linked devices**
2. Tap **+** (iOS) or **Link a new device** (Android)
3. Scan the `qr.png` on your screen
4. Confirm the device name (`signal-claude-bridge`) and approve

The phone will push your identity keys to the container. Linking takes 10-30 s.

## 4. Capture your own number

You'll need this in `.env`. It's the E.164 number of your main Signal account (e.g. `+3585012345678`).

Verify the container knows about it:

```powershell
curl.exe "http://127.0.0.1:8080/v1/accounts"
```

Expected response: a JSON array containing your number.

## 5. Smoke test — send yourself a message

```powershell
curl.exe -X POST "http://127.0.0.1:8080/v2/send" `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"hello from signal-cli-rest-api\",\"number\":\"+358XXXXXXXX\",\"recipients\":[\"+358XXXXXXXX\"]}"
```

(Replace both numbers with your own.) You should see the message on your phone within a few seconds.

## 6. Set CLAUDE_BIN in .env

The `claude` executable is not on PATH by default. Find it:

```powershell
Get-ChildItem "$env:APPDATA\Claude" -Recurse -Filter "claude.exe" | Select-Object FullName
```

Add the result to `.env`:
```
CLAUDE_BIN=C:\Users\<you>\AppData\Roaming\Claude\claude-code\<version>\claude.exe
```

After a Claude upgrade, the version segment in the path changes — update `.env` to match.

## 7. Smoke test — receive

Send yourself a message **from** your phone (to yourself; Signal calls this "Note to Self"). Then:

```powershell
curl.exe "http://127.0.0.1:8080/v1/receive/+358XXXXXXXX"
```

Expected: JSON with your message payload under `envelope.dataMessage.message`.

> **Note:** If this returns `{"error":"websocket: ..."}`, the container is in
> `json-rpc` mode. Ensure `docker-compose.yml` has `MODE: normal` (not `json-rpc`),
> then `docker compose up -d` to recreate.

## Recovery

- **Unlink**: Signal app → Settings → Linked devices → remove `signal-claude-bridge`.
- **Re-link**: delete the docker volume (`docker compose down -v`), then repeat from step 1. Messages queued while unlinked are not replayed.
- **Upgrade image**: edit the `image:` tag in `docker-compose.yml` after reviewing [release notes](https://github.com/bbernhard/signal-cli-rest-api/releases), then `docker compose pull && docker compose up -d`.
