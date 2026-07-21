#!/usr/bin/env python3
"""Build the release libnativemod.so and cache it in core/native/prebuilt/.

Attach the resulting file to a GitHub Release so end users can build mods without
installing the Android NDK -- they just drop it into core/native/prebuilt/.

Usage:
    python3 core/native/build_prebuilt.py <game.xapk | config.arm64_v8a.apk | libil2cpp.so>

You need one of:
  - the game .xapk           (the script pulls the arm64 split + libil2cpp.so out)
  - the config.arm64_v8a.apk (the arm64 split on its own)
  - libil2cpp.so             (already extracted from lib/arm64-v8a/)

The native lib links against the game's libil2cpp.so so its il2cpp_* symbols
resolve, which is why one of these is required.

Requires the Android NDK (Android Studio -> SDK Manager -> NDK, or set
ANDROID_NDK_HOME). The output filename embeds the SHA-1 of
core/native/nativemod.c, so rebuild and re-upload whenever the native source
changes, and release the .so from the same commit as the source.

Already cached? It returns the existing file. Delete core/native/prebuilt/ first
to force a fresh compile.
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core import nativemod, modbuild


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 else 2)
    src = os.path.abspath(sys.argv[1])
    if not os.path.exists(src):
        sys.exit(f"not found: {src}")

    workdir = tempfile.mkdtemp(prefix="ldprebuilt-")
    low = src.lower()
    if low.endswith(".so"):
        # pre-place libil2cpp.so so get_prebuilt_so links against it directly
        shutil.copyfile(src, os.path.join(workdir, "libil2cpp.so"))
        arm64_in = src  # unused once libil2cpp.so is present
    elif low.endswith(".xapk"):
        apks = modbuild.unpack_xapk(src, os.path.join(workdir, "xapk"))
        arm64_in = apks.get(modbuild.ARM64_APK)
        if not arm64_in:
            sys.exit(f"{modbuild.ARM64_APK} not found inside the xapk")
    else:
        # assume the arm64 split apk (a zip with lib/arm64-v8a/libil2cpp.so)
        arm64_in = src

    so = nativemod.get_prebuilt_so(arm64_in, workdir, debug=False, log=print)
    print()
    print("Built release prebuilt:")
    print(f"  {so}")
    print(f"  {os.path.getsize(so)} bytes")
    print()
    print("Attach this file to a GitHub Release. Users drop it into")
    print("core/native/prebuilt/ (keep the filename) to build without the NDK.")


if __name__ == "__main__":
    main()
