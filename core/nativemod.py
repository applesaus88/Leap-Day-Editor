"""
nativemod.py — build & embed the per-individual-enemy tuning library.

The studio can tune ONE placed enemy at a time (projectile / health / walk
speed), keyed by (chunk, sx, sy). Those tunings can't be baked into the level
text (the game spawns every enemy of a type from one shared prefab), so instead
we ship a tiny native library, `libnativemod.so`, that the game loads itself:

    project.enemy_tuning  ->  config.h  ->  clang (NDK)  ->  libnativemod.so
                          embedded as a DT_NEEDED on libmain.so in the arm64 split

At runtime the library waits for il2cpp, enumerates live enemies, keys each to
its editor placement, and writes the tuned fields on that instance only. No root,
no Frida — it's just another one of the app's own libraries. See
core/native/nativemod.c and the project-native-il2cpp-modloader memory.

This module is only invoked when a project actually has `enemy_tuning`; the NDK
and LIEF are soft dependencies (a clear error is raised if a tuning build is
requested without them).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import struct
import subprocess
import zipfile

from .chunkfmt import Chunk

_HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE_DIR = os.path.join(_HERE, "native")
NATIVE_SRC = os.path.join(NATIVE_DIR, "nativemod.c")
# Global dev-authored baseline launch speeds, keyed "class|projectile" -> speed.
SHOOT_BAKES_FILE = os.path.join(NATIVE_DIR, "shoot_bakes.json")

# Enemy runtime classes whose launch speed the mod can set via a real field wired in
# nativemod.c's g_tc — the ones a dev-baked absolute speed makes sense for. (Shooters
# without a speed field are handled by the universal projectile-velocity scaler, which
# doesn't use per-class bakes.)
SHOOT_CLASSES = ["WoolyTrunky", "BigWoolyTrunky", "Cupid", "Asteroid"]


def load_shoot_bakes() -> dict[str, float]:
    """Read the dev-baked (class, projectile) -> baseline launch-speed table."""
    try:
        with open(SHOOT_BAKES_FILE) as fh:
            data = json.load(fh)
        return {str(k): float(v) for k, v in data.items() if v not in (None, "")}
    except (OSError, ValueError):
        return {}


def save_shoot_bakes(bakes: dict[str, float]) -> None:
    """Persist the dev-baked launch-speed table (drops blank/None entries)."""
    clean = {str(k): float(v) for k, v in bakes.items()
             if v not in (None, "") and "|" in str(k)}
    with open(SHOOT_BAKES_FILE, "w") as fh:
        json.dump(clean, fh, indent=2, sort_keys=True)

ARM64_LIBDIR = "lib/arm64-v8a"
LIBMAIN = f"{ARM64_LIBDIR}/libmain.so"
LIBIL2CPP = f"{ARM64_LIBDIR}/libil2cpp.so"
LIBNATIVE = f"{ARM64_LIBDIR}/libnativemod.so"


# --------------------------------------------------------------------------- #
# toolchain discovery
# --------------------------------------------------------------------------- #
def find_ndk_clang() -> str | None:
    """Locate an arm64 Android clang from the NDK. Honours env overrides, else
    scans the standard SDK ndk/ install dir for the newest version."""
    import platform
    _sys = platform.system()          # 'Darwin' | 'Linux' | 'Windows' — os.uname() is Unix-only
    host = {"Darwin": "darwin-x86_64", "Windows": "windows-x86_64"}.get(_sys, "linux-x86_64")
    exe = ".cmd" if _sys == "Windows" else ""     # Windows NDK clang is a .cmd wrapper
    roots: list[str] = []
    for env in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        if os.environ.get(env):
            roots.append(os.environ[env])
    for sdk in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
                os.path.expanduser("~/Library/Android/sdk"),
                os.path.expanduser("~/Android/Sdk")):
        if sdk:
            roots.extend(sorted(glob.glob(os.path.join(sdk, "ndk", "*")), reverse=True))
    for root in roots:
        for api in ("21", "23", "24", "26", "29", "33", "34"):
            cand = os.path.join(root, "toolchains", "llvm", "prebuilt", host,
                                "bin", f"aarch64-linux-android{api}-clang{exe}")
            if os.path.exists(cand):
                return cand
    return None


# --------------------------------------------------------------------------- #
# config codegen
# --------------------------------------------------------------------------- #
def _c_str(s: str | None) -> str:
    if s is None:
        return "0"
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


def gen_config_h(enemy_tuning: dict[str, dict],
                 shoot_bakes: dict[str, float] | None = None) -> tuple[str, int]:
    """Turn the project's enemy_tuning map into a config.h. Returns (text, count).

    A tuning key is "chunk|sx|sy"; the value carries the tuned fields plus the
    chunk height `h` (stored at edit time) needed to convert sy (row-from-top)
    into rowFromBottom = (h-1) - sy, the runtime match coordinate. Each record may
    carry a `shootmult` (launch-speed multiplier for that placement, default 1).

    `shoot_bakes` is the global dev-authored table of baseline launch speeds keyed
    "class|projectile" -> absolute speed; it becomes g_bakes[] and is what a
    placement's shootmult scales. Defaults to load_shoot_bakes()."""
    if shoot_bakes is None:
        shoot_bakes = load_shoot_bakes()
    rows: list[str] = []
    for key, val in sorted(enemy_tuning.items()):
        try:
            chunk, sx, sy = key.rsplit("|", 2)
            sx, sy = int(sx), int(sy)
        except ValueError:
            continue
        h = val.get("h")
        if not h:
            continue                      # can't place without the chunk height
        row = (int(h) - 1) - sy
        proj = val.get("projectile") or None
        health = val.get("health")
        health = int(health) if health not in (None, "") else -1
        walk = val.get("walk")
        walk = float(walk) if walk not in (None, "") else -1.0
        smult = val.get("shootmult")
        smult = float(smult) if smult not in (None, "") else 1.0
        fmult = val.get("firemult")
        fmult = float(fmult) if fmult not in (None, "") else 1.0
        rows.append(
            f"    {{ {_c_str(chunk)}, {sx}, {row}, {_c_str(proj)}, "
            f"{health}, {float(walk)!r}f, {float(smult)!r}f, {float(fmult)!r}f }},"   # repr keeps '.' -> valid C float
        )
    n = len(rows)
    body = "\n".join(rows) if rows else "    { 0, 0, 0, 0, -1, -1.0f, 1.0f, 1.0f }"

    brows: list[str] = []
    for bkey, spd in sorted((shoot_bakes or {}).items()):
        try:
            cls, proj = bkey.split("|", 1)
        except ValueError:
            continue
        if not cls or not proj:
            continue
        brows.append(f"    {{ {_c_str(cls)}, {_c_str(proj)}, {float(spd)!r}f }},")
    nb = len(brows)
    bbody = "\n".join(brows) if brows else "    { 0, 0, -1.0f }"

    text = (
        "/* GENERATED by core/nativemod.py — do not edit. */\n"
        "#ifndef NATIVEMOD_CONFIG_H\n#define NATIVEMOD_CONFIG_H\n\n"
        "typedef struct {\n"
        "    const char* chunk;\n    int col;\n    int row;\n"
        "    const char* projectile;\n    int health;\n    float walk;\n    float shootmult;\n    float firemult;\n"
        "} EnemyTune;\n\n"
        f"static const EnemyTune g_tunes[] = {{\n{body}\n}};\n"
        f"static const int g_ntunes = {n};\n\n"
        "typedef struct {\n"
        "    const char* cls;\n    const char* projectile;\n    float speed;\n"
        "} ShootBake;\n\n"
        f"static const ShootBake g_bakes[] = {{\n{bbody}\n}};\n"
        f"static const int g_nbakes = {nb};\n\n"
        "#endif\n"
    )
    return text, n


