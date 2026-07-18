"""
playtest.py — turn an authored chunk *sequence* into a live, repeatedly
overwritten demo level on the device.

Two ideas wired together:

  * SEQUENCE  : an ordered list of chunk names ("these chunks, next to each
                other"). resolve_sequence() pulls each chunk's content (an
                edited version from the project if present, else the original
                from the bundle) and stack_chunks() concatenates them top-to-
                bottom into ONE tall chunk that renders in exactly that order.

  * FLOOD     : the daily level is generated from a date seed, so to guarantee
                the run shows our content we overwrite (flood) the chunk pool
                with it. flood_levels() builds the {chunk-name -> XML} map; each
                target keeps its own <difficulty> so pool placement is sane.

playtest_sequence() does one build/install/launch iteration; watch_demo() loops
it every time the .ldmod's `demo` sequence changes, so a single demo level is
continuously overwritten as you edit — the fast edit→see-it loop.

Nothing here ships game bytes; it only rewrites text chunks in the user's own
data.unity3d (optionally with the ordered-list override / .so patches the
project already carries).
"""

from __future__ import annotations

import os
import re

from .bundle import Bundle
from .chunkfmt import Chunk, stack_chunks
from .project import Project
from . import apkbuild, modbuild

# chunk-name prefixes that must NOT be flooded: start/end/terminus/dev chunks
# (replacing those breaks generation — empty level or load hang).
_SKIP = re.compile(r"^(end|start|finish|intro|debug|test|chunk_test|_|autotile)", re.I)
_DIFF_RE = re.compile(r"<difficulty>([^<]*)</difficulty>")
_EASY = {"0", "0.0", "0.5"}      # easiest tiers == the run's first section


def resolve_chunk(name: str, project: Project, bundle: Bundle) -> str:
    """XML for a chunk: the project's edited version if any, else the original."""
    if name in project.levels:
        return project.levels[name]
    if bundle.has_text(name):
        return bundle.get_text(name)
    raise KeyError(f"chunk {name!r} not in project or bundle")


def resolve_sequence(names: list[str], project: Project, bundle: Bundle,
                     *, first_at_bottom: bool = True) -> str:
    """Stack an ordered list of chunk names into one tall chunk's XML."""
    if not names:
        raise ValueError("sequence is empty")
    chunks = [Chunk.parse(resolve_chunk(n, project, bundle)) for n in names]
    return stack_chunks(chunks, first_at_bottom=first_at_bottom).to_xml()


def flood_levels(bundle: Bundle, base_xml: str, catalog: dict,
                 *, scope: str = "mid") -> dict[str, str]:
    """Build a {chunk-name -> base_xml} map flooding the day's chunk pool.

    Each target keeps its own <difficulty> so pool placement stays balanced.
    scope:
      * "mid"  (default): every safe GAMEPLAY chunk except the easiest
        first-section tiers. Proven reliable — replacing mid-level chunks
        renders and stays playable; replacing the easy/first-section chunks
        collapses the start to empty (validity rules).
      * "all" : every safe chunk including easy. Maximises the chance you see
        the demo right away, but risks an empty start.
      * "easy": only the first-section (easy) chunks.
    """
    names = catalog.get("all_chunk_names", [])
    out: dict[str, str] = {}
    for n in names:
        if not bundle.has_text(n) or _SKIP.match(n):
            continue
        src = bundle.get_text(n)
        m = _DIFF_RE.search(src)
        dv = m.group(1).strip() if m else "1"
        if scope == "mid" and dv in _EASY:
            continue
        if scope == "easy" and dv not in _EASY:
            continue
        out[n] = _DIFF_RE.sub(f"<difficulty>{dv}</difficulty>", base_xml)
    return out


TEST_DATE = "2026-02-01"   # convention: Feb 1 2026 is the throwaway test level


