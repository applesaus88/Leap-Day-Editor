# PyInstaller spec for the Leap Day Mod Patcher (one-file executable).
# Build from the project root:  pyinstaller patcher/patcher.spec
# Produces LeapDayModPatcher.exe on Windows and LeapDayModPatcher.app on macOS.
import os
import sys
from PyInstaller.utils.hooks import collect_all
from PyInstaller.building.datastruct import Tree

ROOT = os.path.abspath(os.getcwd())
IS_MAC = sys.platform == "darwin"

datas, binaries, hiddenimports = [], [], []
# only UnityPy (text-level editing). No capstone / TypeTreeGeneratorAPI: the
# tool has no code path to disassemble or edit compiled code / serialized fields.
for pkg in ("UnityPy",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# our engine + the signer jar
hiddenimports += ["core.bundle", "core.chunkfmt",
                  "core.apkbuild", "core.modbuild", "core.project"]
datas += [(os.path.join(ROOT, "vendor", "uber-apk-signer.jar"), "vendor")]

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
