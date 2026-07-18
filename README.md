# Leap Day Mod Studio

A desktop **level editor** for Leap Day, distributed **without** shipping the
copyrighted game.

By default the editor runs in **Safe mode**: you edit the game's **levels** only
— painting tiles, placing enemies/hazards, and setting each chunk's
`<difficulty>` rating. This is all most people need, and it can't break the game.

An optional **Power mode** (a toggle in the toolbar, off by default) unlocks
advanced tools — theme reassignment, per-enemy tuning, checkpoints, and other
tweaks that patch **the copy of the game on your own machine** at build time.
Those patches are applied locally and are **never shared**: the mod you
distribute is still only your authored level content (see below).

## How distribution stays legal
The mod you share is a **`.ldmod` file** — it contains *only your authored level
content* (custom level XML), never any original game bytes. Whoever installs
your mod supplies **their own** copy of the game (`leap-day-*.xapk`); the studio
applies your `.ldmod` to it on their machine and produces a signed, installable
APK. Same legal model as ROM-hack patches.

## Requirements (Windows · macOS · Linux)
- **Python 3.10+**, then:
  ```
  python3 -m pip install -r requirements.txt
  ```
  (installs `UnityPy`, `pywebview`, `Pillow`, `TypeTreeGeneratorAPI`.
  `TypeTreeGeneratorAPI` is used **read-only**, only to locate sprites the game
  stores inside custom script components — it writes no gameplay values.)
- Per-OS GUI backend for `pywebview`:
  - **Windows** — the Edge **WebView2** runtime (already on Win 11; on Win 10
    grab the free "Evergreen" runtime from Microsoft).
  - **Linux** — `sudo apt install python3-gi gir1.2-webkit2-4.1` then
    `pip install pycairo PyGObject`.
  - **macOS** — built-in WebKit, nothing extra.
- **Building an APK** additionally needs **Java 17+** (signing, via the bundled
  `vendor/uber-apk-signer.jar`). Installing to a device needs `adb` + an
  **arm64** Android emulator (the game ships arm64/armv7 only — no x86).

## Run the studio (GUI)
```
python3 studio/app.py
```
Workflow: **Load Game (.xapk)** → pick a chunk → paint tiles / place enemies /
set the level's `<difficulty>` rating → **Save level → mod** → **Build APK**
(or **Build + Install** with an emulator connected). Save with **Save .ldmod**.

## Headless build (no GUI)
```
python3 studio/cli.py --xapk LEAPDAY.xapk --mod my.ldmod --install
```

## What you can edit
- **Custom levels** — overwrite any of the game's level chunks with your own
  layouts (14-wide grid), tiles, enemies, hazards, paths, and per-chunk
  `<difficulty>` *rating* (which difficulty pool the level lands in — this is
  level metadata, not a physics change). They flow into the daily rotation
  automatically.

## Layout
```
core/      engine: bundle (UnityPy TextAsset edits), chunkfmt (level XML),
           sprites (palette art), apkbuild (rebuild+sign+install),
           modbuild (orchestrate), project (.ldmod)
studio/    desktop app (app.py = pywebview shell, ui/ = canvas editor) + cli.py
patcher/   standalone patcher app (Tkinter) + PyInstaller spec
tiles/     catalog.json — tile/enemy palette scraped from the game's chunks
vendor/    uber-apk-signer.jar
```

## Roadmap
- ~~Visual path/waypoint editing.~~ Done: direction arrows, distinct start
  marker, click-a-segment to insert a vertex, Alt-drag to move a whole path,
  Shift to draw straight, live point/length readout.
- Autotile edge preview.
- ~~Theme / daily-rotation editing.~~ Done: the "Build a day's level" panel shows
  each day's background theme and lets you reassign it (a `force_theme` `.so`
  patch on the locked day), insert an existing/custom chunk between two gameplay
  chunks (＋), and author from-scratch chunks in the **Custom chunks** library.
  Note: the game can't add brand-new *named* chunks the generator picks on its
  own, and a day's level length is fixed — inserting drops the last gameplay slot.
