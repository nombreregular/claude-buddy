# Claude Buddy

**English** | [Español](README.es.md)

> Unofficial project, not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic.

A touchscreen companion for Claude desktop, built on a Raspberry Pi 4 and a 7" display.

It shows up in Claude desktop as a **Hardware Buddy** over Bluetooth LE. When Claude asks for permission to run a tool, the request appears on the screen and you **approve or deny it with a tap**. At a glance you also see what your Claude sessions and background runs are doing, and a small pet (Mochi) reacts to it all.

It speaks the same protocol as the official M5StickC Plus buddy, but runs on **BlueZ and Python** instead of an ESP32.

![The Buddy screen: the pet, your sessions and the live threads](docs/img/overview.png)

## What you need

- A **Raspberry Pi 4** with **Raspberry Pi OS (64-bit) with desktop** and the built-in Bluetooth. A 2 GB model is enough.
- The official **7" touch display** (DSI, 800x480). Without a display you can still run the buddy and answer with the keyboard.
- **Python 3.11 or newer** (Pi OS Bookworm ships 3.11, Trixie ships 3.13) and **Chromium** (`sudo apt install chromium`).
- **Claude desktop** on a Mac or a Windows PC with **Developer Mode** turned on.
- Optional features that read your sessions (the context hook, the census) run on macOS.

## Quick start (about 10 minutes)

**1. Prepare the Pi.** With Raspberry Pi Imager choose *Raspberry Pi OS (64-bit)* with desktop, set the hostname to `buddy`, create your user, enable SSH and, in the OS customisation, your Wi-Fi. Boot with the display attached and make sure the desktop logs in by itself (`sudo raspi-config nonint do_boot_behaviour B4`). Install Chromium if it is missing.

**2. Copy the project to the Pi and install it.** From your computer:

```bash
rsync -av --exclude .venv --exclude __pycache__ --exclude .pytest_cache ./ <user>@buddy.local:claude-buddy/
```

On the Pi:

```bash
cd claude-buddy
python3 -m venv .venv            # if it complains: sudo apt install python3-venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python buddy.py check  # must end with "BlueZ aceptó todo"; it leaves nothing running
```

**3. Run it and pair.**

```bash
.venv/bin/python buddy.py
```

