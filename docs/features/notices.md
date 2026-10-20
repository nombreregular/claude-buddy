# Notices (optional)

A script or an assistant leaves a short message on the screen: "the batch finished", "meeting in 10 minutes". It appears as a **Notices** group at the top of the list, closes with a tap on **Done**, expires by itself and, if asked, wakes the dimmed screen at night. Notices live in memory: restarting the daemon clears them.

Needs the [local API](../local-api.md).

## From the command line

`tools/notify.py` is a single file (standard library, Python 3.9) that imports nothing from the repo, so it also runs on the Pi or on the machine where an assistant lives:

```bash
python3 tools/notify.py --title 'The batch finished' --body '37 of 40 passed' --kind ok --ttl 600 --id batch-21 --wake
echo 'long detail' | python3 tools/notify.py --title 'Notice' --body -     # stdin must close
python3 tools/notify.py --status batch-21      # shown, dismissed, expired, withdrawn, evicted or unknown
python3 tools/notify.py --withdraw batch-21
```

| Option | Meaning |
|---|---|
| `--title` | Required, 1 to 60 characters once cleaned |
| `--body` | Optional; up to 4 lines and 240 characters; `-` reads stdin |
| `--kind` | `info` (default, the accent colour), `ok` (green), `warn` (amber); anything else is `400` |
| `--ttl` | Seconds, clamped to 10..3600, default 300 |
| `--wake` | At night, light the screen for 30 seconds |
| `--id` | `[A-Za-z0-9_.:-]` up to 64. The same id **replaces** the notice that has it and restarts its time; without one the daemon invents `n-<counter>` |
| `--source` | Who sent it, up to 24 characters, shown as "from <source>"; default `hermes` |

Environment: `CLAUDE_BUDDY_URL` (default `http://buddy:8766`; the daemon accepts only the IP it is bound to or the Pi's own name in `Host`) and `CLAUDE_BUDDY_TOKEN_FILE` (default `~/.config/claude-buddy/hermes.token`; a regular file, yours, 0600, 16 or more characters).

Exit codes: 0 the buddy answered (its JSON is on stdout); 1 it could not be delivered (a fixed phrase on stderr with the exception name or the HTTP status, never the token or the text); 2 wrong usage. The whole call takes at most 3 seconds and is never retried: **do not retry after a 403**, five bad tokens in a minute lock that client out of every route, hooks included. With `curl` the token shows up in `ps`, which is why the script is preferred. `--insecure-url-ok` lifts the URL rule and the address check.

## The HTTP side

`POST /notice` `{"title","body?","kind?","ttl?","wake?","id?","source?"}` -> `200 {"ok":true,"id":...,"active":n}`. Unknown keys are ignored; a wrong type or shape is `400`. At most **3 active**: a fourth evicts the oldest (`evicted`). The request budget is 30 per minute per client, apart from the hooks'. `GET /notice/<id>` returns the state (finished ones are remembered for an hour, 50 at most); `DELETE /notice/<id>` takes it back (harmless again or for an unknown id).

`wake: true` wakes the screen on **every** POST, even when the notice did not change, on purpose (so one that waited can be escalated). A sender that repeats it more often than every 30 seconds keeps the panel lit; only the token and the 30-per-minute budget bound it.

## On the screen

A row of 44 px with a bell, the title and the age, tinted by kind (a check or an exclamation mark also marks `ok` and `warn`, so the colour is not the only signal). Tap it for the detail: title, "Notice, from <source>, <age> ago" and the body, with **Done** and **Back**. A new or changed notice makes the pet look up for 4 seconds, never on the first view after loading, with the screen dark, under a permission request or the pairing code. A pending permission request always keeps the screen.

At night, without `--wake` a notice waits and is there the next time the screen wakes. By day `--wake` changes nothing visible.

## Privacy

The text of a notice (title, body, source, not even the id) is never written to a log or to an error; the daemon only logs "notice: <kind>, N active". The page writes it with `textContent`. `/notice` and `/dismiss` can never approve or deny anything.
