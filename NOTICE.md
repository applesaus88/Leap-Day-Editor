# Third-party notices & legal model

## What this project is
Leap Day Editor is a **level editor / patcher**. It ships **no Leap Day code or
assets**. "Leap Day" is © Nitrome Ltd; all game code, art, audio, levels, and
metadata belong to Nitrome. This project is **unofficial** and is **not affiliated
with, authorized by, or endorsed by Nitrome**.

To produce a modded build, **the user supplies their own copy of the game**
(`leap-day-*.xapk`); the tool applies the user's authored content (`.ldmod`, which
contains only user-created level data) to that copy on the user's own machine.
Same model as a ROM-hack patch.

## Which game version
The patches are version-specific. This tool targets **Leap Day `1.142.2`**,
distributed as **`leap-day-1-142-2.xapk`**. The editor refuses to load any other
game or version. A different build won't work and isn't supported.

## What you MAY share
- **`.ldmod` files** — yes. A `.ldmod` holds **only your own edited level content**
  (your layouts, enemy/hazard placement, difficulty, paths). It contains **no
  original Nitrome assets or game code**, so you are free to share and distribute
  it. Whoever plays it must bring their own copy of the game.
- **This tool's own source code** — yes, under its MIT License (see `LICENSE`).

## What you MUST NOT share
Do not redistribute any file produced under `build/`, `dist/.../*.apk`, or any
extracted `data.unity3d` / `libil2cpp.so` / `global-metadata.dat` / `*.apk` /
`*.xapk` — those contain Nitrome's copyrighted bytes. See `.gitignore`.

## The native library / libmain patch
The repo ships a prebuilt native library (`core/native/prebuilt/libnativemod-*.so`,
original code, MIT) and a **libmain diff** (`libmain_needed-*.bin`). The `.bin` is a
**pure delta** — a list of byte spans to add a `DT_NEEDED` entry so the game loads
the native library. It does **not** embed Nitrome's original `libmain.so` bytes; it
is applied at build time to the user's own copy of the game.

## Bundled / used third-party components
The downloadable builds bundle a Python runtime, a Java runtime, and several
open-source libraries so end users need nothing pre-installed. The full component
list, licenses, and source links are in **`THIRD_PARTY_NOTICES.txt`**, which also
ships inside every release build. Key items:

- **Eclipse Temurin (OpenJDK) JRE** — GPLv2 with the Classpath Exception; bundled
  only to run the signer so users need no separate Java install.
- **uber-apk-signer** (`vendor/uber-apk-signer.jar`) — Apache-2.0; signs the
  rebuilt APK splits.
- **UnityPy** (MIT) — reads/writes the game's text level chunks and decodes sprites.
- **Pillow** (HPND), **pywebview** (BSD-3), **TypeTreeGeneratorAPI** (MIT, bundles
  Capstone BSD-3), **NumPy** (BSD-3), **LIEF** (Apache-2.0), texture decoders.
- **FMOD Engine** — proprietary (© Firelight Technologies), pulled in transitively
  by UnityPy's audio path and bundled only so imports succeed; this tool does not
  use audio. Subject to the FMOD EULA (https://www.fmod.com/legal).

`core/axml.py` is an original implementation (no third-party code) based on the
public AOSP `ResourceTypes.h` binary-XML format.