In Claude desktop: **Help > Troubleshooting > Enable Developer Mode**, then **Developer > Open Hardware Buddy…**, click **Connect** and pick **`Claude-XXXX`** (the four characters are printed in the Pi's terminal). Allow Bluetooth if macOS asks. The Pi shows a **6-digit code**, big on the touchscreen and in the terminal; type it into the dialog on your computer. You pair only once, afterwards it reconnects by itself.

**4. Open the screen.** In another terminal on the Pi (or over SSH):

```bash
./kiosk.sh        # full screen Chromium; Alt+F4 exits. Use ./kiosk.sh --window while you tweak things
```

**5. Make it start with the Pi** (daemon as a user service, screen at login on Pi OS Trixie with labwc):

```bash
mkdir -p ~/.config/systemd/user ~/.config/labwc
cp claude-buddy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-buddy
sudo loginctl enable-linger "$USER"      # start at boot, before anyone logs in
echo "$HOME/claude-buddy/kiosk.sh > $HOME/.cache/claude-buddy-kiosk.log 2>&1 &" >> ~/.config/labwc/autostart
```

Reboot to check: the buddy advertises about 20 seconds after power-on and the screen appears about 10 seconds later. Pi OS starts labwc with `-m`, so that autostart file is added to the system one instead of replacing it. On Bookworm with wayfire the line goes in `~/.config/wayfire.ini`, section `[autostart]` (documented, not tested). Check that the screen never blanks: `sudo raspi-config nonint get_blanking` should print `1`; if not, `sudo raspi-config nonint do_blanking 1`.

If BlueZ only accepts the buddy under `sudo` on your Pi, use the system service version in [docs/architecture.md](docs/architecture.md#running-as-a-system-service).

## What you see and do

![Your Claude threads, grouped like the Claude desktop sidebar](docs/img/threads.png)

**Approve or deny with a tap.** A permission request from Claude desktop shows the tool, the text of the request and two big buttons, **Deny** and **Approve once**. The buttons arm 0.6 seconds after the request appears, so a finger that was already resting on the screen cannot approve anything, and two quick taps send one answer. If you answer somewhere else (your computer, the keyboard) the request disappears by itself. The keyboard works too: `a` approves, `d` denies.

![A permission request waiting for a tap](docs/img/ask.png)

**Threads.** With the context hook installed (optional, below), the speech bubble lists which Claude conversations are alive, each with its real title and grouped like the Claude desktop sidebar. Tap a row to read what it last said. See [docs/features/threads-and-context-hook.md](docs/features/threads-and-context-hook.md).

**Runs.** Headless `claude -p` runs and the scripts you name appear in a **Runs** group ("Corridas" in the Spanish interface) with how long each has been running. Optional, see [docs/features/census.md](docs/features/census.md).

**Notices.** A script or an assistant can leave a short message on the screen with `tools/notify.py`; you dismiss it with a tap. See [docs/features/notices.md](docs/features/notices.md).

![A notice waiting at the top of the list](docs/img/notices.png)

**Night mode.** From 23:00 to 07:00 (your computer's clock) the screen dims by lowering the real backlight. A tap wakes it for 30 seconds, and a permission request wakes it by itself. On by default; `--night off` disables it. See [docs/features/night-mode.md](docs/features/night-mode.md).

![The screen at night, dimmed](docs/img/night.png)

**A pet with moods.** Mochi sleeps, idles, works, needs you, celebrates, gets dizzy and gets happy, and levels up every 50,000 output tokens. Tap it for a smile; five fast taps make it dizzy. Packs sent from Claude desktop can replace it. See [docs/features/pets-and-packs.md](docs/features/pets-and-packs.md).

| Screen | When |
|---|---|
| **Bluetooth is not ready** | bluetoothd stopped, rfkill, no adapter. Says why and retries |
| **Pairing** | 6 big digits while the computer asks for the code |
| **Claude asks for permission** | tool, request text, **Deny** / **Approve once** (and **Always** for Claude Code in a terminal). `+1 in queue` if more are waiting |
| **Connected** | the pet with its mood, sessions / working / waiting, tokens today, level, live threads, runs, notices |
| **Waiting for Claude desktop** | nobody connected; reminds you of the `Claude-XXXX` name |

Keys on the Pi's terminal: `a` approve, `d` deny, `s` status, `u` forget pairings (confirm with `y`), `q` quit. With several requests queued, `a` and `d` answer the oldest first.

## Optional features

Everything in this section is optional. Without any of it you still get pairing, permission approvals, the pet and night mode.

### The local API (needed by the hooks, the census, notices and Hermes)

Everything that comes from outside Claude desktop reaches the Pi through a small HTTP API on port 8766, with its own token. It is **off until you start the daemon with `--hermes-bind`** (the option names still say "hermes" for historical reasons). It can queue approval requests and show information, but it can never approve anything: only a tap on the screen or a key on the Pi does. Details in [docs/local-api.md](docs/local-api.md).

```bash
# on the Pi: a token only your user can read, at least 16 characters
mkdir -p ~/.config/claude-buddy
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/claude-buddy/hermes.token)

# start with the API on the Pi's Tailscale IP (or 0.0.0.0 for the whole LAN, not recommended)
.venv/bin/python buddy.py --hermes-bind <tailscale-ip> --hermes-token-file ~/.config/claude-buddy/hermes.token
```

As a service, add those two options to the `ExecStart` of `claude-buddy.service`. On the computer that runs Claude, copy the same token without it passing through the screen:

```bash
mkdir -p ~/.config/claude-buddy
(umask 077; ssh <user>@buddy.local cat .config/claude-buddy/hermes.token > ~/.config/claude-buddy/hermes.token)
```

The clients use `http://buddy:8766` by default (the Pi's name on Tailscale). If the API listens on your LAN, set `CLAUDE_BUDDY_URL=http://buddy.local:8766`.

### Context hook: which conversations are alive

A Claude Code hook that tells the buddy what each session is doing. Standard library only, runs on the Python 3.9 that ships with macOS. Add it to `~/.claude/settings.json` inside `"hooks"` (if you already have those keys, add the hook to each one):

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/context_hook.py\" 2>/dev/null; exit 0", "timeout": 5}]}
    ]
  }
}
```

Keep the `2>/dev/null; exit 0` at the end of each command. If the script file ever disappears (a moved folder, a restored backup), Python exits with code 2 and Claude Code reads that as "block", which would deny every tool in every session. The script itself always exits 0, but only the command line covers a script that never starts.

The hook never slows a session down: it exits silently whatever happens, gives up after 1.5 s to connect and 2 s to wait for a reply, and a watchdog ends it after 2.5 s no matter what the network does. If the buddy does not answer it is left alone for 30 seconds. Turn it off with `CLAUDE_BUDDY_CONTEXT=off`. What travels to the Pi and every privacy switch: [docs/features/threads-and-context-hook.md](docs/features/threads-and-context-hook.md).

### Approvals for Claude Code in a terminal

`tools/cli_hook.py` is a `PermissionRequest` hook: the same screen answers the permission prompts of Claude Code in a terminal (shown as `cli · Bash`). Only a tap decides; anything else (Pi off, no token, nobody taps in 60 s) falls back to the normal terminal prompt, which stays available the whole time. When Claude Code proposes an "always allow" rule, a third button, **Always**, shows the whole rule and saves it exactly as the terminal would. Configuration: [docs/features/hermes.md](docs/features/hermes.md#claude-code-in-a-terminal-cli-hook).

### Census of background runs

`tools/census_agent.sh install` on macOS installs a LaunchAgent that looks at the process table every 15 seconds and shows headless `claude -p` runs (and scripts you name in `census.json`) in the Runs group. It sends names and durations only, never command lines. [docs/features/census.md](docs/features/census.md).

### Notices from scripts or an assistant

```bash
python3 tools/notify.py --title 'The batch finished' --body '37 of 40 passed' --kind ok --ttl 600 --id batch-21 --wake
```

Shows up as a **Notices** group at the top of the list, closes with a tap, expires by itself and, with `--wake`, wakes the dimmed screen. [docs/features/notices.md](docs/features/notices.md).

### Costume closet for the pet

Off by default. `python3 tools/make_closet.py ~/closet --count 8` (needs Pillow) draws costumes (party hats, crown, bow, flower, cat ears), you copy them to `<state-dir>/closet` on the Pi, and `--closet auto` lets the pet wear one at random. [docs/features/closet.md](docs/features/closet.md).

### Hermes Agent

If you use the [Hermes Agent](docs/features/hermes.md), it can use this screen as the place where its dangerous-command approvals are answered (a plugin in `hermes-plugin/claude-buddy/`), and it can leave notices through a skill (`hermes-plugin/skills/buddy-pantalla/`). Never approves by itself. [docs/features/hermes.md](docs/features/hermes.md).

### Other language

The screen comes in Spanish and English: `--lang es|en` (the terminal and the log stay in Spanish). Adding a language is a table in the page and a table in the daemon: [docs/features/i18n.md](docs/features/i18n.md).

## Configuration

Commands: `buddy.py` (run), `buddy.py check` (diagnosis and a registration rehearsal with BlueZ), `buddy.py forget-all` (delete the pairings made with the buddy; devices that never talked to it, such as a Bluetooth keyboard, are not touched).

| Option | What it does |
|---|---|
| `--lang es\|en` | Language of the screen, default `es`; without the option the `BUDDY_LANG` variable is read. The terminal and the log stay in Spanish |
| `--state-dir PATH` | Where to keep data (default `~/.local/state/claude-buddy`): `state.json`, `peers.json`, `characters/`, `closet/` |
| `--adapter hci1` | Use another Bluetooth adapter (default `hci0`), for example a USB dongle |
| `--adv-mode kernel` | Last resort if the computer cannot see `Claude-XXXX`: the kernel sets the name but **cuts it to 10 characters** (`Claude-345`) |
| `--no-keys` | Do not read the keyboard (for services) |
| `--ui-port 8765` | Port of the touch screen server (always on `127.0.0.1`) |
| `--no-ui` | No touch screen, terminal only |
| `--night HH:MM-HH:MM\|off` | Night mode window, default `23:00-07:00`; `off` also stops the daemon from touching the backlight |
| `--night-level N` | Backlight at night, default 12 of 255 (never below 4) |
| `--backlight auto\|off\|PATH` | The panel backlight night mode moves; `off` dims with a dark veil on the page instead |
| `--closet off\|auto\|PATH` | Costume closet; default `off` |
| `--closet-every HOURS` | How often the pet picks another costume, default 3 (0.25 to 168; 0 picks one at start) |
| `--hermes-bind IP` | Turns the local API on, listening on this IP (Tailscale IP, or `0.0.0.0`). Off without it |
| `--hermes-port 8766` | Port of the local API |
| `--hermes-token-file PATH` | File with the API token; otherwise the `BUDDY_HERMES_TOKEN` variable. At least 16 characters, never printed |
| `-v` / `-vv` | More logs |
| `--log-file buddy.log` | Send logs to a file; attach it to a bug report |
| `--insecure` | **Tests only.** No pairing and no encryption: the text of your sessions travels in the clear |

Environment variables of the clients (`context_hook.py`, `cli_hook.py`, `census.py`, `notify.py`): `CLAUDE_BUDDY_URL` (default `http://buddy:8766`) and `CLAUDE_BUDDY_TOKEN_FILE` (default `~/.config/claude-buddy/hermes.token`, it must be a regular file, yours, mode 0600). The rest are listed with each feature.

