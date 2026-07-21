# Leap Day Mod Studio

Alpha. Most things work (~95%), and I'm still smoothing out the rest.

A desktop level editor for the mobile game Leap Day. You build custom levels and
share them without ever handing out the game itself.

By default it runs in Safe mode, where you only edit levels: paint tiles, drop
enemies and hazards, and set each chunk's difficulty. You can't break anything.

Turn on Power mode (a toolbar toggle, off by default) for the advanced tools:
theme changes, per-enemy tuning, checkpoints, and other patches. Those only touch
the copy of the game on your own machine, and they're never part of what you share.

## How sharing stays legal

What you share is a `.ldmod` file. It holds only your level content, never any
game code or assets. To play a mod, you bring your own copy of the game; the studio
applies the `.ldmod` on your machine and produces a signed, installable APK. Same
idea as a ROM-hack patch.

You need the exact game file **`leap-day-1-142-2.xapk`**. The patches target that
specific version, so a different build won't work. Supply your own copy (it isn't,
and can't legally be, included here).

## Download (Windows and macOS)

The easy way. Grab a ready-to-run build from the
[Releases page](https://github.com/applesaus88/Leap-Day-Editor/releases) — no
Python, no Java, no NDK to install.

- Windows: download `LeapDayModStudio.exe` and double-click it. The first launch
  may show a blue "Windows protected your PC" box (unknown publisher) — click
  **More info → Run anyway**.
- macOS: download `LeapDayModStudio-macos.zip`, unzip it, and move
  `LeapDayModStudio.app` to Applications. The first launch is blocked because it's
  unsigned — **right-click the app → Open**, then confirm. After that it opens
  normally.

Builds go to a `LeapDayModStudio` folder in your home directory, not inside the
app, so nothing gets locked up.

Linux users (and anyone who'd rather run the source) — see Install below.

## Install (run from source)

You need Python 3.10 or newer. Then:

```
python3 -m pip install -r requirements.txt
```

That installs UnityPy, pywebview, Pillow, and TypeTreeGeneratorAPI.
TypeTreeGeneratorAPI is read-only; it only helps find sprites and never changes
gameplay.

The GUI needs a webview backend, which depends on your OS:

- macOS: nothing to install, it uses the built-in WebKit.
- Windows: the Edge WebView2 runtime. It ships with Windows 11; on Windows 10 you
  can grab the free "Evergreen" runtime from Microsoft.
- Linux: `sudo apt install python3-gi gir1.2-webkit2-4.1`, then
  `pip install pycairo PyGObject`.

To build an APK you also need:

- Java 17 or newer, for signing. The signer itself is included, nothing to download.
- To install to a device: `adb`, plus an arm64 emulator or phone. The game is
  arm64/armv7 only, so x86 won't work.

That's it for a normal build. The repo already ships the prebuilt native library
and its libmain patch, so you do **not** need the Android NDK or LIEF. You only
need the NDK if you change the native source (`core/native/nativemod.c`); after
that, rebuild the prebuilt with:

```
python3 core/native/build_prebuilt.py <game.xapk>
```

## Disk space

The clone itself is tiny, about 4 MB. But a build unpacks the game and makes several
working copies, so the folder balloons while it runs — figure 500–650 MB for one
build's scratch and output. Plan for roughly **2 GB free** to edit and build
comfortably (clone + Python deps ~150 MB + your game file ~150 MB + build scratch).

## Run it

```
python3 studio/app.py
```

The flow: Load Game (.xapk), pick a chunk, paint tiles and place enemies, set the
difficulty, then Save level → mod. Hit Build APK, or Build + Install if a device is
connected. Use Save .ldmod to save your project.

No GUI? Build from the command line:

```
python3 studio/cli.py --xapk LEAPDAY.xapk --mod my.ldmod --install
```

## Playtesting on a PC (Android emulator)

You only need this to install and playtest on your computer. If you just want to
make a mod, skip it and use Build APK to get an APK you can copy to a phone.

The game is arm64 only, so the emulator has to be arm64-v8a. On Apple Silicon Macs
that runs natively and is fast. On Intel and AMD machines (most Windows and Linux
PCs) an arm64 emulator has to be emulated, so it works but it's slow.

**Windows and Linux users on Intel/AMD: use a real phone instead.** Your CPU can
only speed up x86 emulator images, and the game has no x86 build, so an arm64
emulator crawls. The easy path is to click Build APK, copy the `.apk` to an Android
phone, and install it there (turn on "Install unknown apps" first). Only set up the
arm64 emulator if you have no phone and want the occasional test on your PC.

Setup:

1. **Get the SDK tools.** Install Android Studio; it comes with the SDK, the
   emulator, and `adb`. Put `adb` and `emulator` on your PATH. They live under the
   SDK, for example `~/Library/Android/sdk/platform-tools` and `.../emulator` on
   macOS, or `%LOCALAPPDATA%\Android\Sdk\...` on Windows.

2. **Make an arm64 AVD.** In Android Studio, open Device Manager, create a device,
   pick a phone like the Pixel 5, and choose a system image with the arm64-v8a ABI.
   Name it `leapday`. Or do it from the command line:

   ```
   sdkmanager "platform-tools" "emulator" "system-images;android-30;google_apis;arm64-v8a"
   avdmanager create avd -n leapday -k "system-images;android-30;google_apis;arm64-v8a" -d pixel_5
   ```

   API 30 (Android 11) is a safe choice.

3. **Start it.**

   ```
   emulator -avd leapday -gpu swiftshader_indirect
   ```

   The `-gpu swiftshader_indirect` flag avoids graphics glitches. You can drop it if
   your GPU handles the emulator fine.

4. **Check the connection.**

   ```
   adb devices
   ```

   You want a line like `emulator-5554   device`. If it says `offline`, the emulator
   is still booting. If nothing shows up, run `adb kill-server && adb start-server`.

5. **Install.** With the emulator listed in `adb devices`, click Build + Install in
   the studio, or add `--install` to the `cli.py` command. It signs the APK and
   installs it on the emulator.

If something goes wrong:

- `INSTALL_FAILED_NO_MATCHING_ABIS`: your emulator is x86. Remake the AVD with an
  arm64-v8a image.
- `adb devices` is empty: `adb` isn't on your PATH, or the emulator hasn't finished
  booting.
- Black screen or crash: start with `-gpu swiftshader_indirect`, and if it hangs
  after fast tapping, close and reopen it.
- Very slow on Intel/AMD: that's expected. Use it for quick checks, or install on a
  real phone instead.

## Building a standalone yourself

The Windows/macOS downloads are built automatically by GitHub Actions
(`.github/workflows/build-editor.yml`) whenever a `v*` tag is pushed. To make one
locally instead:

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm studio/editor.spec
```

The result lands in `dist/` — `LeapDayModStudio.app` on macOS, a single
`LeapDayModStudio.exe` on Windows. PyInstaller only builds for the OS you run it
on. Java isn't bundled in a local build (CI adds it), so APK signing still needs
Java installed when you run a hand-built copy.

## What you can edit

Overwrite any of the game's level chunks with your own layout: a 14-wide grid of
tiles, enemies, hazards, and paths, plus the chunk's difficulty rating (which pool
the level lands in, not a physics change). Your levels drop into the daily rotation
automatically.

## Project layout

```
core/    the engine: bundle (UnityPy asset edits), chunkfmt (level XML),
         sprites (palette art), apkbuild (rebuild/sign/install),
         modbuild (orchestration), project (.ldmod)
studio/  the desktop app: app.py (pywebview shell) + ui/ (canvas editor), plus cli.py
patcher/ standalone patcher app (Tkinter) + PyInstaller spec
tiles/   catalog.json, the tile/enemy palette scraped from the game
vendor/  uber-apk-signer.jar
```

## AI disclosure

Claude has been used to debug code. 95% of the code is written by a human. Claude
did help a lot with writing/translating the documentation and the comments since
English isn't my first language.
