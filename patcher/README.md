# Leap Day Mod Patcher (standalone)

A self-contained app that turns a **mod file** (`.ldmod`) + **your own copy of
the game** (`.xapk`/`.apk`) into a signed, installable modded APK. No Python,
no Java, no Android SDK required by the end user — everything is bundled.

Because the `.ldmod` holds only authored content, the patcher + a mod file
contain **no game data**: each user supplies their own game.

## Using it
1. Launch **LeapDayModPatcher** (`.exe` on Windows, `.app` on macOS).
2. Pick the **mod file** (`.ldmod`).
3. Pick your **game package** (`leap-day-*.xapk`).
4. Options:
   - **Install alongside the original game** (on by default) — renames the build
     to its own package id (derived from the mod name, e.g.
     `com.nitrome.leapday.<mod>`) and gives it a distinct launcher name, so it
     installs as a **separate app next to** the stock Leap Day instead of
     replacing it. Turn off to build a straight replacement (same package).
   - **Remove ads / billing / Play-store connections** (on by default) — strips
     the ad/billing/Play components from the manifest so those SDKs don't
     auto-initialise.
5. Choose an output folder and click **Build modded APK**.
6. Install the result on an emulator/phone:
   ```
   adb install-multiple -r com.nitrome.leapday.apk config.arm64_v8a.apk
   ```
   (Or tick *Install to connected device* if you have `adb` on PATH.) With
   *install alongside* on, this does **not** touch your original game.

> The package rename + store strip are applied to the binary `AndroidManifest`
> (no apktool). It keeps every component **class** name, so the unchanged game
> code still runs — but it's a real device change: **test the build on your
> phone/emulator**. If a strip breaks launch, rebuild with the store strip off.

The game ships **arm64/armv7 only** (no x86), so use an arm64 emulator
(Android Studio AVD with an `arm64-v8a` image on Apple Silicon, etc.).

macOS note: the app is unsigned, so the first launch needs **right-click → Open**
to get past Gatekeeper.

## Getting the builds
- **From CI (recommended):** the GitHub Actions workflow
  `.github/workflows/build-patcher.yml` builds both the Windows `.exe` and the
  macOS `.app` (each with a bundled JRE) and uploads them as artifacts. Trigger
  it from the Actions tab.
- **Locally:** from the project root,
  ```
  pip install -r patcher/requirements.txt
  pyinstaller patcher/patcher.spec
  ```
  Output lands in `dist/`. A locally built app uses your system Java for signing
  (the CI builds bundle a JRE so other machines need none).

## CLI (for testing / scripting)
```
LeapDayModPatcher --mod my.ldmod --xapk leap-day.xapk --out ./out [--install]
```