def stored_tuning_height(name: str, xml: str) -> int:
    """Convenience for callers: parse a chunk's height (for storing `h` in a
    tuning record at edit time)."""
    return Chunk.parse(xml).h


# --------------------------------------------------------------------------- #
# compile
# --------------------------------------------------------------------------- #
def compile_so(config_text: str, libil2cpp_path: str, out_so: str,
               clang: str | None = None, debug: bool = False, log=print) -> str:
    """Compile libnativemod.so (linking the user's own libil2cpp.so so the
    il2cpp_* symbols resolve at load). Returns out_so."""
    clang = clang or find_ndk_clang()
    if not clang:
        raise RuntimeError(
            "Android NDK not found — needed to build the enemy-tuning library. "
            "Install the NDK via Android Studio (SDK Manager > NDK) or set "
            "ANDROID_NDK_HOME.")
    build = os.path.dirname(out_so)
    os.makedirs(build, exist_ok=True)
    # config.h next to a copy of the source so #include "config.h" resolves
    with open(os.path.join(build, "config.h"), "w") as fh:
        fh.write(config_text)
    src = os.path.join(build, "nativemod.c")
    with open(NATIVE_SRC) as fh:
        src_text = fh.read()
    with open(src, "w") as fh:
        fh.write(src_text)
    # libil2cpp.so must be present as `libil2cpp.so` in a -L dir for -l:libil2cpp.so
    libdir = os.path.join(build, "link")
    os.makedirs(libdir, exist_ok=True)
    linkcopy = os.path.join(libdir, "libil2cpp.so")
    if os.path.abspath(libil2cpp_path) != os.path.abspath(linkcopy):
        with open(libil2cpp_path, "rb") as s, open(linkcopy, "wb") as d:
            d.write(s.read())
    cmd = [clang, "-shared", "-fPIC", "-O2", "-Wl,-soname,libnativemod.so",
           "-I", build, "-o", out_so, src, "-L", libdir, "-l:libil2cpp.so", "-llog"]
    if debug:
        cmd.insert(1, "-DNATIVEMOD_DEBUG")
    log(f"[nativemod] compiling libnativemod.so ({os.path.basename(clang)})")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"libnativemod.so compile failed:\n{r.stderr.strip()}")
    return out_so


