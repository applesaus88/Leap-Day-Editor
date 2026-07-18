# Third-party notices & legal model

## What this project is
Leap Day Mod Studio is a **level editor / patcher**. It ships **no Leap Day code
or assets**. "Leap Day" is © Nitrome Ltd; all game code, art, audio, levels, and
metadata belong to Nitrome. To produce a modded build, **the user supplies their
own copy of the game** (`leap-day-*.xapk`); the tool applies the user's authored
content (`.ldmod`, which contains only user-created level data) to that copy on
the user's own machine. Same model as a ROM-hack patch.

Do not redistribute any file produced under `build/`, `dist/.../*.apk`, or any
extracted `data.unity3d` / `libil2cpp.so` / `global-metadata.dat` / `*.apk` /
`*.xapk` — those contain Nitrome's copyrighted bytes. See `.gitignore`.

## Bundled / used third-party components

### uber-apk-signer  (vendor/uber-apk-signer.jar)
- Author: Patrick Favre-Bulle — https://github.com/patrickfav/uber-apk-signer
- License: Apache License 2.0
- Use: signs the rebuilt APK splits. Fetched by CI at build time.

### UnityPy
- https://github.com/K0lb3/UnityPy
- License: MIT
- Use: read/write the game's text level chunks inside `data.unity3d`.

### Eclipse Temurin (OpenJDK) JRE
- https://adoptium.net — License: GPLv2 with the Classpath Exception (GPLv2+CE)
- Use: a trimmed JRE is bundled alongside the CI-built patcher solely to run the
  signer, so end users need no separate Java install. Source availability and the
  Classpath Exception per the Temurin/OpenJDK distribution apply.

### Android binary-XML / APK handling
- `core/axml.py` is an original implementation (no third-party code) based on the
  public AOSP `ResourceTypes.h` binary-XML format.

This project's own source is provided under its repository license (add a
`LICENSE` file to state your chosen terms, e.g. MIT).