## Troubleshooting

The terminal and the log of the daemon are in Spanish whatever the language of the screen, so its messages are quoted below as it prints them. `buddy.py check` prints the diagnosis and ends with `[ERROR]` lines when something blocks the buddy.

| Symptom | Likely cause | What to do |
|---|---|---|
| `bluetoothd no está corriendo` | Service is off | `sudo systemctl enable --now bluetooth` |
| `D-Bus no me deja hablar con BlueZ` | Your system's D-Bus policy is stricter than the usual one | Run with sudo: `sudo .venv/bin/python buddy.py --state-dir ~/.local/state/claude-buddy`. On old systems `sudo usermod -aG bluetooth $USER` and log in again also works |
| `bloqueado por software` | rfkill | `sudo rfkill unblock bluetooth` |
| `no encuentro el adaptador hci0` | No Bluetooth or a badly seated dongle | `bluetoothctl list` shows what exists |
| The computer cannot see `Claude-XXXX` | The name is not received, or Bluetooth permission is closed | Check with a phone first (next row): if it appears there, the Pi advertises fine. Then Developer Mode and the Bluetooth permission of your computer. Last resort: `--adv-mode kernel` |
| You want to confirm the advertisement without the computer | | **nRF Connect** or **LightBlue** on a phone: `Claude-XXXX` with service `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| The code appears but pairing fails | An old pairing is left on one side | Use **Forget** in the Hardware Buddy window. If it persists, `buddy.py forget-all` on the Pi and remove `Claude-XXXX` from your computer's Bluetooth settings |
| It connects and drops after ~30 s | The computer does not receive our notifications | Run with `-vv --log-file buddy.log` and look at the log |
| The screen says "Sleeping" after restarting the daemon | The computer stayed connected but never reopened the channel | The daemon disconnects a silent buddy device after 20 s and the computer reconnects by itself (up to 3 tries). By hand: `bluetoothctl disconnect <address>` |
| `kiosk.sh`: Chromium not found | Not installed | `sudo apt install chromium` |
| `kiosk.sh`: cannot find the Pi's screen | The desktop is not started, or you run it without a display | Log in to the desktop (or enable autologin) |
| `kiosk.sh`: the daemon does not answer | `buddy.py` is not running or uses another port | Start it; with another port: `BUDDY_PORT=9000 ./kiosk.sh` |
| Cannot open the screen on port 8765 | Another buddy (or something) is on that port | `--ui-port 8766` and `BUDDY_PORT=8766 ./kiosk.sh`, or stop the other one. If you run `buddy.py` by hand, `systemctl --user stop claude-buddy` first, the service restarts it within 5 s |
| The screen says "Sin conexión con el buddy" | The daemon died (the service restarts it in 5 s) | `systemctl --user status claude-buddy` and `journalctl --user-unit claude-buddy -n 50` |
| Touch does not respond | Display not calibrated, or the request had just appeared | Wait half a second; `libinput list-devices` shows whether the touch device exists |
| A hook logs `Errno 65` (`No route to host`) with the Pi on your LAN | macOS has not granted **Local Network** to your terminal | System Settings > Privacy & Security > Local Network. A `tmux` server started earlier keeps the old permission: `tmux kill-server` and reopen. Over Tailscale (100.x) it does not happen |

On Pi OS Trixie `journalctl --user -u claude-buddy` may say `No journal files were found` (the user journal is not persistent); `journalctl --user-unit claude-buddy` works. To exit the kiosk from SSH: `pkill -f "[c]laude-buddy-kiosk"` (the bracket stops the command from matching itself). To see the raw Bluetooth traffic: `sudo btmon`.

To look at the screen from another computer (it only listens inside the Pi, on purpose): `ssh -L 8765:localhost:8765 <user>@buddy.local` and open `http://localhost:8765`. If the stream stalls through the tunnel, add `?poll=500`. To work on the design without a daemon, open `ui/index.html` in a browser with `?demo=cycle`; the list of demos is in the file (`DEMOS`).