# --------------------------------------------------------------------------- #
# runtime config: one prebuilt .so, patched per-mod (no NDK per tuning change)
# --------------------------------------------------------------------------- #
# The tuning table is no longer compiled in — it's written into a reserved blob
# in the prebuilt libnativemod.so as plain bytes. So a tuning change (an enemy's
# speed, etc.) needs only a byte-patch, never the NDK. The blob and text format
# are defined in core/native/nativemod.c (load_config()).
CONFIG_MAGIC = bytes([0x4C, 0x44, 0x4E, 0x4D, 0xC0, 0xDE, 0xF1, 0x9E,
                      0x43, 0x46, 0x47, 0x42, 0x4C, 0x4F, 0x42, 0x7F])
CONFIG_CAP = 65536
PREBUILT_DIR = os.path.join(NATIVE_DIR, "prebuilt")


def serialize_config(enemy_tuning: dict[str, dict],
                     shoot_bakes: dict[str, float] | None = None,
                     axe_settings: dict | None = None,
                     respawn_links: list[tuple[str, int, int]] | None = None,
                     playtest: dict | None = None,
                     ghostr_cells: list[tuple[str, int, int]] | None = None) -> tuple[str, int]:
    """Serialize the tuning map + dev bakes into the blob's line format. Mirrors
    the old gen_config_h codegen (same (chunk,col,rowFromBottom) key math), but
    emits text the .so parses at runtime instead of C the compiler bakes in.
    Returns (text, tuning_count)."""
    if shoot_bakes is None:
        shoot_bakes = load_shoot_bakes()
    lines = ["v1"]
    # global axe-boomerang tunables (x|range|speed|spin); blank field = keep the
    # .so's baked default. Only emitted when the user has overridden something.
    if axe_settings:
        def _f(k):
            v = axe_settings.get(k)
            return repr(float(v)) if v not in (None, "") else ""
        rng, spd, spn, hng = _f("range"), _f("speed"), _f("spin"), _f("hang")
        if rng or spd or spn or hng:
            lines.append(f"x|{rng}|{spd}|{spn}|{hng}")
    n = 0
    for key, val in sorted(enemy_tuning.items()):
        try:
            chunk, sx, sy = key.rsplit("|", 2)
            sx, sy = int(sx), int(sy)
        except ValueError:
            continue
        h = val.get("h")
        if not h:
            continue                      # can't place without the chunk height
        if "|" in chunk or "\n" in chunk:
            continue                      # delimiter safety (chunk basenames are clean)
        row = (int(h) - 1) - sy
        proj = val.get("projectile") or "-"
        health = val.get("health")
        health = int(health) if health not in (None, "") else -1
        walk = val.get("walk")
        walk = float(walk) if walk not in (None, "") else -1.0
        sm = val.get("shootmult")
        sm = float(sm) if sm not in (None, "") else 1.0
        fm = val.get("firemult")
        fm = float(fm) if fm not in (None, "") else 1.0
        wm = val.get("walkmult")
        wm = float(wm) if wm not in (None, "") else 1.0
        # muzzle spawn offset for the native bird-spawner (forward, up). Blank -> "" so
        # the native side keeps its defaults (16 forward, 6 up).
        mx = val.get("muzzle_x"); mx = float(mx) if mx not in (None, "") else ""
        my = val.get("muzzle_y"); my = float(my) if my not in (None, "") else ""
        lines.append(f"t|{chunk}|{sx}|{row}|{proj}|{health}|{walk!r}|{sm!r}|{fm!r}|{wm!r}|{mx}|{my}")
        n += 1
    for bkey, spd in sorted((shoot_bakes or {}).items()):
        try:
            cls, proj = bkey.split("|", 1)
        except ValueError:
            continue
        if not cls or not proj or "|" in proj:
            continue
        lines.append(f"b|{cls}|{proj}|{float(spd)!r}")
    # respawn links (🚩flag→🟢respawn connections): r|chunk|col|rowFromBottom.
    # `row` is already bottom-relative (same convention as t| tunes / enemy_cell).
    for (chunk, col, row) in (respawn_links or []):
        chunk = str(chunk)
        if "|" in chunk or "\n" in chunk:
            continue
        lines.append(f"r|{chunk}|{int(col)}|{int(row)}")
    # ghostR cells (gr|chunk|col|rowFromBottom): each placed ghostR was emitted as
    # ghostL; the native mod flips the ghost at this cell to leftToRight=1.
    for (chunk, col, row) in (ghostr_cells or []):
        chunk = str(chunk)
        if "|" in chunk or "\n" in chunk:
            continue
        lines.append(f"gr|{chunk}|{int(col)}|{int(row)}")
    # playtest features baked native (gated by their Settings toggles):
    # p|keep|bgbare|smooth|locky|captop|hidetimer|hideprog|respawn  (each 0/1)
    if playtest:
        def _b(k, default=0):
            return 1 if playtest.get(k, default) else 0
        flags = [_b("keep_music_bg"), _b("bg_bare"), _b("smooth_camera"),
                 _b("lock_camera_y"), _b("lock_y_cap_top", 1), _b("hide_timer"),
                 _b("hide_progress"), _b("respawn_flags")]
        if any(flags):
            lines.append("p|" + "|".join(str(f) for f in flags))
    return "\n".join(lines) + "\n", n


