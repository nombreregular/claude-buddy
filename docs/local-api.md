# Local API

The daemon runs two HTTP servers. In the code the second one is still called the Hermes listener (`buddy_hermes.py`, `--hermes-*` options); in these docs it is **the local API (historically "Hermes listener")**, because anything can use it: Hermes Agent, the Claude Code hooks, the census, scripts.

| | Screen server | Local API |
|---|---|---|
| Default port | 8765 (`--ui-port`) | 8766 (`--hermes-port`) |
| Listens on | `127.0.0.1` only | the IP given with `--hermes-bind`; **off without it** |
| Used by | the Chromium page | hooks, census, `notify.py`, Hermes |
| Can approve a command | yes, with a tap (`POST /decide`) | never |

Both speak strict HTTP/1.1, one request per connection, bodies up to 4 KB (`413` above), JSON errors `{"ok": false, "error": "..."}`. Neither ever writes the text of a request, thread or notice to a log.

## The local API (port 8766)

### Start it

```bash
buddy.py --hermes-bind <tailscale-ip> --hermes-token-file ~/.config/claude-buddy/hermes.token
```

The token is a file readable only by you (the daemon warns if others can read it) or the `BUDDY_HERMES_TOKEN` variable, and has **at least 16 characters**. Without a valid token the daemon prints `Hermes apagado: ...` and runs without the API. Prefer the Tailscale IP to `0.0.0.0`, which opens the API to your whole LAN.

### What every request is checked for

- `Authorization: Bearer <token>`, compared in constant time, never logged. **5 bad tokens in a minute from the same IP lock that IP out for a minute** (`429`), on every route. Do not retry after a `403`.
- `Host` must be the IP the client connected to, or the Pi's own host name or `<name>.local`, each with the port. A MagicDNS name such as `buddy.<tailnet>.ts.net` is refused with `403`; use the IP.
- Any `Origin` header is refused: clients are scripts, not browsers.
- `Content-Type: application/json` on `POST`.
- At most 16 open connections, 16 pending approvals.
- Request budgets per client and minute, each counted apart so one cannot starve another: **300** for `/approval` and `/session` together, **60** for `/census`, **30** for `/notice`. Past it: `429`.
- `GET /healthz` answers `ok` with no token (it still checks `Host`).

### `POST /approval`

Queue a permission request on the screen, in the same queue as Claude desktop's.

```json
{"id": "h_1", "tool": "terminal", "hint": "sudo systemctl restart pihole-FTL", "ttl": 900, "source": "hermes"}
```

- `id`: 1 to 64 characters of `A-Za-z0-9_.-`, chosen by the caller. `tool` (cut to 80) and `hint` (cut to 500) are what the screen shows.
- `ttl`: seconds, 1 to 3600, default 900. If nobody answers in that time the request expires and leaves the screen.
- `source`: `hermes` (default) or `cli` (a Claude Code terminal, see [features/hermes.md](features/hermes.md)); never `claude`, those answers go to the computer over BLE.
- `always` (only with `source: "cli"`, up to 120 printable characters): the permission rule a third button, **Always**, would save. Only then does the screen show that button and may `decision` come back as `always`.
- A `callback` key is refused with `400`: the Pi never opens connections towards a URL it was handed.
- Answer `202 {"ok":true,"id":"h_1","key":"hermes:h_1","poll":"/approval/h_1","ttl":900}`. `409` if that `id` is already pending.

### `GET /approval/<id>?wait=N`

`{"id","key","tool","state","decision"}`. `state` is `pending`, `decided` (`decision` is `once`, `deny` or `always`), `withdrawn` or `expired`. `wait` (0 to 30) holds the connection up to N seconds for the decision: long polling. Loop on it until `state` is not `pending`. An answered request is remembered for 10 minutes, then `404`.

### `DELETE /approval/<id>`

The caller gave up: the request leaves the screen (`withdrawn`).

