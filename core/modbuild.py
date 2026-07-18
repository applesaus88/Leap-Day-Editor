"""
modbuild.py — apply a .ldmod project to the user's own .xapk -> signed APKs.

This is the copyright-clean "patcher": the mod (.ldmod) carries only authored
content (level XML, ordered-list overrides, patch names); the original game is
supplied by the user as their own .xapk. Nothing here ships game bytes.

Pipeline:
  unpack xapk
    -> overwrite text level chunks + apply ordered-list overrides in
       data.unity3d (repack) -> swap into base apk
    -> apply named libil2cpp.so byte patches in the arm64 split
    -> sign all splits -> [install]

The level-chunk path needs no type trees; ordered-list overrides and .so
patches are only built when the project actually requests them.
"""

from __future__ import annotations

import os
import zipfile

from .bundle import Bundle
from .project import Project
from . import apkbuild, axml, sopatch, typetree

BASE_APK = "com.nitrome.leapday.apk"
ARM64_APK = "config.arm64_v8a.apk"
DATA_ENTRY = "assets/bin/Data/data.unity3d"
METADATA_ENTRY = "assets/bin/Data/Managed/Metadata/global-metadata.dat"
SO_ENTRY = "lib/arm64-v8a/libil2cpp.so"
MANIFEST_ENTRY = "AndroidManifest.xml"
ORIG_PKG = "com.nitrome.leapday"

# Projectiles that have no script of their own — normally a specific enemy drives
# their SpriteRenderer. Swapped onto another shooter they need their renderer
# force-enabled to be visible. Keep in sync with is_thrower_puppet() in
# core/native/nativemod.c.
THROWER_PUPPET_PROJECTILES = {"axe"}


def _extract_respawn_links(levels: dict[str, str], log=print):
    """Scan authored chunks for 🚩flag(homingcannonUp)→🟢respawn(homingcannonDown)
    connection lines. Returns [(chunk_basename, col, rowFromBottom), ...] for the
    respawn endpoint — the native mod moves that chunk's checkpoint respawn there.
    Rows are bottom-relative to match the native side (enemy_cell / t| tunes)."""
    from .chunkfmt import Chunk
    out = []
    for name, xml in (levels or {}).items():
        try:
            ch = Chunk.parse(xml)
        except Exception:
            continue
        at = {(round(e.sx), round(e.sy)): e.properties for e in ch.enemies}
        base = str(name).split("/")[-1]
        for cn in ch.conns:
            a = at.get((cn.sx, cn.sy)); b = at.get((cn.mx, cn.my))
            if a == "homingcannonUp" and b == "homingcannonDown":
                col, sy_top = cn.mx, cn.my
            elif a == "homingcannonDown" and b == "homingcannonUp":
                col, sy_top = cn.sx, cn.sy
            else:
                continue
            row = (ch.h - 1) - sy_top                     # bottom-relative
            out.append((base, int(col), int(row)))
            log(f"[modbuild] respawn link: {base} → 🟢 cell ({col},{sy_top}) "
                f"[row_from_bottom={row}]")
    return out


def _slug(name: str) -> str:
    """A clean package/label suffix from a project name ('My Mod!' -> 'mymod')."""
    s = "".join(c for c in (name or "").lower() if c.isalnum())
    return s or "mod"


def clone_identity(project: Project) -> tuple[str, str, str]:
    """(new_package, app_label, clone_tag) for an install-alongside build.
    Per-project: derived from the mod name so different mods can coexist."""
    tag = _slug(project.name)
    pkg = f"{ORIG_PKG}.{tag}"
    label = f"Leap Day · {(project.name or 'Mod').strip()}"[:40]
    return pkg, label, tag


def unpack_xapk(xapk: str, dest: str) -> dict[str, str]:
    os.makedirs(dest, exist_ok=True)
    out: dict[str, str] = {}
    with zipfile.ZipFile(xapk) as z:
        for name in z.namelist():
            if name.endswith(".apk"):
                target = os.path.join(dest, os.path.basename(name))
                with z.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                out[os.path.basename(name)] = target
    return out


def _extract_entry(apk: str, entry: str, out_path: str) -> None:
    with zipfile.ZipFile(apk) as z, z.open(entry) as src, open(out_path, "wb") as dst:
        dst.write(src.read())


