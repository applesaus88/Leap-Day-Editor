# Leap Day Mod Studio

Alpha version. Found some more bugs, this version is not very stable but 95% of the things work. Will fix the other 5% somewher this week. Big bug some enemies wont spawn in certain themes.

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
python3 -m pip install -r requirements.txt
python3 studio/app.py
```
Workflow: **Load Game (.xapk)** → pick a chunk → paint tiles / place enemies /
set the level's `<difficulty>` rating → **Save level → mod** → **Build APK**
(or **Build + Install** with an emulator connected). Save with **Save .ldmod**.

## Headless build (no GUI)
```
python3 studio/cli.py --xapk LEAPDAY.xapk --mod my.ldmod --install
```

## Android emulator setup (for Build + Install)
You only need this to **install/playtest** on your PC. To just make a mod you can
skip it — **Build APK** produces a signed APK you can copy to a real Android phone.

Leap Day ships **arm64/armv7 only** (no x86 build), so the emulator **must be
arm64-v8a**. On Apple-Silicon Macs an arm64 emulator runs natively (fast); on
Intel/AMD (most Windows/Linux) an arm64 AVD is *emulated* and **slow but usable**.

**1. Install the SDK tools.** Easiest: install **Android Studio** (Win/macOS/Linux)
— it bundles the SDK, the emulator, and `adb`. Make sure `adb` and `emulator` are
on your `PATH` (they live under the SDK, e.g. macOS
`~/Library/Android/sdk/platform-tools` and `.../emulator`; Windows
`%LOCALAPPDATA%\Android\Sdk\...`).

**2. Install an arm64 system image + create the AVD.**
- **Android Studio:** *Device Manager → Create device → pick a phone (e.g. Pixel 5)
  → choose a system image whose ABI is **arm64-v8a** → name it `leapday` → Finish.*
- **Command line:**
  ```
  sdkmanager "platform-tools" "emulator" "system-images;android-30;google_apis;arm64-v8a"
  avdmanager create avd -n leapday -k "system-images;android-30;google_apis;arm64-v8a" -d pixel_5
  ```
  (API 30 / Android 11 is a safe choice.)

**3. Start the emulator.**
```
emulator -avd leapday -gpu swiftshader_indirect
```
(`-gpu swiftshader_indirect` avoids graphics glitches/crashes; you can drop it if
your host GPU works fine.)

**4. Confirm it's connected.**
```
adb devices
```
You should see e.g. `emulator-5554   device`. If it says `offline`, the emulator is
still booting — wait a bit. Nothing listed? `adb kill-server && adb start-server`.

**5. Install your mod.** With the emulator showing in `adb devices`, click
**Build + Install** in the studio (or headless: add `--install` to the `cli.py`
command above). It signs the APK and pushes it to the running emulator.

**Troubleshooting**
- `INSTALL_FAILED_NO_MATCHING_ABIS` — your emulator is **x86**; recreate the AVD
  with an **arm64-v8a** image (the game has no x86 build).
- `adb devices` empty — `adb` not on PATH, or the emulator hasn't finished booting.
- Emulator crashes / black screen — start it with `-gpu swiftshader_indirect`; if it
  hangs after rapid taps, close and relaunch it.
- Painfully slow on Intel/AMD — expected (arm64 is emulated there). Use it for
  correctness checks, or **Build APK** and install on a real phone instead.

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
