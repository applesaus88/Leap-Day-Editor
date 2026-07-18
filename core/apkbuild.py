"""
apkbuild.py — rebuild, sign, and install the split APKs.

Pipeline:
  1. replace_entry()  : copy an APK, swapping one inner file (preserving the
                        original per-entry compression — important: data.unity3d
                        is STORED and Unity mmaps it, so it must stay STORED).
  2. sign_all()       : sign every split with ONE keystore (split APKs must all
                        carry the same certificate or the install is rejected).
                        Uses the bundled uber-apk-signer.jar (Java only).
  3. install()        : adb install-multiple of the signed splits.

No Android SDK build-tools required — only Java + adb.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile


def _resource(*parts) -> str:
    """Resolve a bundled resource both when running from source and when frozen
    into a PyInstaller one-file/one-dir executable (datas land in _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..")))
    return os.path.join(base, *parts)


def _java() -> str:
    """Prefer a JRE bundled next to the app, else $LEAPDAY_JAVA, else PATH."""
    exe = "java.exe" if os.name == "nt" else "java"
    bundled = _resource("jre", "bin", exe)
    if os.path.exists(bundled):
        return bundled
    return os.environ.get("LEAPDAY_JAVA", shutil.which("java") or "java")


SIGNER_JAR = _resource("vendor", "uber-apk-signer.jar")


def _find_adb() -> str:
    """Prefer the Android SDK's adb (matches the SDK emulator's adb server).
    A mismatched adb client (e.g. an old /etc copy on PATH) keeps killing and
    restarting the shared adb server, which transiently drops the emulator and
    makes install/launch flaky."""
    cands = [
        os.environ.get("ADB"),
        os.path.join(os.environ.get("ANDROID_HOME", ""), "platform-tools", "adb"),
        os.path.join(os.environ.get("ANDROID_SDK_ROOT", ""), "platform-tools", "adb"),
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        shutil.which("adb"),
        "/etc/platform-tools/adb",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return "adb"


ADB = _find_adb()


def replace_entry(src_apk: str, out_apk: str, entry: str, new_bytes: bytes) -> None:
    """Rewrite src_apk -> out_apk, replacing `entry` with new_bytes.

    Every other entry is copied verbatim with its original compression type so
    we don't accidentally re-compress STORED assets the engine mmaps.
    """
    with zipfile.ZipFile(src_apk, "r") as zin:
        infos = zin.infolist()
        names = {i.filename for i in infos}
        if entry not in names:
            raise KeyError(f"{entry!r} not found in {src_apk}")
        # write to a temp then move into place
        tmp = out_apk + ".tmp"
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                data = new_bytes if info.filename == entry else zin.read(info.filename)
                # preserve compression + (for STORED) alignment-relevant flags
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                zi.internal_attr = info.internal_attr
                zi.create_system = info.create_system
                zout.writestr(zi, data)
    os.replace(tmp, out_apk)


def sign_all(apk_paths: list[str], out_dir: str) -> list[str]:
    """Sign every split with uber-apk-signer's built-in debug key.

    Using the tool's default debug keystore (no --ks) means all splits get the
    same, deterministic certificate — so they install together — and we need
    only a JRE (no keytool/JDK to generate a keystore).
    """
    os.makedirs(out_dir, exist_ok=True)
    signed = []
    for apk in apk_paths:
        staged = os.path.join(out_dir, os.path.basename(apk))
        if os.path.abspath(staged) != os.path.abspath(apk):
            shutil.copy2(apk, staged)
        signed.append(staged)
    subprocess.run(
        [_java(), "-jar", SIGNER_JAR, "-a", out_dir,
         "--overwrite", "--allowResign"],
        check=True,
    )
    return signed


def adb_devices() -> list[str]:
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout
    return [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]


def _emulator_bin() -> str | None:
    exe = "emulator.exe" if os.name == "nt" else "emulator"
    for base in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.expanduser("~/Library/Android/sdk"),
                 os.path.expanduser("~/Android/Sdk")):
        if base:
            p = os.path.join(base, "emulator", exe)
            if os.path.exists(p):
                return p
    return shutil.which("emulator")


def list_avds() -> list[str]:
    emu = _emulator_bin()
    if not emu:
        return []
    try:
        out = subprocess.run([emu, "-list-avds"], capture_output=True, text=True, timeout=20).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


def start_emulator(name: str | None = None) -> bool:
    """Launch an AVD detached (with a window so you can play). Prefers an AVD
    named 'leapday', else the first available. swiftshader renderer = reliable
    display on Apple Silicon."""
    emu = _emulator_bin()
    avds = list_avds()
    if not emu or not avds:
        return False
    name = name or ("leapday" if "leapday" in avds else avds[0])
    try:
        subprocess.Popen([emu, "-avd", name, "-gpu", "swiftshader_indirect", "-no-boot-anim"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def wait_for_boot(timeout: int = 180) -> bool:
    import time
    try:
        subprocess.run([ADB, "wait-for-device"], timeout=timeout)
    except Exception:
        pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            out = subprocess.run([ADB, "shell", "getprop", "sys.boot_completed"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            out = ""
        if out == "1":
            return True
        time.sleep(2)
    return False


def ensure_device(timeout: int = 180, log=print) -> bool:
    """Return True if a device is available, auto-starting an emulator if not."""
    if adb_devices():
        return True
    log("[emu] no emulator running — starting one…")
    if not start_emulator():
        return False
    log("[emu] booting emulator (first boot can take ~1 min)…")
    return wait_for_boot(timeout) and bool(adb_devices())


PKG = "com.nitrome.leapday"
ACTIVITY = f"{PKG}/.ExtendedUnityActivity"


def install(apk_paths: list[str], keep_data: bool = False) -> None:
    # keep_data: reinstall over the top (-r) without uninstalling — faster, and
    # keeps the privacy prompt accepted across playtests.
    if not keep_data:
        subprocess.run([ADB, "uninstall", PKG], capture_output=True, text=True)
    subprocess.run([ADB, "install-multiple", "-r", *apk_paths], check=True)


def force_stop() -> None:
    subprocess.run([ADB, "shell", "am", "force-stop", PKG], capture_output=True, text=True)


def launch() -> None:
    """Cold-launch the game (force-stop first so a modded reinstall restarts)."""
    force_stop()
    subprocess.run([ADB, "shell", "am", "start", "-n", ACTIVITY],
                   capture_output=True, text=True)