def _read_entry_bytes(apk: str, entry: str) -> bytes | None:
    with zipfile.ZipFile(apk) as z:
        if entry not in z.namelist():
            return None
        return z.read(entry)


def build(
    xapk: str,
    project: Project,
    out_dir: str,
    *,
    install: bool = False,
    clone_package: bool | None = None,
    strip_store: bool | None = None,
    log=print,
) -> dict:
    """Returns a summary dict; signed APKs land in out_dir/signed.

    clone_package: rename the package so the build installs ALONGSIDE the
        original (per-project id from the mod name). strip_store: remove
        ad/billing/Play components from the manifest (defaults on when cloning).
        Both default to the project's settings when not passed explicitly."""
    if clone_package is None:
        clone_package = bool(project.settings.get("clone_package"))
    if strip_store is None:
        strip_store = bool(project.settings.get("strip_store", clone_package))
    os.makedirs(out_dir, exist_ok=True)
    unpacked = os.path.join(out_dir, "unpacked")
    log(f"[modbuild] unpacking {os.path.basename(xapk)}")
    apks = unpack_xapk(xapk, unpacked)
    missing = {BASE_APK} - set(apks)
    if missing:
        raise FileNotFoundError(f"xapk missing expected split(s): {missing}")

    summary = {"levels_applied": 0, "overrides_applied": 0, "patches_applied": 0,
               "signed": []}

    # grapple-skin forces playing as Lick (so the grapple works) while his pack's
    # look is swapped for another character's.
    eff_force_character = (1 if project.grapple_skin is not None
                           else project.force_character)

    # "thrower-puppet" projectiles (e.g. the axe) render only because another
    # enemy enables their SpriteRenderer at runtime; swapped onto a normal shooter
    # they'd be invisible. Detect any tuned in so we can make their prefab
    # self-visible with a one-field bundle edit (see typetree below).
    puppet_projectiles = {
        v.get("projectile") for v in project.enemy_tuning.values()
        if v.get("projectile") in THROWER_PUPPET_PROJECTILES
    }

    # ---- 1. data.unity3d edits: text chunks + ordered-list / firebar overrides
    if (project.levels or project.overrides or project.firebars
            or project.element_overrides
            or project.grapple_skin is not None
            or puppet_projectiles
            or "grappling_hook" in project.patches):
        data_tmp = os.path.join(out_dir, "data.unity3d")
        _extract_entry(apks[BASE_APK], DATA_ENTRY, data_tmp)
        b = Bundle(data_tmp)

        # grappling-hook sprite fix: swap the GrapplingHook prefab's Lick-tongue
        # placeholder sprites for the real metal-hook art (no generator needed).
        if "grappling_hook" in project.patches:
            n = typetree.fix_grappling_hook_sprites(b.env, log=log)
            if n:
                b.mark_dirty()
                summary["grappling_hook_sprites"] = n

        # make thrower-puppet projectiles (axe) self-visible: enable their
        # SpriteRenderer on the prefab so a swapped-in shot renders on its own.
        if puppet_projectiles:
            n = typetree.enable_puppet_projectile_sprites(
                b.env, puppet_projectiles, log=log)
            if n:
                b.mark_dirty()
                summary["puppet_projectiles"] = sorted(puppet_projectiles)

        # 1a. overwrite text level chunks (no type trees needed)
        for name, xml in project.levels.items():
            if not b.has_text(name):
                raise KeyError(f"chunk {name!r} not present in bundle (can only "
                               f"overwrite existing chunks)")
            # the editor's half-cell-offset "second grid" (<grid2>) isn't a game
            # layer — fold it into <fg> at integer coords so the game renders it.
            if "<grid2>" in xml:
                from .chunkfmt import Chunk
                xml = Chunk.parse(xml).to_xml(for_game=True)
            # optional: brick the dead SIDE columns of wide chunks so their side
            # screens read as wall instead of empty background (the play area is
            # left untouched — see Chunk.fill_dead_sides).
            if project.brick_dead_sides:
                try:
                    from .chunkfmt import Chunk
                    c = Chunk.parse(xml)
                    if c.w > 14 and c.fill_dead_sides():
                        xml = c.to_xml()
                        summary["brick_filled"] = summary.get("brick_filled", 0) + 1
                except Exception as e:
                    log(f"[modbuild] brick-fill skipped for {name!r}: {e}")
            b.set_text(name, xml)
            summary["levels_applied"] += 1
        if project.levels:
            log(f"[modbuild] applied {summary['levels_applied']} custom level(s)")
            if summary.get("brick_filled"):
                log(f"[modbuild] bricked dead side areas on {summary['brick_filled']} wide chunk(s)")

        # 1b/1c. serialized-field overrides on MonoBehaviours (Level ordered
        #        lists + Mace firebars). Type trees are generated from the user's
        #        own .so + metadata — no game bytes are kept.
        if (project.overrides or project.firebars or project.element_overrides
                or project.grapple_skin is not None or project.flag_checkpoints):
            so_tmp = os.path.join(out_dir, "global-metadata.dat")
            _extract_entry(apks[BASE_APK], METADATA_ENTRY, so_tmp)
            lib_tmp = os.path.join(out_dir, "libil2cpp.so.src")
            if ARM64_APK not in apks:
                raise FileNotFoundError(f"xapk missing {ARM64_APK} (needed for "
                                        f"type-tree generation)")
            _extract_entry(apks[ARM64_APK], SO_ENTRY, lib_tmp)
            gen = typetree.TreeGen.from_paths(lib_tmp, so_tmp)
            if project.overrides:
                log("[modbuild] generating Level type tree for ordered-list override")
                n = typetree.override_level_lists(b.env, gen, project.overrides, log=log)
                b.mark_dirty()
                summary["overrides_applied"] = sum(len(v) for v in project.overrides.values())
                log(f"[modbuild] applied ordered-list override(s) to {n} Level "
                    f"instance(s): {', '.join(project.overrides)}")
            if project.firebars:
                fb = typetree.override_mono_fields(b.env, gen, project.firebars,
                                                   cls="Mace", log=log)
                b.mark_dirty()
                summary["firebars_applied"] = fb
                log(f"[modbuild] applied {fb} universal firebar(s)")
            if project.element_overrides:
                # generalized element knobs (rotating block / conveyor / cannon /
                # …): group carriers by their MonoBehaviour class, override each.
                from collections import defaultdict
                # a {"__prefab__": name} field value (e.g. a cannon's projectile)
                # resolves to that GameObject's PPtr in THIS bundle.
                _go_pid: dict[str, int] = {}
                for _o in b.env.objects:
                    if _o.type.name == "GameObject":
                        try:
                            _go_pid.setdefault(_o.read().m_Name, _o.path_id)
                        except Exception:
                            pass

                def _resolve_prefabs(fields):
                    out = {}
                    for k, v in fields.items():
                        if isinstance(v, dict) and "__prefab__" in v:
                            pid = _go_pid.get(v["__prefab__"])
                            if pid:
                                out[k] = {"m_FileID": 0, "m_PathID": pid}
                            else:
                                log(f"[modbuild] projectile {v['__prefab__']!r} "
                                    f"not found; kept default")
                        else:
                            out[k] = v
                    return out

                by_cls: dict[str, dict] = defaultdict(dict)
                for tok, ov in project.element_overrides.items():
                    by_cls[ov["cls"]][tok] = _resolve_prefabs(ov["fields"])
                n_el = 0
                for cls_name, by_token in by_cls.items():
                    n_el += typetree.override_mono_fields(b.env, gen, by_token,
                                                          cls=cls_name, log=log)
                b.mark_dirty()
                summary["element_overrides_applied"] = n_el
                log(f"[modbuild] applied {n_el} element override(s)")
            if project.grapple_skin is not None:
                typetree.clone_skin_onto_lick(b.env, gen, project.grapple_skin, log=log)
                b.mark_dirty()
                summary["grapple_skin"] = sopatch.CHARACTER_NAMES[project.grapple_skin]
            if project.flag_checkpoints:
                fc = typetree.flag_style_checkpoints(b.env, gen, log=log)
                b.mark_dirty()
                summary["flag_checkpoints"] = fc

        if b.dirty:
            repacked = os.path.join(out_dir, "data.unity3d.mod")
            b.save(repacked)
            modded_base = os.path.join(out_dir, BASE_APK)
            apkbuild.replace_entry(apks[BASE_APK], modded_base, DATA_ENTRY,
                                   open(repacked, "rb").read())
            apks[BASE_APK] = modded_base

    # ---- 2. libil2cpp.so behaviour patches + force_date (arm64 split) -----
    # vip_unlock (VIP, all characters visible/unlocked, no ads) and
    # allow_all_elements (let any enemy/tile appear in any theme — otherwise the
    # generator drops a chunk whose elements are theme-forbidden, e.g.
    # valentinesBlob in Newyear, and swaps in a fallback chunk) are ALWAYS
    # applied so every build/playtest keeps the authored content as-is.
    patches = list(project.patches)
    for _always in ("vip_unlock", "allow_all_elements"):
        if _always not in patches:
            patches.append(_always)
    if (patches or project.force_date or project.force_theme is not None
            or eff_force_character is not None
            or project.checkpoint_fruit_cost is not None
            or project.force_checkpoint_mode is not None):
        if ARM64_APK not in apks:
            raise FileNotFoundError(f"xapk missing {ARM64_APK} (needed for "
                                    f".so patches)")
        lib_patch = os.path.join(out_dir, "libil2cpp.so.patched")
        _extract_entry(apks[ARM64_APK], SO_ENTRY, lib_patch)
        applied = sopatch.apply(lib_patch, patches,
                                force_date=project.force_date,
                                force_theme=project.force_theme,
                                force_character=eff_force_character,
                                checkpoint_fruit_cost=project.checkpoint_fruit_cost,
                                force_checkpoint_mode=project.force_checkpoint_mode,
                                log=log)
        summary["patches_applied"] = applied
        if project.force_date:
            summary["force_date"] = project.force_date
        if project.force_theme is not None:
            summary["force_theme"] = project.force_theme
        if eff_force_character is not None:
            summary["force_character"] = sopatch.CHARACTER_NAMES[eff_force_character]
        if project.checkpoint_fruit_cost is not None:
            summary["checkpoint_fruit_cost"] = project.checkpoint_fruit_cost
        if project.force_checkpoint_mode is not None:
            summary["force_checkpoint_mode"] = sopatch.CHECKPOINT_MODES.get(
                project.force_checkpoint_mode, project.force_checkpoint_mode)
        if applied:
            modded_arm64 = os.path.join(out_dir, ARM64_APK)
            apkbuild.replace_entry(apks[ARM64_APK], modded_arm64, SO_ENTRY,
                                   open(lib_patch, "rb").read())
            apks[ARM64_APK] = modded_arm64
        want = sum(len(sopatch.PATCHES.get(n, [])) for n in patches) \
            + (3 if project.force_date else 0) \
            + (1 if project.force_theme is not None else 0) \
            + (1 if eff_force_character is not None else 0) \
            + (3 if project.checkpoint_fruit_cost is not None else 0) \
            + (2 if project.force_checkpoint_mode is not None else 0)
        log(f"[modbuild] applied {applied}/{want} .so patch(es)"
            + (f" — date locked to {project.force_date}" if project.force_date else "")
            + (f" — theme {sopatch.THEME_NAMES[project.force_theme]}"
               if project.force_theme is not None else "")
            + (f" — character {sopatch.CHARACTER_NAMES[eff_force_character]}"
               if eff_force_character is not None else "")
            + (f" — checkpoint cost {project.checkpoint_fruit_cost} fruits"
               if project.checkpoint_fruit_cost is not None else ""))

    # ---- 2.7 per-individual-enemy tuning: embed libnativemod.so ------------
    # Projectile / health / walk-speed set on ONE placed enemy can't be baked
    # into the level text (every enemy of a type shares one prefab), so we ship a
    # tiny native library the game loads itself (DT_NEEDED on libmain.so) that
    # edits each matching instance at runtime. Keyed by (chunk, sx, sy).
    #
    # Global "fast spawn/fire rate" toggle: multiplies EVERY enemy's cadence field
    # (spawnTimer / Cupid.pauseTime) via a wildcard tune (chunk "*", any cell) that
    # any per-enemy tune still overrides. 1 = off.
    effective_tuning = dict(project.enemy_tuning)
    spawn_mult = project.settings.get("spawn_mult", 1)
    try:
        spawn_mult = float(spawn_mult)
    except (TypeError, ValueError):
        spawn_mult = 1.0
    if spawn_mult and spawn_mult != 1.0:
        effective_tuning.setdefault("*|-1000|0", {"h": 1})
        effective_tuning["*|-1000|0"] = {**effective_tuning["*|-1000|0"],
                                          "firemult": spawn_mult, "h": 1}
        log(f"[modbuild] global spawn/fire rate ×{spawn_mult} on every enemy")

    # 🚩flag→🟢respawn connection lines: move each checkpoint's respawn onto the
    # author-placed 🟢 cell in its chunk (native libnativemod, checkpoint-hit gated).
    respawn_links = _extract_respawn_links(project.levels, log=log)

    if effective_tuning or respawn_links:
        if ARM64_APK not in apks:
            raise FileNotFoundError(f"xapk missing {ARM64_APK} (needed for enemy "
                                    f"tuning / respawn links)")
        from . import nativemod
        nm_work = os.path.join(out_dir, "nativemod")
        os.makedirs(nm_work, exist_ok=True)
        modded_arm64_nm = os.path.join(nm_work, ARM64_APK)   # keep the canonical split name
        nm_summary = nativemod.build_and_embed(
            effective_tuning, apks[ARM64_APK], modded_arm64_nm, nm_work,
            debug=bool(os.environ.get("LEAPDAY_NATIVEMOD_DEBUG")), log=log,
            axe_settings=getattr(project, "axe", None), respawn_links=respawn_links)
        apks[ARM64_APK] = modded_arm64_nm
        summary["enemy_tunings"] = nm_summary["enemy_tunings"]
        if effective_tuning:
            log(f"[modbuild] embedded per-enemy tuning for "
                f"{nm_summary['enemy_tunings']} enemy placement(s)")
        if respawn_links:
            summary["respawn_links"] = len(respawn_links)
            log(f"[modbuild] embedded {len(respawn_links)} flag→respawn link(s)")

    # ---- 2.5 clone package + strip store/ads (AndroidManifest, all splits) -
    # Rename the package so the build installs ALONGSIDE the stock game (Android
    # rejects a reused provider authority / custom permission), optionally drop
    # the ad/billing/Play components. The base manifest gets the full treatment;
    # config splits only need the matching package (install-multiple requires
    # every split share it). Component CLASS names are left untouched, so the
    # unchanged dex still resolves every class.
    if clone_package:
        pkg, label, tag = clone_identity(project)
        summary["clone_package"] = pkg
        summary["clone_label"] = label
        log(f"[modbuild] cloning package -> {pkg}"
            + (" (+strip store/ads)" if strip_store else ""))
        clone_dir = os.path.join(out_dir, "clone")
        os.makedirs(clone_dir, exist_ok=True)
        for split, full in ((BASE_APK, True), (ARM64_APK, False),
                            ("config.armeabi_v7a.apk", False)):
            if split not in apks:
                continue
            man = _read_entry_bytes(apks[split], MANIFEST_ENTRY)
            if man is None:
                continue
            newman = axml.clone_and_strip(
                man, new_pkg=pkg, old_pkg=ORIG_PKG, clone_tag=tag,
                label=(label if full else None),
                strip=(strip_store and full), log=log)
            modded = os.path.join(clone_dir, split)   # keep the clean split name
            apkbuild.replace_entry(apks[split], modded, MANIFEST_ENTRY, newman)
            apks[split] = modded
        summary["strip_store"] = bool(strip_store)

    # ---- 3. sign all splits with one shared key --------------------------
    log("[modbuild] signing splits")
    signed_dir = os.path.join(out_dir, "signed")
    signed = apkbuild.sign_all(list(apks.values()), signed_dir)
    summary["signed"] = [os.path.basename(s) for s in signed]
    summary["signed_dir"] = signed_dir

    # ---- 4. optional install ---------------------------------------------
    if install:
        devs = apkbuild.adb_devices()
        if not devs:
            log("[modbuild] no device connected; skipping install")
        else:
            to_install = [p for p in signed if "armeabi" not in p]  # arm64 emu
            log(f"[modbuild] installing onto {devs[0]}")
            # a cloned build is a DIFFERENT package — install it without touching
            # the stock game (keep_data avoids uninstalling com.nitrome.leapday).
            apkbuild.install(to_install, keep_data=bool(clone_package))
            summary["installed_on"] = devs[0]
    return summary
