# Census of background runs (optional)

The context hook only speaks when a session calls a tool. A quiet `claude -p` run, or a script that is not a Claude session at all, would not appear. `tools/census.py` looks at your computer's process table every 15 seconds and posts a snapshot to the [local API](../local-api.md) (`POST /census`, same token). The screen shows the rows as the **Runs** group ("Corridas" in the Spanish interface) with each row's name and how long it has been running.

It targets macOS (it runs as a LaunchAgent) and uses only the standard library and the Python 3.9 that ships with macOS.

## What counts as a run

A process that is **all** of these:

- its program is called `claude`, and the word after it is a flag;
- `-p` or `--print` is among its first 80 words, and there is no `--sdk-url` there;
- nothing above it lives inside Claude.app or is itself a run (a nested `claude -p` belongs to the outer one);
- it is older than `min_age_s` (20 s).

Wrappers (`bash -c ... claude -p`, `caffeinate`, `ssh`) are never runs; their child is. Scripts ("jobs") appear only if your `census.json` names them.

## Install

```bash
tools/census_agent.sh install      # writes and loads the LaunchAgent com.claude-buddy.census; -n shows the plan only
tools/census_agent.sh status       # loaded? pid, last exit code, token, config; --ping does a GET /healthz
tools/census_agent.sh restart      # after updating census.py
tools/census_agent.sh uninstall    # unloads and removes the plist (--purge also deletes the log)
```

The repo cannot live in Documents, Desktop, Downloads or iCloud (macOS protects them from background agents). Nothing runs without `install`. Pass `--url` to `install` if the buddy is not `http://buddy:8766`: launchd does not forward your shell's environment.

Your own settings go in `~/.config/claude-buddy/census.json` (outside git; the model is `tools/census.example.json`). Without it the census only sees `claude -p`. Keys: `jobs` (what to name, matched against a process's script or program name, never its arguments; `regex`, `name`, `min_age_s`), `ignore`, `strip_prefix`, `interval_s`, `max_rows`, `group`, `url`, `token_file`, `insecure_url_ok`.

## Privacy

What leaves the computer: a name (the run's folder name, or the one your config gives a job), the seconds it has been running, the seconds since its transcript last changed, the session id (so a run and its hook thread show as one row) and the folder name. **Never** a command line, argument, environment, path, prompt or output: they are read in memory to classify a process and dropped. The token only travels to this computer, the tailnet or loopback; if the URL's name leads to another address the census holds it back.

## Contract

`POST /census` `{v:1, source, seq, ttl, more, rows[<=8]}`; each row `{id: "run-..."|"job-...", name, group, state:"working", elapsed, idle?, session?, project?}`. A snapshot replaces the previous one of its `source` and expires by itself after `ttl` (4 times the interval), so if the census dies the group is gone within a minute. Wrong types are `400`; numbers out of range are clamped. The screen draws up to 8 rows, of which up to 5 are runs and at least 3 are threads. A run and its hook thread show as one row (matched by session, or by folder only when the run does not name its session).

Against an old daemon `/census` answers `404`; the census waits 5 minutes and tries again, so the order of deployment does not matter.

## Known limits

The registry `~/.claude/sessions/<pid>.json` is Claude Code's own and undocumented: if it changes, the exact session match is lost and only the folder match remains. A run whose prompt contains the words `--sdk-url` can confuse the 80-word window. A `claude -p` that launches a desktop conversation as a tool is deliberately not shown.

Options (environment): `CLAUDE_BUDDY_URL`, `CLAUDE_BUDDY_TOKEN_FILE`, `CLAUDE_BUDDY_CENSUS=off` (exit at once), `CLAUDE_BUDDY_CENSUS_CONFIG`, `CLAUDE_CONFIG_DIR`. `python3 tools/census.py once --print` runs one pass and prints the snapshot without sending anything.