def patch_blob(so_bytes: bytes, config_text: str) -> bytes:
    """Write the tuning text into the prebuilt .so's config blob, in place (the
    .so's byte length is unchanged). Layout: [MAGIC][u32 len LE][text]."""
    idx = so_bytes.find(CONFIG_MAGIC)
    if idx < 0:
        raise RuntimeError("config blob MAGIC not found in libnativemod.so "
                           "(stale prebuilt? rebuild it)")
    if so_bytes.find(CONFIG_MAGIC, idx + 1) != -1:
        raise RuntimeError("config blob MAGIC found more than once — ambiguous")
    data = config_text.encode("utf-8")
    if len(data) + 20 >= CONFIG_CAP:
        raise RuntimeError(f"tuning config too large: {len(data)} bytes "
                           f"(cap {CONFIG_CAP - 20}). Reduce tunings or grow CONFIG_CAP.")
    out = bytearray(so_bytes)
    out[idx + 16:idx + 20] = struct.pack("<I", len(data))
    out[idx + 20:idx + 20 + len(data)] = data
    # blank any stale text from a prior patch (parser is length-bounded, but keep tidy)
    for i in range(idx + 20 + len(data), idx + CONFIG_CAP):
        out[i] = 0
    return bytes(out)


def get_prebuilt_so(arm64_in: str, workdir: str, debug: bool = False,
                    log=print) -> str:
    """Path to a config-blank prebuilt libnativemod.so, compiled ONCE (needs the
    NDK) and cached under core/native/prebuilt keyed by the C source hash. Reused
    for every subsequent build so per-mod tuning needs no compiler. For a shipped
    editor/patcher this file is bundled, so end users never invoke the NDK."""
    src_bytes = open(NATIVE_SRC, "rb").read()
    key = hashlib.sha1(src_bytes).hexdigest()[:12] + ("-dbg" if debug else "")
    os.makedirs(PREBUILT_DIR, exist_ok=True)
    cached = os.path.join(PREBUILT_DIR, f"libnativemod-{key}.so")
    if os.path.exists(cached):
        return cached
    # cache miss: compile once (needs NDK + the fixed libil2.so to link against)
    il2cpp = os.path.join(workdir, "libil2cpp.so")
    if not os.path.exists(il2cpp):
        os.makedirs(workdir, exist_ok=True)
        with zipfile.ZipFile(arm64_in) as z, z.open(LIBIL2CPP) as s, open(il2cpp, "wb") as d:
            d.write(s.read())
    log("[nativemod] no cached prebuilt — compiling libnativemod.so once")
    scratch_so = os.path.join(workdir, "prebuild", "libnativemod.so")
    compile_so("/* config is patched in at build time, not compiled */\n",
               il2cpp, scratch_so, debug=debug, log=log)
    import shutil
    tmp = cached + ".tmp"
    shutil.copyfile(scratch_so, tmp)
    os.replace(tmp, cached)          # atomic; keeps PREBUILT_DIR clean (only .so files)
    return cached


