# Architecture

Claude Buddy is one Python daemon on the Pi, a Chromium window that draws its screen, and a few small clients on the computer that runs Claude. Everything the daemon needs is in the Python standard library except `dbus-fast`, which the Bluetooth transport uses.

## Processes

| Where | Process | What it does |
|---|---|---|
| Pi | `buddy.py` (one asyncio process) | Speaks BLE with Claude desktop, keeps the state, serves the screen and the local API |
| Pi | Chromium, started by `kiosk.sh` | Draws `ui/index.html` full screen from `http://127.0.0.1:8765` |
| Your computer | Claude desktop | The Hardware Buddy side of the BLE protocol |
| Your computer | `tools/context_hook.py` (optional) | One short run per Claude Code event; tells the Pi what each session does |
| Your computer | `tools/cli_hook.py` (optional) | Asks for permission on the screen for Claude Code in a terminal |
| Your computer | `tools/census.py` via a LaunchAgent (optional) | Looks at the process table, reports background runs |
| Anywhere | `tools/notify.py` (optional) | Leaves a notice on the screen |
| Hermes host | `hermes-plugin/claude-buddy` (optional) | Hermes Agent's approvals answered on the screen |

## Modules inside the daemon

- `buddy_core.py`: the protocol, line framing, heartbeats, the `ApprovalBroker` (one queue of pending approvals, whatever their source), the persistent state, character pack receiving, the `TaskBoard` (live threads and runs) and the `NoticeBoard`.
- `buddy_bluez.py`: GATT server, advertisement, passkey agent and a watchdog for a computer that connects but never opens the channel. It never touches the protocol.
- `buddy_ui.py`: `UIHub` (the view model: what the screen shows, computed from the core, the broker and the radio, plus night mode and the closet) and `UIServer`, the loopback HTTP server. It also holds `HttpServer`, the strict HTTP/1.1 base the local API shares.
- `buddy_hermes.py`: the local API server (`HermesServer`).
- `buddy_backlight.py`, `buddy_closet.py`: the panel backlight for night mode and the costume closet.

## Ports

| Port | Bind | Purpose | Auth |
|---|---|---|---|
| 8765 (`--ui-port`) | `127.0.0.1` only | The screen: page, server-sent events, `POST /decide`, `/wake`, `/dismiss` | Token baked into the page at each start, plus `Host`/`Origin` checks |
| 8766 (`--hermes-port`) | `--hermes-bind IP`; off if not given | The local API (`/approval`, `/session`, `/census`, `/notice`) | `Authorization: Bearer` with its own token |

The two ports never share routes: nothing on 8766 can approve anything, and `/approval` does not exist on 8765. The routes are in [local-api.md](local-api.md).

## Data flow

```
Claude desktop ──BLE──> buddy_bluez ──> BuddyCore ──> ApprovalBroker ──> UIHub ──SSE──> Chromium page
   (heartbeats,            (GATT)         (protocol)      (one queue)      (view model)        │
    prompts)                                                  ▲                                 │ tap
        ▲                                                     │ POST /approval (hermes, cli)    ▼
        └──── {"cmd":"permission","decision":...} <── decide ◄───────────────────── POST /decide (127.0.0.1)

context_hook / census / notify ──HTTP, token──> local API ──> TaskBoard / NoticeBoard ──> UIHub ──> page
```

1. **Permissions from Claude desktop.** A heartbeat carries a `prompt` (id, tool, hint). The core puts it in the broker as an approval with source `claude`. The page shows it; a tap calls `POST /decide`; the broker hands the decision to the responder, which sends `{"cmd":"permission","id":...,"decision":"once"|"deny"}` over BLE.
2. **Permissions from other sources.** `POST /approval` on the local API queues an approval with source `hermes` or `cli` in the same broker; the caller learns the answer by long polling. Terminal keys (`a`, `d`) and the screen answer the same queue, oldest first, and every answer returns only to the source that asked.
3. **Information, never decisions.** Threads (`POST /session`), runs (`POST /census`) and notices (`POST /notice`) go into boards kept in memory. The hub adds them to the view (`view.tasks`, `view.notices`) and the page redraws. Restarting the daemon empties the boards until the senders speak again.
4. **The page** receives the whole view model as server-sent events (or by polling `GET /state` every 500 ms when the page URL has `?poll=500`), draws it with `textContent` only, and sends back only taps.

## State on disk

`<state-dir>` (default `~/.local/state/claude-buddy`): `state.json` (name, owner, level, approvals, denials, tokens), `peers.json` (which Bluetooth devices belong to the buddy), `characters/` (installed packs), `closet/` (costumes, if you use them). The daemon never writes what your sessions say to disk or to any log.

On your computer the clients keep `~/.config/claude-buddy/hermes.token` (the API token, mode 0600) and optionally `census.json`; the context hook keeps a small cache in `~/.cache/claude-buddy/`.

## Running as a system service

The normal way is the user service in `claude-buddy.service` (see the README). If BlueZ only accepts the buddy under `sudo` on your Pi, use a system unit instead:

```ini
# /etc/systemd/system/claude-buddy.service
[Unit]
Description=Claude Buddy (BLE and touch screen)
After=bluetooth.service
Wants=bluetooth.service

[Service]
ExecStart=/home/<user>/claude-buddy/.venv/bin/python /home/<user>/claude-buddy/buddy.py --no-keys --state-dir /home/<user>/.local/state/claude-buddy
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now claude-buddy
journalctl -u claude-buddy -f
```

## Design rules that shape the code

- `POST /decide` approves commands: loopback only, per-start token, `Host`/`Origin` checks, JSON content type required. Any change there comes with a test.
- The local API can queue and report but never decide. It opens no connection towards a URL that arrives in a request body (no callbacks).
- Text that comes from outside (a request, a thread title, a notice) is only ever written with `textContent`; the page has a CSP of `default-src 'none'`.
- The approve buttons arm 600 ms after a request appears. Nothing else in the page reads or writes that timer.
- Animations use only `transform` and `opacity` (Pi 4).