```bash
TOKEN=$(cat ~/.config/claude-buddy/hermes.token)
curl -sS -X POST http://buddy:8766/approval \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id": "h_1", "tool": "terminal", "hint": "ls /tmp"}'
curl -sS "http://buddy:8766/approval/h_1?wait=30" -H "Authorization: Bearer $TOKEN"
```

Never use `curl -v`: it prints the token. Why long polling and not a callback: with a callback a stolen token could turn the Pi into a scanner of your network. With long polling the Pi calls nobody, the caller needs no open port and it works the same over Tailscale, NAT or a LAN.

### `POST /session`: what a thread is doing

Sent by `tools/context_hook.py`. Only `id` is required.

| Field | Meaning |
|---|---|
| `id` | The hook's `session_id`, 1 to 64 of `A-Za-z0-9_.-` |
| `state` | `working` (default), `idle` (turn finished, waiting for you) or `gone` (leaves the board with everything) |
| `project`, `tool`, `what` | Folder (40), tool (40), one-line summary (120). An empty `project` keeps the previous one; an `idle` with no `tool`/`what` keeps the previous turn's |
| `said` | Up to 300 characters of plain text; paragraph breaks are kept |
| `title`, `group` | Up to 60 and 40 characters, one line |
| `parent` | The `id` of the thread that started this one |

For `said`, `title`, `group` and `parent` one rule: **key absent keeps what the board has, `""` clears it, text replaces it.** A value that is not a string, or a `parent` that is not shaped like an id, is `400`; a thread is never its own parent. Answer `200 {"ok":true,"id":...,"state":...}`. The board keeps 12 threads and gives the screen 8. See [features/threads-and-context-hook.md](features/threads-and-context-hook.md).

### `POST /census`: what runs without a hook

Sent by `tools/census.py`: `{"v":1,"source":"mac","seq":N,"ttl":60,"more":0,"rows":[...]}`, at most 8 rows `{id, name, group, state:"working", elapsed, idle?, session?, project?}` with ids starting `run-` or `job-`. A snapshot replaces the previous one of its `source` and expires after `ttl` seconds (clamped to 5..120, default 60). A wrong type is `400`; a number out of range is clamped, never refused; an older `seq` answers `{"ok":true,"stale":true}`. See [features/census.md](features/census.md).

### `POST /notice`, `GET /notice/<id>`, `DELETE /notice/<id>`

`{"title","body?","kind?","ttl?","wake?","id?","source?"}` -> `200 {"ok":true,"id":...,"active":n}`. The state of a notice is `shown`, `dismissed`, `expired`, `withdrawn`, `evicted` or `unknown`. All limits are in [features/notices.md](features/notices.md).

`/session`, `/census` and `/notice` return `404` if the daemon has no board for them, and none of them can approve or deny anything.

## The screen server (port 8765)

Only reachable from the Pi itself (or through an SSH tunnel). Every request needs a `Host` of a loopback name and, if the browser sends an `Origin`, one that matches.

| Route | What it does |
|---|---|
| `GET /` | The page, with this start's token inside (a new token per start; the page reloads itself when the daemon restarts) |
| `GET /events` | Server-sent events with the whole view model, a `retry: 1500` hint and a ping every 10 s; at most 8 streams |
| `GET /state` | The same view model once (the page polls it with `?poll=500` in its URL) |
| `POST /decide` | `{"key": "claude:<id>", "decision": "once"\|"deny"\|"always"}`. Needs `X-Buddy-Token`, `Content-Type: application/json` (`415` otherwise). `200`, `409` if no longer pending, `400` if `always` is not offered |
| `POST /wake` | A tap on the dark screen: wakes it for 30 seconds. Same token rules; at most 20 per 10 s; `{"ok":true,"dim":bool}` |
| `POST /dismiss` | `{"id": "<notice id>"}` removes a notice. Same token rules; at most 20 per 10 s; `{"ok":true,"state":...}` |
| `GET /character/<pack>/<file>` | An image of the active pack (or of the worn costume), and only files its manifest names |
| `GET /healthz` | `ok` |

The page is served with `Content-Security-Policy: default-src 'none'` (script and style inline, images from itself).