## Security

- Pairing uses a code that only appears on the Pi, so every new device needs it. Bluetooth carries summaries of your sessions and the text of permission requests (for example the command Claude wants to run). `--insecure` removes all of that.
- **The screen can approve commands**, so it listens only on `127.0.0.1`, every start generates a token that lives inside the page and that `POST /decide` requires, requests with another `Host` or `Origin` (DNS rebinding, other sites' pages) and plain HTML forms are refused, and the page never turns received text into HTML.
- The local API faces your network, so it has its own token and cannot approve anything. Prefer the Tailscale IP to `0.0.0.0`. With the context hook, the titles and groups of your conversations and up to 300 characters of the last reply travel in plain HTTP; that text leaves your computer only towards itself or the tailnet and can be turned off. Everything is listed in [docs/local-api.md](docs/local-api.md) and [docs/features/threads-and-context-hook.md](docs/features/threads-and-context-hook.md).

## Status and what is not verified

Everything is tested against a simulated BlueZ (about 2,000 tests; the BlueZ ones need `dbus-daemon`, the page tests drive a real headless Chromium with simulated touches). On real hardware (Pi 4, Pi OS Trixie, labwc, Claude desktop on a Mac) these were verified: pairing, permission approvals by touch, the kiosk starting by itself after a reboot, a character pack pushed over BLE, Hermes approvals through Tailscale, notices and the costume closet. The first run on your Pi is the real test. What can still fail, in order: the full `Claude-XXXX` name in the advertisement (plan B: `--adv-mode kernel`), passkey pairing against your OS, the advertisement resuming after a disconnect, notification size according to the negotiated MTU, kiosk on other Pi OS versions, and the finger-level feel of the thread list (drag against tap) on the real display. The detailed list is in [CLAUDE.md](CLAUDE.md). If something fails, attach the output of `buddy.py check` and the file from `buddy.py -vv --log-file buddy.log` to your report.

## Project layout

| Path | What it is |
|---|---|
| `buddy.py` | Entry point: terminal, keys, `check`, `forget-all` |
| `buddy_core.py` | The protocol (standard library only): line-based JSON, heartbeats, approvals, state, character transfer, the thread board |
| `buddy_bluez.py` | Bluetooth over BlueZ's D-Bus API: GATT server (Nordic UART), advertisement, passkey agent |
| `buddy_ui.py` | The screen: view model plus the local HTTP server and the shared HTTP base |
| `buddy_hermes.py` | The local API on port 8766 (still called the Hermes listener in the code) |
| `buddy_backlight.py`, `buddy_closet.py` | Night mode backlight; costume closet |
| `ui/index.html` | The touch page: one file, no dependencies, fixed 800x480 stage |
| `kiosk.sh`, `claude-buddy.service` | Chromium launcher; user service template |
| `tools/` | `context_hook.py`, `cli_hook.py`, `census.py`, `census_agent.sh`, `notify.py`, `make_closet.py`, `make_pixel_buddy.py` |
| `hermes-plugin/` | The Hermes Agent plugin and skill (optional) |
| `characters/buddy-pixel/` | The Mochi pack |
| `tests/` | About 2,000 tests, see [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) |
| `docs/` | Architecture, protocol, local API and one page per feature (also in [Spanish](docs/es/)) |
| `CLAUDE.md`, `PRODUCT.md`, `DESIGN.md` | Contributor and agent guide, product brief, visual design system |

## Documentation

- [Architecture](docs/architecture.md): processes, ports, data flow
- [Hardware Buddy protocol](docs/protocol.md): the BLE protocol as implemented
- [Local API](docs/local-api.md): `/approval`, `/session`, `/census`, `/notice` and the screen server
- Features: [threads and the context hook](docs/features/threads-and-context-hook.md), [census](docs/features/census.md), [notices](docs/features/notices.md), [night mode](docs/features/night-mode.md), [closet](docs/features/closet.md), [Hermes](docs/features/hermes.md), [pets and packs](docs/features/pets-and-packs.md), [languages](docs/features/i18n.md)
- [Contributing](docs/CONTRIBUTING.md)

## Support the project

Claude Buddy is free and MIT licensed. If it is useful to you, you can sponsor the work through the **Sponsor** button of this repository or with a coffee on [Ko-fi](https://ko-fi.com/nombreregular). It also helps to tell us what you would like next: a ready-made kit, new pet costumes, or a version that approves actions for other AI agents. Open an issue and say so.

## License

[MIT](LICENSE). Copyright (c) 2026 nombreregular.

## Sources

- [Hardware Buddy REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md) and the [claude-desktop-buddy firmware](https://github.com/anthropics/claude-desktop-buddy)
- BlueZ: [GattCharacteristic](https://manpages.debian.org/trixie/bluez/org.bluez.GattCharacteristic.5.en.html), [LEAdvertisement](https://manpages.debian.org/trixie/bluez/org.bluez.LEAdvertisement.5.en.html), [LEAdvertisingManager](https://man.archlinux.org/man/org.bluez.LEAdvertisingManager.5.en), [Agent API](https://bluez.readthedocs.io/en/latest/agent-api/)
- [Raspberry Pi: Chromium kiosk tutorial with labwc](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/)
- [MDN: Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events), [Screen Wake Lock API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API)
