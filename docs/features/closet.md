# Costume closet (optional, off by default)

The pet can wear a costume: party hat, crown, wool cap, wizard hat, chef hat, bow, flower or cat ears, in seven colours (56 looks). The costumes are **baked into the GIFs** (an overlay on the page would drift off the bobbing body), they are made on your computer and they live in a closet folder **outside git**. The daemon stays standard library only and never imports Pillow.

## Make costumes

On your computer (needs Pillow):

```bash
python3 tools/make_closet.py ~/closet-buddy --count 8 --sheet closet.png
```

Each costume lands in `~/closet-buddy/<template>-<color>/`: a complete pack (`manifest.json` plus the 9 GIFs, about 60 KB) whose manifest has a `costume` block (`name`, `template`, `color`, `base`). Other options: `--seed S`, `--base-name buddy` (the pack the costume dresses), `--list` (what the closet holds). Every run adds different ones; at 56 it says so and exits 0. `--sheet` draws every costume in the 7 moods so you can look at them. Without Pillow it tells you how to install it and exits 1.

To add a template: one more `Template(id, name_es, rows, sink)` in `TEMPLATES` of `tools/closet_costumes.py`.

## Put them on the Pi and switch it on

```bash
rsync -a ~/closet-buddy/ <user>@buddy.local:.local/state/claude-buddy/closet/
```

Add `--closet auto --closet-every 3` to the `ExecStart` of the service and restart it (`auto` is `<state-dir>/closet`; or pass any folder).

| Option | Meaning |
|---|---|
| `--closet off\|auto\|PATH` | Default `off`: nothing is read, no timer runs and the character is exactly the active pack |
| `--closet-every HOURS` | 0.25 to 168, default 3; 0 picks one at start and never changes |

## How it behaves

- The daemon picks at random among the costumes **and "no costume"**, never the same twice in a row, at start and every H hours with one timer. Each pick rereads the folder, so a costume copied later enters the draw.
- It is only a view override: it never changes the active pack. A costume is worn only while the active pack has the same name as the costume's `base` (`buddy` on the reference setup), so keep the closet homogeneous. A closet that vanishes or breaks while running falls back to the pack underneath.
- The closet is somebody's folder and nothing in it is trusted: a folder with a manifest up to 64 KB, a `costume` block (name up to 40 characters and `base`), states pointing at regular files up to 256 KB, a folder name `[a-z0-9-]` up to 63 characters, no symbolic links anywhere, and at most 64 costumes. Anything else is skipped and counted once per kind in the log.
- The log (without `-v`) says "Closet: N disfraces, cambia cada H h", "Closet: descarté N carpetas (tipos)", and when Buddy puts on or takes off a costume. Never the contents of a file.

The costumes are one piece with a 1 px ink outline, centred on the head of each frame (following the bounce and the dizzy lean) and never hiding the extras (Z's, hearts, sparks, drops). The default Mochi pack in `characters/buddy-pixel` comes out byte for byte the same with or without the costume hook of the generator.
