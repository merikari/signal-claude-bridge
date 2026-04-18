# signal-claude-bridge

Send a word or phrase to yourself on Signal — get a researched markdown note written to a local folder within seconds.

Built on top of [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) and [Claude Code](https://claude.ai/code). No cloud relay, no HA dependency — just a Docker container and a Python daemon running on your PC.

---

## What it does

You send a Signal message to yourself (Note to Self). The bridge picks it up and routes it one of two ways:

| Message type | Example | What happens |
|---|---|---|
| Short topic (≤ 4 words, no sentence punctuation) | `stoicism` | Claude researches the topic online and writes a short markdown note to your output folder |
| Longer instruction | `summarise the key points of the EU AI Act and save it to my notes` | Claude follows the instruction directly |

You get a one-line Signal reply confirming what was written (or an error if something went wrong).

Notes are tagged `ai-generated` and `signal-bridge` in frontmatter so you can review and promote them later.

---

## Architecture

```
Phone (Signal)
  └─► signal-cli-rest-api  (Docker, 127.0.0.1:8080)
            │
            │  GET /v1/receive  every 3 s
            ▼
      polling daemon  (pythonw.exe, background)
            │
            ├── short topic  ──► prompts/research.md
            └── freeform     ──► prompts/freeform.md
                                        │
                                        ▼
                              claude -p <message>
                              cwd = vault / output folder
                                        │
                                        ▼
                              writes YYYY-MM-DD <slug>.md
                                        │
                                        ▼
                         POST /v2/send  (one-line ack to you)
```

Everything runs locally. No data leaves your machine except through Signal's own E2EE channel and Claude's API.

---

## Requirements

- Windows 10/11 (the task scheduler integration is Windows-specific; the Python daemon itself is cross-platform)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Claude Code](https://claude.ai/code) installed
- A Signal account on your phone

---

## Setup

### 1. Clone and start the container

```powershell
git clone https://github.com/yourname/signal-claude-bridge
cd signal-claude-bridge
docker compose up -d signal-cli-rest-api
```

Wait ~15 s, then verify:

```powershell
docker inspect signal-cli-rest-api --format "{{.State.Health.Status}}"
# expected: healthy
```

### 2. Pair your Signal account

The bridge links to your existing Signal account as a secondary device — no new number needed.

```powershell
# Download and open the QR code
curl.exe "http://127.0.0.1:8080/v1/qrcodelink?device_name=signal-claude-bridge" -o qr.png
start qr.png
```

On your phone: **Signal → Settings → Linked devices → Link new device** → scan the QR.

Verify the pairing:
```powershell
curl.exe "http://127.0.0.1:8080/v1/accounts"
# expected: ["+1234567890"]
```

Full pairing guide (smoke tests, recovery steps) in [PAIRING.md](PAIRING.md).

### 3. Configure

```powershell
copy .env.example .env
```

Open `.env` and fill in at minimum:

| Variable | Description |
|---|---|
| `VAULT_ROOT` | Absolute path to your Obsidian vault or output folder |
| `CLAUDE_BIN` | Full path to `claude.exe` — see note below |
| `SIGNAL_NUMBER` | Your Signal number in E.164 format, e.g. `+1234567890` |
| `ALLOWED_SENDERS` | Same as above (messages from anyone else are dropped) |

**Finding `CLAUDE_BIN`:** Claude's exe is not on PATH by default on Windows.

```powershell
Get-ChildItem "$env:APPDATA\Claude" -Recurse -Filter "claude.exe" | Select-Object FullName
```

Typical result: `C:\Users\<you>\AppData\Roaming\Claude\claude-code\<version>\claude.exe`

> **Note:** After a Claude upgrade the version segment changes. Update `CLAUDE_BIN` in `.env` to match.

### 4. Run manually (first test)

```powershell
.\run.ps1
```

Creates `.venv`, installs dependencies, starts the daemon. You should see:

```
bridge up; signal=+1234567890 vault=C:\... poll=3.0s
```

Send yourself a Note to Self — try a single word like `stoicism`. Within ~30 s a new markdown file should appear in your output folder and you'll get a Signal reply.

### 5. Install as a background service

Once the end-to-end test passes, register the daemon as a Windows scheduled task — no terminal window, auto-starts at logon, restarts on failure:

```powershell
.\install-service.ps1
```

Logs are written to `logs\bridge.log`.

---

## Configuration reference

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `VAULT_ROOT` | *(required)* | Working directory passed to `claude -p` |
| `CLAUDE_BIN` | `claude` | Path to Claude Code CLI executable |
| `CLAUDE_MODEL` | *(CLI default)* | Model for `claude -p`, e.g. `claude-sonnet-4-6` |
| `CLAUDE_TIMEOUT` | `300` | Max seconds to wait for Claude subprocess |
| `SIGNAL_API_URL` | `http://127.0.0.1:8080` | signal-cli-rest-api base URL |
| `SIGNAL_NUMBER` | *(required)* | Your E.164 Signal number |
| `ALLOWED_SENDERS` | `SIGNAL_NUMBER` | Comma-separated allowlist of sender numbers |
| `POLL_INTERVAL` | `3` | Seconds between `/v1/receive` polls |
| `SHORT_TOPIC_MAX_TOKENS` | `4` | Token threshold for research vs freeform mode |

---

## Customising Claude's behaviour

Edit the prompt files — no code changes needed:

- `prompts/research.md` — governs short-topic research notes (structure, length, language matching, source requirements)
- `prompts/freeform.md` — governs longer instructions (output location, tagging, safety guardrails)

Both prompts instruct Claude to return a single `OK: ...` or `FAIL: ...` line on stdout, which is forwarded back to you as the Signal reply.

> **Note on output format:** The included prompts produce [Obsidian](https://obsidian.md)-flavoured markdown — YAML frontmatter, `[[wikilinks]]`, and tags like `ai-generated`. If you use a different tool or just want plain markdown, edit the prompts to match your preferred format. The bridge itself is format-agnostic.

---

## Security

- **Signal E2EE** is end-to-end encrypted end-to-end. Only contacts you've accepted can reach your linked account.
- **Container is loopback-bound** (`127.0.0.1:8080`) — unreachable from the LAN or internet.
- **Sender allowlist** — `ALLOWED_SENDERS` defaults to your own number. Messages from anyone else are logged and dropped before Claude is invoked.
- **No inbound ports** are opened on your router.
- **Image is pinned** to a specific release tag in `docker-compose.yml`. Upgrades are deliberate.

---

## Upgrading the container

Check [releases](https://github.com/bbernhard/signal-cli-rest-api/releases), then:

```powershell
# Edit image: tag in docker-compose.yml, then:
docker compose pull && docker compose up -d
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `400 Bad Request` on `/v1/receive` | Container in `json-rpc` mode | Ensure `MODE: normal` in `docker-compose.yml`, recreate container |
| `FileNotFoundError` on startup | `CLAUDE_BIN` not set or path wrong | Set full path in `.env`; re-run `install-service.ps1` after Claude upgrades |
| Task shows `Ready` not `Running` | pythonw crashed on startup | Check `logs\bridge.log` and `logs\bridge-err.log` |
| Messages received but no note written | Claude failed silently | Signal reply will say `FAIL: ...`; check logs for details |

---

## License

MIT