# --------------------------------------------------------------------------- #
# embed into the arm64 split
# --------------------------------------------------------------------------- #
def patch_libmain_lief(libmain_bytes: bytes) -> bytes:
    """Add a DT_NEEDED on libnativemod.so to libmain.so via LIEF, returning the
    patched bytes. Used ONCE to generate the reusable diff below — not on the
    per-mod / patcher path."""
    try:
        import lief
    except ImportError:
        raise RuntimeError("LIEF not installed — needed once to generate the "
                           "libmain DT_NEEDED patch. `pip install lief`.")
    import tempfile
    d = tempfile.mkdtemp()
    src = os.path.join(d, "libmain.so")
    with open(src, "wb") as fh:
        fh.write(libmain_bytes)
    m = lief.parse(src)
    needed = {getattr(e, "name", "") for e in m.dynamic_entries
              if getattr(e, "tag", None) is not None and "NEEDED" in str(e.tag)}
    if "libnativemod.so" not in needed:
        m.add_library("libnativemod.so")
    out = os.path.join(d, "libmain.patched")
    m.write(out)
    return open(out, "rb").read()


# The DT_NEEDED patch to libmain.so is IDENTICAL for every mod (it always just
# adds libnativemod.so) and libmain.so is byte-identical for everyone (same fixed
# xapk). So LIEF runs ONCE to produce it; we store the src->dst delta and replay
# it with pure Python thereafter — the shipped patcher needs no LIEF. Regenerated
# automatically if the game's libmain.so ever changes (keyed by its hash).
_LMPATCH_MAGIC = b"LDMAINP1"


def _diff_spans(src: bytes, dst: bytes) -> list[tuple[int, bytes]]:
    """Spans of dst that differ from src (src zero-padded to len(dst))."""
    n = len(dst)
    s = src if len(src) >= n else src + bytes(n - len(src))
    spans: list[tuple[int, bytes]] = []
    i = 0
    while i < n:
        if dst[i] != s[i]:
            j = i
            while j < n and dst[j] != s[j]:
                j += 1
            spans.append((i, dst[i:j]))
            i = j
        else:
            i += 1
    return spans


def _apply_spans(src: bytes, dst_len: int, spans: list[tuple[int, bytes]]) -> bytes:
    out = bytearray(src)
    if dst_len > len(out):
        out.extend(bytes(dst_len - len(out)))
    else:
        del out[dst_len:]
    for off, b in spans:
        out[off:off + len(b)] = b
    return bytes(out)


def _serialize_lmpatch(src_sha1_hex: str, dst_len: int, spans) -> bytes:
    out = bytearray(_LMPATCH_MAGIC)
    out += bytes.fromhex(src_sha1_hex)                 # 20 bytes
    out += struct.pack("<II", dst_len, len(spans))
    for off, b in spans:
        out += struct.pack("<II", off, len(b))
        out += b
    return bytes(out)


def _deserialize_lmpatch(data: bytes):
    if data[:8] != _LMPATCH_MAGIC:
        raise RuntimeError("bad libmain patch artifact")
    src_sha1 = data[8:28].hex()
    dst_len, nsp = struct.unpack_from("<II", data, 28)
    pos = 36
    spans = []
    for _ in range(nsp):
        off, ln = struct.unpack_from("<II", data, pos)
        pos += 8
        spans.append((off, data[pos:pos + ln]))
        pos += ln
    return src_sha1, dst_len, spans


