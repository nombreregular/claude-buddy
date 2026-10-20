# Night mode

At night the screen dims **by lowering the real backlight** of the Pi's display, because a black veil over an LCD leaves the backlight on. A tap wakes it for 30 seconds; a permission request, a pairing code or a notice sent with `wake` wake it by themselves. It is on by default.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--night HH:MM-HH:MM` or `off` | `23:00-07:00` | The window. Start inclusive, end exclusive, it may cross midnight, start equal to end means always night. An invalid value exits with code 2 |
| `--night-level N` | 12 | Backlight at night, from 4 up to the panel's maximum (never fully dark). By day it is the maximum |
| `--backlight auto\|off\|PATH` | `auto` | `auto` takes the first writable panel under `/sys/class/backlight/`; `off` makes the page darken itself with a veil (78% black) instead; or give the folder of one (with `brightness` and `max_brightness`) |

The clock is your computer's local time (it tells the buddy over BLE, the same clock the screen shows) and, if it never did, the Pi's own. With `--night off`, `--no-ui` or the screen port busy, night mode and the backlight are off entirely and the daemon never touches the brightness.

## What wakes the screen (30 seconds, `AWAKE_S`)

- A tap on the dark screen (`POST /wake`). That tap does nothing else: it opens no row, presses no button and leaves no echo.
- A pending permission request (30 more seconds after the last one is resolved or expires), and a pairing code on screen. The approve buttons still arm in 0.6 s.
- While awake at night, any tap on the screen keeps it awake (the page reports it, at most once every 5 seconds), so you can keep reading a thread.
- A notice with `wake: true` ([notices](notices.md)).

The daemon decides: the view carries `night`, `dim` and `backlight`, and the daemon republishes by itself when the awake time ends or the window starts or ends. For 350 ms after waking (extended to 600 ms from the moment a finger lands in that guard) the dark layer still swallows taps. A permission request is never covered or darkened.

## The panel and the exit

The backlight is only written when the level changes. A write error disables dimming and the page falls back to the veil; if the failed write was the one back to day, it retries once a second up to 60 times so the panel is not left dim. A panel whose "night" is not below its "day" (an on/off GPIO) is not used. The daemon restores day brightness first thing when it stops (SIGTERM from systemd, SIGINT, the `q` key, SIGHUP, a Bluetooth start that fails). SIGKILL and a power cut do not restore it: the panel stays dim until the service restarts and the first sync writes the right level.

The service needs write access to the backlight: on Pi OS the user must be in the `video` group. After a restart look for the line "Modo noche ..." in `journalctl --user-unit claude-buddy`; if it says it found no screen light to move, the service lacks the group and only the veil is used.

Do not run the test suite on a Pi at night: the tests that start the real daemon do not pass `--backlight off` and would dim the real panel for a while.

Preview without a daemon: `ui/index.html?demo=night`, `?demo=night-overlay` (the veil) and `?demo=night-ask` (a permission request at night).
