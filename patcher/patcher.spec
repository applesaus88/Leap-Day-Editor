# PyInstaller spec for the Leap Day Mod Patcher (one-file executable).
# Build from the project root:  pyinstaller patcher/patcher.spec
# Produces LeapDayModPatcher.exe on Windows and LeapDayModPatcher.app on macOS.
import os
import sys
from PyInstaller.utils.hooks import collect_all
from PyInstaller.building.datastruct import Tree

ROOT = os.path.abspath(os.getcwd())
IS_MAC = sys.platform == "darwin"

import glob as _glob
datas, binaries, hiddenimports = [], [], []
# UnityPy (text-level editing) + TypeTreeGeneratorAPI + lief. modbuild now
# generates a Level/MonoBehaviour type tree for ANY mod that carries custom levels
# (cactus / allow-all-elements / ordered-list / firebar / element / flag-checkpoint
# edits) and embeds a native libnativemod.so (checkpoint fixes / enemy tuning) via
# lief. The old "text-only, no TypeTreeGeneratorAPI/lief" assumption was stale and
# left the frozen app crashing on import (ModuleNotFoundError: TypeTreeGeneratorAPI)
# and, once past that, failing at the native-embed step.
for pkg in ("UnityPy", "TypeTreeGeneratorAPI", "lief"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# TypeTreeGeneratorAPI loads its native lib by a path RELATIVE TO ITS __file__
# (os.path.dirname(realpath(__file__)) + "/libTypeTreeGeneratorAPI.<ext>"), NOT the
# app root where PyInstaller drops binaries. So copy every native lib in the package
# dir in as DATAS under "TypeTreeGeneratorAPI/" so ctypes finds them when frozen.
# Globbed (not hard-coded) so it works on macOS (.dylib), Windows (.dll) and Linux
# (.so) from whichever wheel CI installed for that runner.
import TypeTreeGeneratorAPI as _ttg_mod
_ttg = os.path.dirname(_ttg_mod.__file__)
for _ext in ("*.dylib", "*.so", "*.dll", "*.pyd"):
    for _p in _glob.glob(os.path.join(_ttg, _ext)):
        datas += [(_p, "TypeTreeGeneratorAPI")]
hiddenimports += ["TypeTreeGeneratorAPI", "TypeTreeGeneratorAPI.TypeTreeGenerator"]

# our engine + the signer jar. core.nativemod is imported lazily inside
# modbuild.build (from . import nativemod), which PyInstaller's static analysis can
# miss — list every core module the build path touches so none is dropped.
hiddenimports += ["core.bundle", "core.chunkfmt", "core.apkbuild", "core.modbuild",
                  "core.project", "core.sopatch", "core.typetree", "core.axml",
                  "core.nativemod", "core.override", "core.dayorder"]
datas += [(os.path.join(ROOT, "vendor", "uber-apk-signer.jar"), "vendor")]

# the native mod: nativemod.c (its SHA-1 keys the prebuilt lookup) + the prebuilt
# libnativemod-<hash>.so (+ -dbg + libmain-needed .bin) so end users never invoke
# the NDK. Bundled at "core/native/…" to match core/nativemod.py's __file__-relative
# paths. Every mod with custom levels embeds this lib (checkpoint renumber/fix), so
# it's required for essentially any content mod — not optional.
_native = os.path.join(ROOT, "core", "native")
for _f in ("nativemod.c", "config.h", "shoot_bakes.json"):
    _p = os.path.join(_native, _f)
    if os.path.exists(_p):
        datas += [(_p, os.path.join("core", "native"))]
for _p in _glob.glob(os.path.join(_native, "prebuilt", "*")):
    if os.path.isfile(_p):
        datas += [(_p, os.path.join("core", "native", "prebuilt"))]

# third-party license notices must accompany the distributed binary (GPL JRE,
# Apache signer, etc.) — bundle at the app root so it ships with every download
_notices = os.path.join(ROOT, "THIRD_PARTY_NOTICES.txt")
if os.path.exists(_notices):
    datas += [(_notices, ".")]

# optional bundled JRE (CI drops a trimmed runtime at ./jre before building) so
# the user needs no Java install. Omitted from source/mac test builds.
tree_extra = []
if os.path.isdir(os.path.join(ROOT, "jre")):
    tree_extra.append(Tree(os.path.join(ROOT, "jre"), prefix="jre"))

# Heavy libs installed in the dev environment that the patcher never uses.
# Without excluding them, dependency analysis sweeps them in and bloats the
# one-file exe by ~130 MB (torch alone is huge). The patcher only does text-level
# level editing via UnityPy.
EXCLUDES = [
    "torch", "torchvision", "torchaudio",
    "scipy", "pandas", "matplotlib",
    "numba", "llvmlite",
    "frida", "frida_tools", "_frida",
    "h5py", "sympy",
    "tensorflow", "sklearn", "scikit_learn", "cv2",
    "IPython", "notebook", "jupyter", "jupyter_core",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "pytest",
    # NOTE: do NOT exclude tkinter here — the patcher's GUI is Tkinter.
]

a = Analysis(
    [os.path.join(ROOT, "patcher", "patcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, *tree_extra,
    name="LeapDayModPatcher",
    console=False,            # windowed app (Tkinter)
    disable_windowed_traceback=False,
    upx=False,
)
if IS_MAC:
    app = BUNDLE(
        exe,
        name="LeapDayModPatcher.app",
        icon=None,
        bundle_identifier="com.leapdaymod.patcher",
        info_plist={"NSHighResolutionCapable": True},
    )
