# Hermes Agent and Claude Code in a terminal (optional)

Both use the same door: the [local API](../local-api.md) (`POST /approval`), which queues a request in the **same queue** the screen answers. On the screen the request is labelled by who asks (`hermes · terminal`, `cli · Bash`) and Claude and the others are served in order of arrival. Only a tap (or the `a`/`d` keys on the Pi) decides.

Common setup: start the daemon with `--hermes-bind <tailscale-ip> --hermes-token-file ...` and copy the token to the machine that asks (see the README, "The local API").

## Hermes Agent plugin

Hermes Agent already asks a human before it runs a dangerous command (in Discord, the TUI, ...). `hermes-plugin/claude-buddy/` is a Hermes plugin that registers an approval transport named `buddy`: the same request goes to the Pi's `POST /approval`, shows on the screen as `hermes · terminal` with the command and Hermes' explanation, and the tap comes back as the decision (`once` or `deny`). The plugin only presents the request: what counts as dangerous, the choices, the maximum time and the last word stay with Hermes (`hermes_cli/approval_transport.py` in its code). If the screen cannot answer (buddy off, no token, nobody taps within `timeout` seconds) the plugin fails on purpose and, with `transport_fallback: builtin`, Hermes asks the way it did before. **It never approves or denies on its own.** Standard library only.

Install on the machine where Hermes runs, as the user that runs Hermes (`<hermes-user>`):

```bash
# copy the plugin folder into the Hermes plugins folder of that user
rsync -a --delete hermes-plugin/claude-buddy/ <hermes-host>:<hermes-home>/plugins/claude-buddy/
```

The listener token must end up on the Hermes host owned by `<hermes-user>` with mode 0600, without crossing a shared terminal. For example from your computer, piping it so it is never written locally:

```bash
ssh <user>@buddy.local 'cat ~/.config/claude-buddy/hermes.token' \
  | ssh <hermes-host> 'sudo install -m 600 -o <hermes-user> -g <hermes-user> /dev/stdin <hermes-home>/claude-buddy.token'
```

Then, as `<hermes-user>`:

```bash
hermes plugins doctor claude-buddy        # import and registration OK
hermes plugins enable claude-buddy
hermes config set plugins.entries.claude-buddy.settings.url http://<tailscale-ip>:8766
hermes config set plugins.entries.claude-buddy.settings.timeout 120
hermes config set security.approval.transport buddy
hermes config set security.approval.transport_fallback builtin
```

| Setting (`plugins.entries.claude-buddy.settings.*`) | Default | Meaning |
|---|---|---|
| `url` | `http://buddy.local:8766` | The local API on the Pi (the Tailscale IP is better) |
| `token_file` | `~/.hermes/claude-buddy.token` | The API token, mode 0600 and owned by the Hermes user; if it does not comply the plugin will not use it |
| `timeout` | `120` | Seconds the screen gets before Hermes asks elsewhere (5 to 3600) |

How it looks: Hermes wants to run something dangerous, it appears on the screen, you tap **Approve once** or **Deny**, Hermes continues. If nobody taps within the timeout the request leaves the screen and Hermes asks through its fallback. `hermes approvals test "<command>"` says, without running it, whether a command would ask. To test the plumbing without touching Hermes' config use the `curl` of [local-api.md](../local-api.md#delete-approvalid). Note that `hermes chat -q --oneshot` is unattended and denies by itself without consulting transports: test with the interactive TUI or a chat channel.

## Notices from Hermes (the `buddy-pantalla` skill)

`hermes-plugin/skills/buddy-pantalla/SKILL.md` is a Hermes skill (written in Spanish; translate it for your agent if you prefer) that teaches when and how to leave a short notice on the screen: when something long finished or failed, never for chatter, stable ids, `--wake` only for what cannot wait until morning, and what to do after a `403` or `429`. It expects a command named `buddy-notify` on the Hermes host. A wrapper next to a copy of `tools/notify.py`:

```sh
#!/bin/sh
export CLAUDE_BUDDY_URL=http://<tailscale-ip>:8766
export CLAUDE_BUDDY_TOKEN_FILE="$HOME/.hermes/claude-buddy.token"
exec python3 -I "$(dirname "$0")/buddy-notify.py" "$@"
```

Copy `tools/notify.py` as `buddy-notify.py` beside it (again after any change to the original) and copy the skill folder into the Hermes skills folder. Hermes' own approval policy decides whether it may run `buddy-notify` without asking each time. Details of the command: [notices.md](notices.md).

## Claude Code in a terminal (CLI hook)

`tools/cli_hook.py` is a `PermissionRequest` hook (standard library, runs on the Python 3.9 of macOS). It sends the request to the same API with `"source": "cli"`: it appears as `cli · Bash`, "Claude Code asks for permission", with the command and the folder. Claude Code only calls the hook when it was **about to show its own dialog**, so your `allow` rules, `acceptEdits` and `auto` keep working as before.

**Only a tap on the screen approves or denies.** Anything else (Pi off, the API closed, no token, a token others can read, nobody taps in 60 s, a strange answer, Ctrl-C) ends in the **normal terminal prompt**, and a request already on the screen goes away. `AskUserQuestion` and `ExitPlanMode` stay in the terminal. In **Claude desktop** sessions the hook does nothing (`CLAUDE_CODE_ENTRYPOINT=claude-desktop`): they read the same `settings.json` but their requests already arrive over Bluetooth. A terminal opened from a desktop session inherits that variable.

**The terminal does not wait for the buddy.** Claude Code shows its dialog while the hook waits; answer wherever is closer and the first wins. If you answer in the terminal the hook sees the tool result appear in the session transcript and removes the request from the screen.

**Always.** When Claude Code proposes a rule so it will not ask again, the screen adds a third button, **Always**, and shows the whole rule above it, for example "Always save Bash(npm run *) in this project". Mind that the rule may be broader than the command. Tapping approves and hands Claude Code the same suggestion unchanged, which saves it as if you had chosen it in the terminal (`.claude/settings.local.json` of the project). Only plain `allow` rules are offered; new folders or mode changes stay in the terminal.

Register in `~/.claude/settings.json` (if you already have `"hooks"`, add only the `PermissionRequest` key):

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 \"$HOME/Projects/claude-buddy/tools/cli_hook.py\" 2>/dev/null; exit 0",
            "timeout": 90
          }
        ]
      }
    ]
  }
}
```

Claude Code's `timeout` must exceed the hook's wait (60 s by default). The `2>/dev/null; exit 0` makes a hook that cannot start (the file moved) leave the question in the terminal instead of denying it. To try it without touching your settings: save the JSON elsewhere and run `claude --settings that-file.json`.

| Variable | Flag | Default |
|---|---|---|
| `CLAUDE_BUDDY_URL` | `--url` | `http://buddy:8766` |
| `CLAUDE_BUDDY_TIMEOUT` | `--timeout` | `60` seconds waiting for the tap (1 to 600) |
| `CLAUDE_BUDDY_TOKEN_FILE` | `--token-file` | `~/.config/claude-buddy/hermes.token` |
| `CLAUDE_BUDDY_HOOK_LOG` | `--log` | none; if set, one line per request (never the token) |

The flag wins over the variable. The default URL is the Pi's name on Tailscale; if the API listens on your LAN use `CLAUDE_BUDDY_URL=http://buddy.local:8766`. If the log shows `Errno 65` (`No route to host`) with the Pi on your LAN, grant **Local Network** to your terminal (see the README troubleshooting).
