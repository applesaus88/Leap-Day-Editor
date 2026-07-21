# PyInstaller spec for Leap Day Mod Studio — the full editor (GUI).
# Build from the project root:
#     pyinstaller --noconfirm studio/editor.spec
# Produces LeapDayModStudio.exe on Windows, LeapDayModStudio.app on macOS, and a
# LeapDayModStudio one-file binary on Linux. Bundles Python + every dependency so
# the end user just downloads and runs — no Python, no pip, no NDK.
#
# What ships inside:
#   - the editor engine (core/*), UI (studio/ui/*), tile data (tiles/*)
#   - the prebuilt native tuning lib + libmain diff (core/native/prebuilt/*) so
#     builds work without the Android NDK
#   - the APK signer jar (vendor/uber-apk-signer.jar); Java is still required to
#     sign — CI drops a trimmed JRE at ./jre and it gets bundled too
#   - UnityPy, pywebview (native webview), Pillow, TypeTreeGeneratorAPI (a ctypes
#     dylib/so/dll — no .NET runtime needed)
import os
import sys
from PyInstaller.utils.hooks import collect_all
from PyInstaller.building.datastruct import Tree

ROOT = os.path.abspath(os.getcwd())
IS_MAC = sys.platform == "darwin"

datas, binaries, hiddenimports = [], [], []

# third-party packages that carry their own data / native libs.
# NOTE on FMOD: UnityPy's export/__init__ imports AudioClipConverter, whose only
# module-level need is `import fmod_toolkit` (proprietary FMOD). We never decode
# audio, so instead of bundling FMOD we stub that import via a runtime hook
# (rthook_no_fmod.py) and exclude fmod_toolkit/pyfmodex below — no FMOD binary ships.
# archspec: astc_encoder calls archspec.cpu.host() at import time, which reads a
# bundled JSON (microarchitectures.json). Without its data files that read fails
# and, again, texture decoding dies -> blank sprites.
for pkg in ("UnityPy", "webview", "PIL", "TypeTreeGeneratorAPI", "archspec",
            "texture2ddecoder", "etcpak", "astc_encoder"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# our engine — pull in every core module (some are imported lazily at build time)
hiddenimports += [
    "core.apkbuild", "core.axml", "core.bundle", "core.chunkfmt", "core.dayorder",
    "core.elements", "core.firebar", "core.modbuild", "core.nativemod",
    "core.override", "core.playtest", "core.project", "core.sopatch",
    "core.sprites", "core.typetree",
]

# the signer jar (Apache-2.0) — required to sign built APKs
datas += [(os.path.join(ROOT, "vendor", "uber-apk-signer.jar"), "vendor")]

# third-party license notices must accompany the distributed binary (GPL JRE,
# Apache signer, etc.) — bundle at the app root so it ships with every download
_notices = os.path.join(ROOT, "THIRD_PARTY_NOTICES.txt")
if os.path.exists(_notices):
    datas += [(_notices, ".")]

# read-only resource trees, placed at the same relative paths app.py expects
trees = [
    Tree(os.path.join(ROOT, "tiles"), prefix="tiles", excludes=["*.bak", "*.bak2", "*.pyc"]),
    Tree(os.path.join(ROOT, "studio", "ui"), prefix="studio/ui"),
    # whole native dir: nativemod.c (its SHA-1 keys the prebuilt lookup), config.h,
    # shoot_bakes.json, and prebuilt/*.so + *.bin — so builds use the shipped
    # prebuilt with no NDK. Source files are small (~350 KB total).
    Tree(os.path.join(ROOT, "core", "native"), prefix="core/native",
         excludes=["*.DS_Store", "__pycache__", "*.pyc"]),
]
# captured by-date chunk index the editor reads (so downloaded builds get by-date
# editing). Bundle ONLY the two JSONs the app loads — not the capture script or
# per-day intermediates. Editor degrades gracefully if they're absent.
_jan = os.path.join(ROOT, "tools", "january_chunks")
for _f in ("january_index.json", "theme_index.json"):
    _p = os.path.join(_jan, _f)
    if os.path.exists(_p):
        datas += [(_p, "tools/january_chunks")]

# optional bundled JRE (CI drops a trimmed runtime at ./jre) so the user needs no
# Java install. Omitted from local/source test builds — the app still runs; only
# APK signing needs Java.
if os.path.isdir(os.path.join(ROOT, "jre")):
    trees.append(Tree(os.path.join(ROOT, "jre"), prefix="jre"))

# Heavy libraries that happen to be installed in the dev environment but the
# editor never uses. collect_all / dependency analysis would otherwise sweep them
# in and balloon the download by ~450 MB (torch, llvmlite, scipy, ...). None of
# these are imports of UnityPy / PIL / pywebview / TypeTreeGeneratorAPI / core.
EXCLUDES = [
    "torch", "torchvision", "torchaudio",
    "scipy", "pandas", "matplotlib",
    "numba", "llvmlite",
    "frida", "frida_tools", "_frida",
    "h5py", "sympy",
    "tensorflow", "sklearn", "scikit_learn", "cv2",
    "IPython", "notebook", "jupyter", "jupyter_core",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "tkinter", "pytest",
    # proprietary FMOD, pulled in by UnityPy's audio converter but never used —
    # stubbed at runtime (rthook_no_fmod.py) so nothing proprietary is shipped.
    "fmod_toolkit", "pyfmodex",
]

a = Analysis(
    [os.path.join(ROOT, "studio", "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[os.path.join(ROOT, "studio", "rthook_no_fmod.py")],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
if IS_MAC:
    # macOS: onedir collected into a proper .app bundle. One-file + .app is
    # deprecated in PyInstaller and clashes with Gatekeeper, so use onedir here.
    exe = EXE(
        pyz, a.scripts,
        exclude_binaries=True,
        name="LeapDayModStudio",
        console=False,        # windowed GUI (pywebview)
        disable_windowed_traceback=False,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, *trees, name="LeapDayModStudio", upx=False)
    app = BUNDLE(
        coll,
        name="LeapDayModStudio.app",
        icon=None,
        bundle_identifier="com.leapdaymod.studio",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
else:
    # Windows / Linux: a single-file binary so the user downloads exactly one file.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, *trees,
        name="LeapDayModStudio",
        console=False,        # windowed GUI (pywebview)
        disable_windowed_traceback=False,
        upx=False,
    )