def get_libmain_patched(libmain_bytes: bytes, log=print) -> bytes:
    """Return libmain.so with the libnativemod DT_NEEDED added, WITHOUT LIEF when
    a cached patch for this exact libmain exists. Generates the patch once (LIEF)
    on a cache miss and stores it under core/native/prebuilt (keyed by the game's
    libmain hash). For a shipped patcher the artifact is bundled, so LIEF is never
    needed at apply time."""
    src_sha1 = hashlib.sha1(libmain_bytes).hexdigest()
    os.makedirs(PREBUILT_DIR, exist_ok=True)
    art = os.path.join(PREBUILT_DIR, f"libmain_needed-{src_sha1[:12]}.bin")
    if os.path.exists(art):
        s, dl, spans = _deserialize_lmpatch(open(art, "rb").read())
        if s == src_sha1:
            return _apply_spans(libmain_bytes, dl, spans)     # pure Python, no LIEF
        log("[nativemod] cached libmain patch hash mismatch — regenerating")
    dst = patch_libmain_lief(libmain_bytes)                    # LIEF, once
    spans = _diff_spans(libmain_bytes, dst)
    with open(art, "wb") as fh:
        fh.write(_serialize_lmpatch(src_sha1, len(dst), spans))
    log(f"[nativemod] generated libmain DT_NEEDED patch "
        f"({len(spans)} spans) -> {os.path.basename(art)}")
    return dst


def embed_into_arm64(arm64_in: str, arm64_out: str, libnative_bytes: bytes,
                     log=print) -> None:
    """Rewrite the arm64 split: DT_NEEDED-patch libmain.so and add
    libnativemod.so (STORED, executable). Every other entry is copied verbatim
    with its original compression."""
    with zipfile.ZipFile(arm64_in) as z:
        libmain = z.read(LIBMAIN)
    libmain_patched = get_libmain_patched(libmain, log=log)   # cached diff, no LIEF

    tmp = arm64_out + ".tmp"
    with zipfile.ZipFile(arm64_in, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
        have_native = LIBNATIVE in zin.namelist()
        for info in zin.infolist():
            if info.filename == LIBMAIN:
                data = libmain_patched
            elif info.filename == LIBNATIVE:
                data = libnative_bytes            # replace if a stale one exists
            else:
                data = zin.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.internal_attr = info.internal_attr
            zi.create_system = info.create_system
            zout.writestr(zi, data)
        if not have_native:
            zi = zipfile.ZipInfo(LIBNATIVE)
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = (0o100755 << 16)   # -rwxr-xr-x
            zout.writestr(zi, libnative_bytes)
    os.replace(tmp, arm64_out)
    log("[nativemod] embedded libnativemod.so (+libmain DT_NEEDED) in arm64 split")


def build_and_embed(enemy_tuning: dict[str, dict], arm64_in: str, arm64_out: str,
                    workdir: str, debug: bool = False, log=print,
                    axe_settings: dict | None = None,
                    respawn_links: list[tuple[str, int, int]] | None = None,
                    allow_empty: bool = False, playtest: dict | None = None,
                    ghostr_cells: list[tuple[str, int, int]] | None = None) -> dict:
    """Full path: serialize tuning -> patch it into the prebuilt libnativemod.so
    -> embed. Only the FIRST build on a machine compiles the (config-blank) .so;
    every tuning change after that is a pure byte-patch — no NDK. Returns a
    summary dict; raises RuntimeError (clear message) if a compile is needed but
    the NDK/LIEF are unavailable."""
    os.makedirs(workdir, exist_ok=True)
    respawn_links = respawn_links or []
    config_text, n = serialize_config(enemy_tuning, axe_settings=axe_settings,
                                      respawn_links=respawn_links, playtest=playtest,
                                      ghostr_cells=ghostr_cells)
    if (n == 0 and not respawn_links and not axe_settings and not ghostr_cells
            and not allow_empty):
        raise RuntimeError("no valid enemy tunings to build")
    prebuilt = get_prebuilt_so(arm64_in, workdir, debug=debug, log=log)
    libnative = patch_blob(open(prebuilt, "rb").read(), config_text)
    log(f"[nativemod] patched {n} tuning(s) + {len(respawn_links)} respawn-link(s) "
        f"into prebuilt {os.path.basename(prebuilt)} ({len(config_text)} cfg bytes)")
    embed_into_arm64(arm64_in, arm64_out, libnative, log=log)
    return {"enemy_tunings": n, "respawn_links": len(respawn_links),
            "libnativemod_bytes": len(libnative),
            "prebuilt": os.path.basename(prebuilt)}
