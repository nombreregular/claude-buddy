# Hardware Buddy protocol, as implemented

This describes what the daemon does on the wire, from `buddy_core.py` and `buddy_bluez.py`. The protocol is Anthropic's Hardware Buddy ([REFERENCE.md](https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md) and the ESP32 firmware); the differences are listed at the end.

## Transport

- BLE GATT with the Nordic UART service: service `6e400001-b5a3-f393-e0a9-e50e24dcca9e`, RX `...0002` (the desktop writes; `write` and `write-without-response`), TX `...0003` (the device notifies).
- With pairing on (the default), both characteristics require an authenticated, encrypted link. `--insecure` removes that and is for tests only.
- Pairing uses a DisplayOnly agent: BlueZ picks a passkey, the screen shows it as 6 digits, the desktop asks you to type it. Numeric comparison, "just works" and typing a passkey on the Pi are refused.
- Advertisement: the 128-bit service UUID in the advertisement and the complete local name in the scan response, like the ESP32. The name is `Claude-XXXX`, the last four hex digits of the adapter's address in capitals. `--adv-mode kernel` asks the kernel to add the adapter name instead, which it cuts to 10 characters (`Claude-345`).
- Notifications are chunked to the negotiated MTU minus 3 bytes, at most 180 bytes and at least 20, with 4 ms between chunks.

## Framing

One JSON object per line. Incoming lines end with LF or CR and may arrive split across any number of writes (a UTF-8 character split between two writes is fine). A line over 16 KB is dropped whole, tail included. Lines that are not UTF-8, not JSON or not an object are ignored. Outgoing JSON is ASCII-only (`\uXXXX` escapes), so a notification split can never cut a character in half.

## Messages from the desktop

The kind of a message decides how it is handled:

| Has | Meaning |
|---|---|
| `cmd` | A command; answered with an ack |
| `time` | `[epoch_seconds, tz_offset_seconds]`: the desktop's clock, used for the clock and for night mode |
| `evt` | An event (for example a turn event); it keeps the link alive but is not a heartbeat |
| any of `total running waiting msg entries tokens tokens_today prompt completed` | A heartbeat |

Any command, `time`, `evt` or heartbeat marks the link alive. With no traffic for 30 seconds the link is considered dead and the state is cleared.

### Heartbeat fields

`total`, `running`, `waiting` (counts, clamped to 0 to 2^31-1), `tokens_today`, `completed` (true means "a turn just finished"), `msg` (up to 200 characters), `entries` (up to 8 lines of 200), `tokens` (tokens since the desktop started; the daemon credits deltas and latches on the first value so a daemon restart does not re-credit a whole session), and `prompt`: `{"id", "tool", "hint"}`.

A `prompt` becomes a pending approval with source `claude` (tool up to 80 characters, hint up to 500). The same `id` in later heartbeats is the same request. A heartbeat without `prompt`, or no heartbeat for 30 seconds, withdraws it.

### Commands

| `cmd` | Effect | Ack |
|---|---|---|
| `status` | Reports name, owner, `sec` (encrypted link), uptime and free disk, and stats (approvals, denials, median seconds to answer, nap seconds, level) | `{"ack":"status","ok":true,"n":0,"data":{...}}` |
| `name` | Sets the buddy's name (24 characters) | ack |
| `owner` | Sets the owner name (32 characters) | ack |
| `species` | Sets the species index, 0 to 255; 255 means "use the GIF character" | ack |
| `unpair` | Forgets the pairings made with the buddy | ack |
| `char_begin`, `file`, `chunk`, `file_end`, `char_end` | Receive a character pack, see below | an ack for each, with `n` |

Every command gets an ack, including unknown ones (`ok:false`, `"error":"unknown command"`). A `permission` command from the desktop is ignored: that direction is ours.

## Messages to the desktop

- Acks: `{"ack": "<cmd>", "ok": true|false, "n": <int>, "error"?: "...", "data"?: ...}`.
- The decision on a permission: `{"cmd":"permission","id":"<prompt id>","decision":"once"|"deny"}`. Answered in under 5 seconds, an approval also makes the pet happy.
- Only decisions about prompts that came over BLE go this way. Approvals from other sources never leave the Pi.

## Receiving a character pack

`char_begin` `{name, total}` opens a transfer into a temporary folder, `file` `{path, size}` starts a file, `chunk` `{d}` carries base64 data (the ack's `n` is the bytes of the file so far), `file_end` closes it and `char_end` validates and installs.

Limits and checks: at most 1.8 MB in total and 64 files; the free disk must hold the pack plus 4 KB; a file name is a flat plain name (no `/` or `\`, no leading dot, no control characters, at most 100 characters); a file may not exceed its declared size; every file the manifest names must exist; the manifest must be a JSON object. The pack is installed atomically into `<state-dir>/characters/<name>` (the name is sanitised to `A-Za-z0-9._-`, 48 characters), replacing the previous one, and becomes the active character. A transfer with no traffic for 20 seconds is aborted; a lost link aborts it too. See [features/pets-and-packs.md](features/pets-and-packs.md).

## Differences from the reference firmware

- A `turn` event is not a heartbeat. The reference treats any line without `cmd`/`time` as a snapshot, so a turn event would wipe a pending prompt.
- Every command gets an ack, unknown ones included.
- Pack transfers validate paths and sizes, write to a temporary folder and install atomically.
- One running session is enough for the "working" mood (the ESP32 firmware uses 3).
- If a paired desktop stays connected but never subscribes to TX or writes within 20 seconds, the daemon disconnects it (up to 3 times per silent spell) so that it reconnects and opens the channel. It never touches devices that are not the buddy's, or classic Bluetooth devices.