def playtest_sequence(xapk: str, names: list[str], project: Project,
                      bundle: Bundle, catalog: dict, out_dir: str, *,
                      scope: str = "mid", first_at_bottom: bool = True,
                      force_date: str | None = TEST_DATE,
                      install: bool = True, launch: bool = True,
                      log=print) -> dict:
    """One iteration: stack the sequence, flood the pool, build, install, launch.

    Carries the project's ordered-list overrides and .so patches through too, so
    e.g. the VIP-popup patch applies on the same build. By convention the test
    runs on the Feb-1-2026 level (force_date=TEST_DATE) so playtesting is locked
    to a single, stable date separate from the January custom levels; pass
    force_date=None to test on whatever the project/clock says. Uses keep_data
    installs so the privacy prompt stays accepted across iterations."""
    base_xml = resolve_sequence(names, project, bundle,
                                first_at_bottom=first_at_bottom)
    levels = flood_levels(bundle, base_xml, catalog, scope=scope)
    if not levels:
        raise RuntimeError(f"no floodable chunks for scope={scope!r}")
    patches = project.patches or []
    if "vip_popup" not in patches:
        patches = patches + ["vip_popup"]      # always suppress VIP in playtest
    proj = Project(name="__demo_playtest__", levels=levels,
                   overrides=project.overrides, patches=patches,
                   force_date=force_date or project.force_date,
                   force_theme=project.force_theme,
                   force_character=project.force_character,
                   firebars=project.firebars,
                   grapple_skin=project.grapple_skin)
    summary = modbuild.build(xapk, proj, out_dir, install=False, log=log)
    summary["flooded"] = len(levels)
    summary["sequence"] = list(names)
    if install:
        signed_dir = summary["signed_dir"]
        to_install = [os.path.join(signed_dir, s) for s in summary["signed"]
                      if "armeabi" not in s]           # base + arm64 (emulator)
        apkbuild.install(to_install, keep_data=True)
        log(f"[playtest] installed demo ({len(levels)} chunks flooded)")
        if launch:
            apkbuild.launch()
            log("[playtest] launched — tap the device to play the demo level")
    return summary


def watch_demo(xapk: str, mod_path: str, catalog: dict, out_dir: str, *,
               scope: str = "mid", poll: float = 1.0, log=print) -> None:
    """Continuously re-playtest a .ldmod's `demo` sequence as the file changes.

    Each time mod_path is saved with a different demo, the demo level on the
    device is overwritten and relaunched. Blocks until interrupted (Ctrl-C).
    """
    import time

    if not apkbuild.ensure_device(log=log):
        raise RuntimeError("no emulator/device available")
    last_sig = None
    log(f"[watch] watching {os.path.basename(mod_path)} — edit + save to replay "
        f"(Ctrl-C to stop)")
    while True:
        try:
            project = Project.load(mod_path)
        except Exception as e:               # mid-save / malformed; retry
            log(f"[watch] skip: {e}")
            time.sleep(poll)
            continue
        sig = tuple(project.demo)
        if project.demo and sig != last_sig:
            last_sig = sig
            log(f"[watch] demo changed -> {project.demo}")
            bundle = _bundle_from_xapk(xapk, out_dir)
            try:
                playtest_sequence(xapk, project.demo, project, bundle, catalog,
                                  out_dir, scope=scope, log=log)
            except Exception as e:
                log(f"[watch] build failed: {e}")
        time.sleep(poll)


def _bundle_from_xapk(xapk: str, out_dir: str) -> Bundle:
    """Extract data.unity3d from the xapk's base apk and open it as a Bundle
    (the read-only source of original chunk content for sequence resolution)."""
    import zipfile, io

    os.makedirs(out_dir, exist_ok=True)
    data_tmp = os.path.join(out_dir, "data.src.unity3d")
    with zipfile.ZipFile(xapk) as z:
        base = next(n for n in z.namelist()
                    if n.endswith(modbuild.BASE_APK))
        with zipfile.ZipFile(io.BytesIO(z.read(base))) as bz:
            with bz.open(modbuild.DATA_ENTRY) as src, open(data_tmp, "wb") as out:
                out.write(src.read())
    return Bundle(data_tmp)
