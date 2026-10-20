# Contributing

Thanks for helping. A short guide to getting the code running and what we ask of a change. `CLAUDE.md` (in Spanish) is the long reference for contributors and coding agents: facts verified on hardware, contracts and non-negotiable rules.

## Run the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

About 2,000 tests. They need no hardware: the Bluetooth ones talk to a fake `bluetoothd` on a private D-Bus.

- **On the Pi** (Linux with `dbus-daemon`, which Pi OS ships) the whole suite runs. Do not run it at night: the tests that start the real daemon would dim the real backlight for a while (it is restored at exit).
- **On a Mac** the BlueZ tests and the ones that need Linux or D-Bus (`test_cli`, two in `test_hermes`) are skipped, about 80 in all. `test_kiosk` has one test that fails there only because macOS temporary paths are too long for a Unix socket.
- **Page tests** (`test_ui`, `test_page_*`, `test_closet_e2e`) drive the page in a headless **Chromium** and are skipped if there is none. `tests/page_runner.py` looks for `chromium` on `PATH`, Google Chrome in `/Applications`, or the binary in `CLAUDE_BUDDY_CHROMIUM`. A page test can be flaky under load: rerun it alone before blaming your change.
- `test_pixel_buddy`, `test_closet_tool` and the closet end-to-end test need Pillow.
- The hooks must stay valid on the Python 3.9 that ships with macOS: `/usr/bin/python3 -m py_compile tools/context_hook.py` (no `X | Y` annotations, no `match`). Several tests run them under that interpreter when it exists.
- Opt-in canary after a Claude desktop update: `CLAUDE_BUDDY_CANARY=1 .venv/bin/python -m pytest tests/test_context_hook.py -k real_desktop` checks that Claude desktop's files still have the keys the context hook reads (key names only, nothing private is printed).

A note that applies only when you run the tests from inside a Claude desktop session: that environment sets `CLAUDE*` variables (for example `CLAUDE_CODE_ENTRYPOINT`) that make `test_kiosk` fail. Unset them for the run: `for v in $(env | grep -E '^CLAUDE' | cut -d= -f1); do unset $v; done`.

## Pi versus Mac

The daemon is meant for the Pi (BlueZ, the backlight, Chromium kiosk). On a Mac you can still work on the page, the local API, the hooks, the census and the generators. To see the page without a daemon open `ui/index.html?demo=cycle` in a browser; to see it with a daemon, deploy to the Pi with `rsync` and `systemctl --user restart claude-buddy`, or tunnel with `ssh -L 8765:localhost:8765`.

## What we ask of a change

- **Tests with it**, especially anything touching `POST /decide`, the local API, the hooks or the page's tap handling.
- **Keep the safety rules** ([architecture.md](architecture.md#design-rules-that-shape-the-code)): `/decide` is loopback with a per-start token; the local API never approves; page text always with `textContent`; the 600 ms arming of the approve buttons stays; nothing a session says goes to a log.
- **Core and page stay dependency free** (standard library; the only runtime dependency is `dbus-fast`, in the transport). No CDN, no web fonts.
- Animations only with `transform` and `opacity`.
- The hook scripts run live in every Claude Code session of the people who use them: edit a copy, test it, then `mv` it into place, and keep `2>/dev/null; exit 0` in every documented registration.
- Code and comments in English; user-facing texts go through the i18n tables ([features/i18n.md](features/i18n.md)). No em dashes in texts.
- Design changes follow `DESIGN.md` (the dark room and the speech bubble; round pills; no hairlines, no dashboards).

## Docs

English docs live in `docs/`, the Spanish copies in `docs/es/` with the same file names. When you change one, change the other.
