# The pet (Mochi) and character packs

Buddy's character is **Mochi**: a soft pixel-art blob with blush cheeks and nothing stuck on it. It is the project's own character (not Clawd or any known mascot). It has seven moods and the whole screen takes the colour of its mood.

## Moods

| Mood | Colour | When |
|---|---|---|
| `sleep` | lavender `#A78BFA` | Nobody connected, or Bluetooth not ready |
| `idle` | sand `#FF9E7A` | Connected and calm |
| `busy` | mint `#4FD1C5` | At least one session running |
| `attention` | yellow `#FFCF5C` | A permission request is pending (from any source), or a session is waiting for you |
| `celebrate` | lime `#8BE76A` | A turn just finished, or a level up (3 s) |
| `dizzy` | grey blue `#A9B8CF` | Five quick taps on the pet |
| `heart` | pink `#FF8FA3` | You approved in under 5 seconds (2 s), or a tap on the pet |

Priority: a one-shot (celebrate on level up, heart on a fast approval) wins; then a pending request; then not connected; then waiting sessions; then a finished turn; then running; then idle. The pet levels up every **50,000 output tokens** (the desktop reports tokens since it started; the daemon counts the increases, so restarting it does not re-credit a whole session). Name, owner, level, approvals and denials are kept in `state.json`.

## Touching the pet

A transparent area over the pet (192x196 px) answers taps, with no server round trip and nothing saved:

- Asleep, the first tap wakes it (`idle` for 2.5 s; taps in that window count as hearts).
- Awake (any mood but `attention`), a tap gives `heart` for 2.5 s and another tap restarts the time.
- Five reacting taps within 2 s make it `dizzy` for 3 s, and taps are ignored until it ends. Afterwards the server's real mood returns.
- Each accepted tap squeezes it for 160 ms. The mood pill, headline and counters always say the real mood, not the one the touch put on.
- With a permission request on screen the area is inert: a big finger brushing below the pet's feet cannot land on Deny.

## Packs

The page draws the **active character pack** and falls back to its own SVG Buddy when there is none. A pack is a flat folder: `manifest.json` plus GIF (or PNG/WebP/JPEG) files. The official Hardware Buddy packs are 96 px GIFs; the page shows them at 192 px (2x, even pixels).

```json
{
  "name": "buddy",
  "render": "pixel",
  "colors": {"body": "#FF9E7A", "bg": "#0E0D14", "text": "#F3EEF7", "textDim": "#9A93A4", "ink": "#1B1626"},
  "accents": {"sleep": "#A78BFA", "idle": "#FF9E7A", "busy": "#4FD1C5", "attention": "#FFCF5C", "celebrate": "#8BE76A", "dizzy": "#A9B8CF", "heart": "#FF8FA3"},
  "states": {"sleep": "sleep.gif", "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"], "busy": "busy.gif",
             "attention": "attention.gif", "celebrate": "celebrate.gif", "dizzy": "dizzy.gif", "heart": "heart.gif"}
}
```

`states` maps a mood to one file or a list; a missing mood falls back as the page decides. `colors` (`bg`, `text`, `textDim`, `body`) are the only values that enter from a pack and the server keeps only `#rrggbb`. `render` and `accents` are this project's extensions to the manifest (`render: pixel` for crisp pixel art, one accent colour per mood).

- **Images** are served as `/character/<pack>/<file>`, and only files the manifest names. Each URL carries `?v=<rev>`, a hash of the manifest and of every listed file's name, size and modification time: replace a GIF under the same name and the screen refreshes its images by itself, without a blank frame and without restarting the kiosk.
- **Send a pack from Claude desktop:** drag the folder onto the Hardware Buddy window. It travels over BLE (limits in [protocol.md](../protocol.md#receiving-a-character-pack): 1.8 MB, 64 files, flat names), is validated, installed atomically in `<state-dir>/characters/<name>` and becomes active.
- **Install by hand:** copy the folder to `<state-dir>/characters/<name>` and set `"character": "<name>"` in `state.json` while the daemon is stopped.

## Mochi's generator

`tools/make_pixel_buddy.py` draws Mochi procedurally on a 32x32 grid, scaled 3x to 96 px transparent GIFs, with the seven moods. Every frame is checked after decoding (size, transparency, one body, extras clear of the head) and the run fails on a broken rule. It needs Pillow (the daemon never imports it).

```bash
python3 tools/make_pixel_buddy.py characters/buddy-pixel                    # the pack
python3 tools/make_pixel_buddy.py characters/buddy-pixel --sheet sheet.png  # plus a contact sheet
```

`tests/test_pixel_buddy.py` requires the folder in the repo to be identical to what the generator draws, so after changing the drawing run it again and copy the pack to the Pi. The same generator, with its `COSTUME` hook, makes the [closet](closet.md).
