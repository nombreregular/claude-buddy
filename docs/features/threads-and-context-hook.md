# Threads and the context hook (optional)

With several Claude sessions open, "3 working" does not say which ones. The Hardware Buddy protocol only carries totals, so a small Claude Code hook on your computer tells the buddy what each session is doing. The screen then lists **which conversations are alive**, with their real titles, grouped like the Claude desktop sidebar.

Needs the [local API](../local-api.md) turned on and the token in place. Install the hook with the snippet in the README ([Context hook](../../README.md#context-hook-which-conversations-are-alive)). It targets macOS: it reads Claude desktop's files under `~/Library/Application Support/Claude`.

## What the screen shows

- **Live threads only.** Working ones, and ones that finished their turn and wait for your answer. No history. A working thread that says nothing for 10 minutes leaves; one that finished stays 30 minutes. The board keeps 12, the screen shows 8: working first, then newest.
- **A row** is the conversation's title. A terminal session that Claude desktop does not know has no title or group: it shows its folder name under the group "Terminal". A desktop session in no group appears under "Sessions". A thread started by another hangs, indented, under its parent while the parent is alive.
- **Colours.** Working rows take the pet's mood colour; a finished one is soft grey and carries a check mark before its age. A row changes look when its state changes, never position.
- **More than fits.** The list shows about three rows at a time; a thin bar on the right says there is more (always) and the last visible row fades towards the edge. Drag with your finger.
- **Tap a row** to read what it said (up to 300 characters of its last reply; if it is working, the previous turn's, headed with a "before it said" label). **Back** returns. Untouched it closes after 25 seconds, and a permission request always wins: the detail closes and the buttons arm in 0.6 s as usual.

## How it works

The hook (`tools/context_hook.py`) runs on `PreToolUse` and `UserPromptSubmit` (working), `Stop` (finished, waiting for you) and `SessionEnd` (gone). `Stop` does not run if you interrupt Claude: an interrupted thread keeps showing "working" until it speaks again or 10 minutes pass.

Titles and groups come from Claude desktop's own files, matched only by the id the hook receives (`cliSessionId`); nothing is guessed. The group of a child with none of its own is the nearest ancestor's, as in the sidebar.

## It never slows a session down

It exits 0 and prints nothing whatever happens. It gives up after 1.5 s to connect and 2 s waiting for a reply, and a timer ends it after 2.5 s no matter what the network does (Claude Code's own limit is 5 s). After a failed attempt the buddy is left alone for 30 seconds: tool calls and new prompts do not even try, so a Pi that is off costs a session one slow call every 30 seconds, not one per tool. `Stop` and `SessionEnd` always try, because a lost "finished" would leave a thread saying "working".

Keep the `2>/dev/null; exit 0` on each registered command: without it a missing script exits with code 2, which Claude Code reads as "block".

## What travels, what is read, what is written

Sent to the Pi over plain HTTP, with the token: the folder name, the tool, a one-line summary, the conversation **title**, its **group name**, the id of its parent thread, and, when a turn ends, **`said`**: up to 300 characters of the last reply (the first 200 and the last 97), markdown flattened, code blocks replaced by `(código)` and anything that looks like a secret replaced by `[oculto]` (those two markers are the literal text the hook writes). That filter is best effort, not a boundary. This is why the API should listen on the Tailscale IP rather than `0.0.0.0`.

- `said` leaves the computer only towards itself or the tailnet (100.64.0.0/10 and fd7a:115c:a1e0::/48); to any other address the hook removes it. Note that 100.64.0.0/10 is also used by ISPs (CGNAT) and other VPNs, so this proves "not your home network", not "Tailscale".
- On your computer the hook reads from Claude desktop only `claude-code-sessions/<org>/<org>/local_<uuid>.json` (title, id, who started it) and, **from the desktop config file, only the groups and which group each session belongs to**. Nothing else in that file (connector settings, for instance) is looked at, stored, logged or sent. It also reads `~/.claude/sessions/<pid>.json` to match a hook to a desktop session.
- The only things it writes are in `~/.cache/claude-buddy/` (folder 0700, files 0600, yours; ignored if not private): `threads.json` (hook id, desktop id, title, parent; re-checked every 30 s) and `down_until` (the 30-second pause).
- The daemon keeps threads in memory only, writes none of it to any log and offers it to nobody: the screen listens on `127.0.0.1`.

## Switches (environment variables of the hook)

| Variable | Effect |
|---|---|
| `CLAUDE_BUDDY_URL` | Where it sends. Default `http://buddy:8766` |
| `CLAUDE_BUDDY_TOKEN_FILE` | The token. Default `~/.config/claude-buddy/hermes.token`; must be 0600 and yours |
| `CLAUDE_BUDDY_CONTEXT=off` | Turns the whole hook off |
| `CLAUDE_BUDDY_THREADS=off` | No title, group or parent, and nothing is read from Claude desktop; threads are named by folder |
| `CLAUDE_BUDDY_SAID=off` | Never sends `said` and never opens the transcript. In the **daemon's** environment (read at start) it makes the daemon discard `said` even if a client sends it |
| `CLAUDE_BUDDY_SAID_EXCLUDE=a,b` | Folder names whose sessions never send `said`. The hook is global, so scheduled and headless sessions send it too: this list is for them |

For tests or moved folders only: `CLAUDE_BUDDY_DESKTOP_DIR`, `CLAUDE_BUDDY_CACHE_DIR`, `CLAUDE_CONFIG_DIR`.

If the hook was installed before a Claude desktop update, run the canary afterwards: `CLAUDE_BUDDY_CANARY=1 .venv/bin/python -m pytest tests/test_context_hook.py -k real_desktop`. It checks key names only and prints nothing private.

The wire contract (`POST /session`) is in [local-api.md](../local-api.md#post-session-what-a-thread-is-doing).
