"""
typetree.py — read/modify SERIALIZED MonoBehaviour fields in data.unity3d.

Leap Day's authored level structure is not all plain-text TextAssets. The
`Level` MonoBehaviour carries serialized *ordered lists* (the `endChunksList*`
end-chunk pools that decide which chunk terminates a day's level). Overriding
those lists is the "master key": it lets us pin exactly which chunks the
generator may pick from, instead of blindly flooding every chunk TextAsset.

To touch serialized fields we need Unity type trees, which we generate from the
game's own metadata (libil2cpp.so + global-metadata.dat) via TypeTreeGenerator.

THE TYPE-TREE FIX
-----------------
TypeTreeGenerator (for this game/version) mislabels every `List<string>` /
`vector<string>` container node with m_Type == "string" instead of "vector".
UnityPy then reads such a field as a *single* length-prefixed string, consumes
the wrong number of bytes, and the next field's length reads past the buffer:

    ValueError: read_str out of bounds

`_fix_nodes` repairs the generated node list before use: any node typed
"string" whose Array element ("data") is itself NOT a `char` is really a
container, so it is retyped to "vector". Genuine strings (Array data == char)
are left alone. With this fix the `Level` typetree reads cleanly and round-trips
byte-identically, so its ordered lists can be edited and repacked.

Nothing here ships game bytes: the type trees are generated from the user's own
libil2cpp.so/global-metadata.dat at build time, and an override is just a list
of chunk-path strings (authored config).
"""

from __future__ import annotations

import json
import re

from TypeTreeGeneratorAPI import TypeTreeGenerator

UNITY_VERSION = "2022.3.62f1"
ASSEMBLY = "Assembly-CSharp"
_CONTAINER_TYPES = {"vector", "staticvector", "set", "map", "Array", "TypelessData"}


def _children(nodes: list[dict], idx: int) -> list[int]:
    """Indices of nodes that are direct children (m_Level + 1) of nodes[idx]."""
    lvl = nodes[idx]["m_Level"]
    out: list[int] = []
    j = idx + 1
    while j < len(nodes) and nodes[j]["m_Level"] > lvl:
        if nodes[j]["m_Level"] == lvl + 1:
            out.append(j)
        j += 1
    return out


def _fix_nodes(nodes: list[dict]) -> list[dict]:
    """Retype container nodes the generator mislabeled "string" -> "vector".

    A real `string` serializes as  string -> Array -> {int size, char data}.
    A mislabeled `vector<X>` has the same shape but its Array `data` element is
    X (e.g. another "string"), never `char`. We detect that and fix the m_Type
    in place. Mutates and returns the same list.
    """
    n = len(nodes)
    for i, node in enumerate(nodes):
        if node["m_Type"] != "string":
            continue
        kids = _children(nodes, i)
        arr = next((c for c in kids if nodes[c]["m_Type"] == "Array"), None)
        if arr is None:
            continue
        arr_kids = _children(nodes, arr)  # [size, data]
        if len(arr_kids) < 2:
            continue
        data_type = nodes[arr_kids[1]]["m_Type"]
        if data_type != "char":
            node["m_Type"] = "vector"
    return nodes


class TreeGen:
    """Lazy, cached type-tree generator with the string->vector fix applied."""

    def __init__(self, so_bytes: bytes, metadata_bytes: bytes,
                 unity_version: str = UNITY_VERSION):
        self._g = TypeTreeGenerator(unity_version, "AssetsTools")
        self._g.load_il2cpp(so_bytes, metadata_bytes)
        self._cache: dict[tuple[str, str], list[dict]] = {}

    @classmethod
    def from_paths(cls, so_path: str, metadata_path: str) -> "TreeGen":
        with open(so_path, "rb") as f:
            so = f.read()
        with open(metadata_path, "rb") as f:
            md = f.read()
        return cls(so, md)

    def nodes(self, cls: str, assembly: str = ASSEMBLY) -> list[dict]:
        key = (assembly, cls)
        if key not in self._cache:
            raw = json.loads(self._g.get_nodes_as_json(assembly, cls))
            self._cache[key] = _fix_nodes(raw)
        return self._cache[key]


def override_mono_fields(env, gen: "TreeGen", by_token: dict[str, dict],
                         cls: str = "Mace", *, log=print) -> int:
    """Write serialized fields onto the `cls` MonoBehaviour of named prefab
    GameObjects. `by_token` maps a prefab/token name (e.g. a carrier mace token)
    to {field: value}. Used to turn a fixed mace preset into an arbitrary firebar
    (chainLengthInTiles/doubleMace/angularSpeed/progress/circularMovement).

    Only the listed fields change; the rest round-trip. Returns the count edited.
    Overriding a prefab affects EVERY placement of that token in the build.
    """
    if not by_token:
        return 0
    by_name: dict[str, list] = {}
    for o in env.objects:
        if o.type.name == "GameObject":
            try:
                by_name.setdefault(o.read().m_Name, []).append(o)
            except Exception:
                pass
    nodes = gen.nodes(cls)
    edited = 0
    for token, fields in by_token.items():
        gos = by_name.get(token)
        if not gos:
            log(f"[typetree] firebar carrier {token!r} not found; skipped")
            continue
        done = False
        for go in gos:
            for comp in getattr(go.read(), "m_Components", []):
                ref = comp.component if hasattr(comp, "component") else comp
                try:
                    r = ref.deref()
                except Exception:
                    continue
                if script_class(r) != cls:
                    continue
                tree = r.read_typetree(nodes)
                for k, v in fields.items():
                    if k not in tree:
                        raise KeyError(f"{cls} has no serialized field {k!r}")
                    tree[k] = v
                r.save_typetree(tree, nodes)
                edited += 1
                done = True
                break
            if done:
                break
        if done:
            log(f"[typetree] firebar {token}: "
                + ", ".join(f"{k}={v}" for k, v in fields.items()))
    return edited


# CharacterPack visual fields = the character's LOOK (everything but its id /
# grapple / unlock metadata). Copying these from character X onto Lick's pack,
# while keeping Lick's characterID (1) and grappleAnim, makes "play as Lick" look
# like X but keep the grapple (whose ability is hard-gated to characterID == 1).
SKIN_FIELDS = (
    "characterName", "jumpSprite", "fallSprite", "runningAnim", "rollingAnim",
    "slideAnim", "deathAnim", "respawnAnim", "finishAnim", "portrait",
    "nameSprite", "dropAnim", "glideAnim", "giveGiftAnim",
)


def fix_grappling_hook_sprites(env, *, log=print) -> int:
    """Make the forced grappling hook render as the real metal hook instead of as
    Lick's pink tongue. The GrapplingHook prefab reuses Lick's tongue art as
    placeholders: HeadSprite='Clawed1tongue' (the hook that grabs) and LineSprite=
    'tongue stretch' (the cable). Swapping the sprite REFERENCES breaks the geometry
    (different pivots/sizes flip the line and misplace the head), so instead we keep
    those exact sprites — preserving all geometry — and REPAINT their standalone
    textures: the head with the metal hook art (Grapple_A_Finish1), the cable a
    dark metal grey. Returns the number of sprites repainted.
    """
    from PIL import Image, ImageOps

    sprites = {}
    for o in env.objects:
        if o.type.name == "Sprite":
            try:
                sprites.setdefault(o.read().m_Name, o)
            except Exception:
                pass

    def repaint(name, src_img, fill=False):
        o = sprites.get(name)
        if not o:
            return False
        try:
            tex = o.read().m_RD.texture.read()
            sz = tex.image.size
            if fill:
                new = src_img.convert("RGBA").resize(sz)
            else:
                # paste preserving aspect, centred, transparent padding — keeps the
                # hook un-squished within the placeholder's texture footprint.
                new = Image.new("RGBA", sz, (0, 0, 0, 0))
                si = src_img.convert("RGBA")
                s = min(sz[0] / si.width, sz[1] / si.height)
                si = si.resize((max(1, int(si.width * s)), max(1, int(si.height * s))))
                new.paste(si, ((sz[0] - si.width) // 2, (sz[1] - si.height) // 2), si)
            tex.image = new
            tex.save()
            return True
        except Exception as e:
            log(f"[grappling_hook] repaint {name} failed: {e}")
            return False

    def cable_xsection():
        # Use the game's OWN rope art for the cable: rope-for-present is a thin
        # (6px-wide) vertical rope. Take a cross-section row and GREY it (desaturate
        # to luminance, keep alpha) so the stretched line reads as a real metal rope
        # instead of a flat fill — a greyed copy, leaving the orange in-game ropes
        # untouched (we only repaint the line's own 'tongue stretch' texture).
        o = sprites.get("rope-for-present")
        if o:
            try:
                img = o.read().image.convert("RGBA")
                row = img.crop((0, img.height // 2, img.width, img.height // 2 + 1))
                alpha = row.split()[3]
                g = ImageOps.grayscale(row.convert("RGB"))
                return Image.merge("RGBA", (g, g, g, alpha))
            except Exception:
                pass
        # fallback: cable cross-section from a Shoot frame
        for nm in ("Grapple_A_Shoot6", "Grapple_A_Shoot5", "Grapple_A_Shoot7"):
            o = sprites.get(nm)
            if not o:
                continue
            try:
                img = o.read().image.convert("RGBA")
            except Exception:
                continue
            for y in range(1, max(2, img.height // 2)):
                xs = [x for x in range(img.width) if img.getpixel((x, y))[3] > 90]
                if 1 <= len(xs) <= 6:
                    return img.crop((min(xs), y, max(xs) + 1, y + 1))  # 1-row cable slice
        return None

    n = 0
    # The hook TOP is the claw (Clawed1-4). The pink the user saw was the head's
    # IDLE sprite 'Clawed1tongue' (Lick's tongue ball) — overwrite that texture with
    # the claw so no pink can show as a fallback. ("ignore the tongue".)
    fin = sprites.get("Clawed1")
    if fin and repaint("Clawed1tongue", fin.read().image):
        n += 1

    # The launcher sprite Grapple_A_Shoot7 has a thin STEM/post built into its bottom
    # (the bit that dangles into the body). Erase it — keep only the head — by finding
    # where the silhouette narrows below the head and dropping everything below.
    s7 = sprites.get("Grapple_A_Shoot7")
    if s7:
        try:
            d = s7.read()
            tex = d.m_RD.texture.read()
            r = d.m_RD.textureRect
            x, y, w, h = (int(round(r.x)), int(round(r.y)),
                         int(round(r.width)), int(round(r.height)))
            base = tex.image.convert("RGBA")
            top = base.height - y - h
            region = base.crop((x, top, x + w, top + h)).convert("RGBA")
            widths = [sum(1 for xx in range(w) if region.getpixel((xx, yy))[3] > 80)
                      for yy in range(h)]
            mx = max(widths) if widths else 0
            cut, seen = h, False
            for yy in range(h):
                if widths[yy] >= mx / 2:
                    seen = True
                elif seen:           # first narrow row below the head = stem start
                    cut = yy
                    break
            new = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            new.paste(region.crop((0, 0, w, cut)), (0, 0))   # keep head, drop stem
            base.paste(new, (x, top))
            tex.image = base
            tex.save()
            n += 1
            log(f"[grappling_hook] launcher stem removed (kept head rows 0-{cut})")
        except Exception as e:
            log(f"[grappling_hook] stem removal failed: {e}")
    # CABLE. The game's line renderer STRETCHES a 1px strip by transform scale
    # (SetCorrectLineScale -> localScale=(1,dist,1)); referencing the rope sprite
    # directly renders invisibly-thin and Tiled mode won't tile it. So keep the
    # 'tongue stretch' strip that DOES render and paint it with the GREY Rope (1)
    # cross-section, so the cable reads as a grey rope colour stretched up the line.
    rope = None
    for o in env.objects:
        if o.type.name == "Sprite":
            try:
                if o.read().m_Name == "leapday_premium_offer 1_0":
                    rope = o
                    break
            except Exception:
                pass
    if rope:
        try:
            rimg = rope.read().image.convert("RGBA")
            row = rimg.crop((0, rimg.height // 2, rimg.width, rimg.height // 2 + 1))
            bbox = row.getchannel("A").getbbox()          # trim transparent edges
            if bbox:
                row = row.crop(bbox)
            g = ImageOps.grayscale(row.convert("RGB"))
            cable = Image.merge("RGBA", (g, g, g, row.split()[3]))
            if repaint("tongue stretch", cable, fill=True):
                n += 1
                log("[grappling_hook] cable -> grey Rope (1) colour (stretched)")
        except Exception as e:
            log(f"[grappling_hook] rope cable failed: {e}")

    # Launcher: the prefab has a GunSprite GameObject (the piece the player holds)
    # positioned by the devs but left with NO sprite. Point it at the metal launcher
    # art (Grapple_A_Shoot7). GunSprite starts empty so there is no original pivot to
    # preserve — a plain reference set is safe here (unlike the head/line).
    gun_sprite = None
    for nm in ("Grapple_A_Shoot7", "Grapple_A_Shoot6"):
        if nm in sprites:
            gun_sprite = sprites[nm].path_id
            break
    if gun_sprite is not None:
        byid = {o.path_id: o for o in env.objects}

        # Launcher (small) on top of the head — lowered + nudged left; and the cable
        # base (LineSprite) raised so less of the rope sits inside the character.
        def set_gun(go_id):
            nonlocal n
            go = byid[go_id].read()
            if go.m_Name in ("GunSprite", "LineSprite"):
                for c in go.m_Components:
                    comp = c.component if hasattr(c, "component") else c
                    o = byid.get(comp.path_id)
                    if not o:
                        continue
                    if go.m_Name == "GunSprite" and o.type.name == "SpriteRenderer":
                        tt = o.read_typetree()
                        tt["m_Sprite"] = {"m_FileID": 0, "m_PathID": gun_sprite}
                        tt["m_SortingOrder"] = 12  # draw in front of the cable (10)
                        o.save_typetree(tt)
                        n += 1
                    elif o.type.name == "Transform":
                        tt = o.read_typetree()
                        if go.m_Name == "GunSprite":
                            # bigger (hat-like) launcher sitting on the head
                            tt["m_LocalPosition"] = {"x": -6.0, "y": 21.0, "z": 0.0}
                            tt["m_LocalScale"] = {"x": 0.8, "y": 0.8, "z": 1.0}
                        else:  # LineSprite — start the cable inside the launcher HEAD
                            # so the rope connects to it; the hat hides the base, the
                            # rope emerges from the top (no stem in the body anymore).
                            p = tt.get("m_LocalPosition") or {"x": 0.0, "y": 12.0, "z": 0.0}
                            p["y"] = 21.0
                            tt["m_LocalPosition"] = p
                        o.save_typetree(tt)
                        n += 1
            for c in go.m_Components:
                comp = c.component if hasattr(c, "component") else c
                o = byid.get(comp.path_id)
                if o and o.type.name == "Transform":
                    for ch in o.read().m_Children:
                        set_gun(byid[ch.path_id].read().m_GameObject.path_id)
            return False

        for o in env.objects:
            if o.type.name == "MonoBehaviour":
                try:
                    d = o.read(check_read=False)
                    if d.m_Script.read().m_ClassName == "GrapplingHook":
                        set_gun(d.m_GameObject.path_id)
                        break
                except Exception:
                    pass

    # ANIMATION FIX. The hook's animation is fully built but its head clips point at
    # Lick's tongue frames. AnimationClip.m_ClipBindingConstant.pptrCurveMapping is
    # the runtime sprite list each clip cycles; the GrapplingHookHead clips use
    # Clawed* (tongue) while the body clips (Shoot/Break/Hide) already use the metal
    # Grapple_A_* frames. Repoint the head clips at the metal hook frames so the head
    # ANIMATES as the real metal hook instead of the tongue.
    pid = {}
    for o in env.objects:
        if o.type.name == "Sprite":
            try:
                pid[o.read().m_Name] = o.path_id
            except Exception:
                pass

    def fseq(*names):
        return [pid[x] for x in names if x in pid]

    # The hook top = the CLAW. HeadFlying already cycles Clawed1-4 (keep them); the
    # fix is to make the IDLE/latched state animate the same claw instead of sitting
    # on the pink tongue ball — done by cloning HeadFlying onto HeadIdle below.
    fly_frames = fseq("Clawed1", "Clawed2", "Clawed3", "Clawed4")
    fly_obj = idle_obj = None
    for o in env.objects:
        if o.type.name == "AnimationClip":
            try:
                nm = o.read_typetree().get("m_Name")
            except Exception:
                continue
            if nm == "HeadFlying":
                fly_obj = o
            elif nm == "HeadIdle":
                idle_obj = o
    if fly_obj and fly_frames:
        # Repoint the hook's flight animation to the metal hook frames.
        tt = fly_obj.read_typetree()
        pm = (tt.get("m_ClipBindingConstant") or {}).get("pptrCurveMapping") or []
        for i, entry in enumerate(pm):
            entry["m_PathID"] = fly_frames[i % len(fly_frames)]
        fly_obj.save_typetree(tt)
        n += 1
        log("[grappling_hook] HeadFlying -> animated metal hook frames")
        # Make the LATCHED hook keep animating, not freeze: clone HeadFlying's
        # animation onto HeadIdle (the attached state). HeadFlying drives only the
        # SpriteRenderer (one binding, no transform curve), so the clone adds the
        # frame-cycle with no stray motion; the idle state loops, so the hook stays
        # alive at the grab/anchor point the whole time it's attached.
        if idle_obj:
            itt = idle_obj.read_typetree()
            for k, v in tt.items():
                if k != "m_Name":
                    itt[k] = v
            idle_obj.save_typetree(itt)
            n += 1
            log("[grappling_hook] HeadIdle cloned from HeadFlying (latched hook loops)")

    # The LAUNCHER (GunSprite) is driven by the ROOT animator; its Idle clip shows a
    # blank placeholder 'LED_NoFruit20', so the launcher vanishes when not firing.
    # Repoint that idle frame to Shoot3 so the launcher is visible at rest (Shoot
    # clip = firing, Pulling = Shoot7 latched are already the metal launcher frames).
    # The launcher (bottom) is STATIC per the user's request — no animation. Its clips
    # bind to the GunSprite (path-hash 3769694503); point its Idle clip at a single
    # static Grapple_A_Shoot7 frame (was a blank external fileID-5 placeholder).
    LAUNCHER_PATH = 3769694503
    sh7 = fseq("Grapple_A_Shoot7")
    if sh7:
        for o in env.objects:
            if o.type.name != "AnimationClip":
                continue
            try:
                tt = o.read_typetree()
                if tt.get("m_Name") != "Idle":
                    continue
                cbc = tt.get("m_ClipBindingConstant") or {}
                if not any(b.get("path") == LAUNCHER_PATH
                           for b in (cbc.get("genericBindings") or [])):
                    continue
                pm = cbc.get("pptrCurveMapping") or []
                if not pm:
                    continue
                for e in pm:
                    e["m_FileID"] = 0
                    e["m_PathID"] = sh7[0]
                o.save_typetree(tt)
                n += 1
                log("[grappling_hook] launcher Idle -> static Shoot7")
            except Exception:
                pass

    # LAYERS (user idea): put the rope + claw BEHIND the player and the launcher hat
    # IN FRONT, so the bunch-up when the character reaches the top hides behind the
    # body while the hat stays visible. The player SR sits on sorting layer
    # (-7, id 1295371705, order 2); same layer, order < 2 = behind, > 2 = in front.
    # rope + claw -> the PLAYER's layer behind it; hat -> the DEFAULT layer (0), which
    # renders in front of the player, so the launcher stays on top of the head.
    sort_spec = {
        "LineSprite": (-7, 1295371705, -5),   # behind the player
        "HeadSprite": (-7, 1295371705, -5),   # behind the player
        "GunSprite": (0, 0, 12),              # default layer = in front of the player
    }
    _b = {o.path_id: o for o in env.objects}
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            go = o.read()
            spec = sort_spec.get(go.m_Name)
            if not spec:
                continue
            for c in go.m_Components:
                comp = c.component if hasattr(c, "component") else c
                co = _b.get(comp.path_id)
                if co and co.type.name == "SpriteRenderer":
                    tt = co.read_typetree()
                    (tt["m_SortingLayer"], tt["m_SortingLayerID"],
                     tt["m_SortingOrder"]) = spec
                    co.save_typetree(tt)
                    n += 1
        except Exception:
            pass
    log("[grappling_hook] sorted rope+claw behind player, hat in front")

    log(f"[grappling_hook] hook art done: head+cable repaint, launcher, animation ({n} edits)")
    swapped = n
    return swapped


def enable_puppet_projectile_sprites(env, prefab_names, *, log=print) -> int:
    """Make "thrower-puppet" projectiles self-sufficient. Some projectiles (the
    axe) have no script: they're normally driven by another enemy (the
    totemBoomeranger), which flips their SpriteRenderer on and animates it during
    the throw. Swapped onto a different shooter, the puppet spawns with a valid
    sprite reference but a DISABLED SpriteRenderer -> it's an invisible-but-deadly
    projectile. Enabling the renderer on the prefab makes it a standalone visible
    entity (the clone inherits m_Enabled), no boomeranger and no runtime hook
    needed. Returns the number of renderers enabled.
    """
    want = {n for n in prefab_names if n}
    if not want:
        return 0
    _b = {o.path_id: o for o in env.objects}
    n = 0
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            go = o.read()
            if go.m_Name not in want:
                continue
            for c in go.m_Components:
                comp = c.component if hasattr(c, "component") else c
                co = _b.get(comp.path_id)
                if not co or co.type.name != "SpriteRenderer":
                    continue
                tt = co.read_typetree()
                if not tt.get("m_Enabled"):
                    tt["m_Enabled"] = 1
                    co.save_typetree(tt)
                    n += 1
                    log(f"[puppet-projectile] enabled SpriteRenderer on "
                        f"{go.m_Name!r} prefab (self-visible)")
        except Exception:
            pass
    return n


def allow_cactus_variants(env, gen: "TreeGen", *, log=print) -> int:
    """cactusbig / cactussmall never spawn because no theme lists them: each
    ThemeFilter.allowedEnemies contains cactusmed but not big/small, so the engine
    refuses to instantiate them. Add both to every ThemeFilter that already allows
    cactusmed, so all three sizes spawn wherever the medium one does. Data-only —
    no prefab or code change. Returns the number of ThemeFilters updated."""
    gopid: dict[str, int] = {}
    for o in env.objects:
        if o.type.name == "GameObject":
            try:
                nm = o.read().m_Name
                if nm in ("cactusmed", "cactusbig", "cactussmall"):
                    gopid[nm] = o.path_id
            except Exception:
                pass
    if not all(k in gopid for k in ("cactusmed", "cactusbig", "cactussmall")):
        log("[typetree] cactus prefabs not all found; theme-allow skipped")
        return 0
    med = gopid["cactusmed"]
    add = [gopid["cactusbig"], gopid["cactussmall"]]

    def _shared2_fid(reader):
        """The file id that points at sharedassets2.assets (where the cactus
        prefabs live) FROM this reader's own serialized file — it varies per file."""
        try:
            for i, ext in enumerate(reader.assets_file.externals or []):
                if getattr(ext, "name", "").lower().endswith("sharedassets2.assets"):
                    return i + 1
        except Exception:
            pass
        return None

    def _augment_med(lst):
        """If cactusmed is in this PPtr list, append big/small with med's file id."""
        if not isinstance(lst, list):
            return False
        med_ref = next((e for e in lst if isinstance(e, dict) and e.get("m_PathID") == med), None)
        if med_ref is None:
            return False
        fid = med_ref.get("m_FileID", 2)
        have = {e.get("m_PathID") for e in lst if isinstance(e, dict)}
        ch = False
        for pid in add:
            if pid not in have:
                lst.append({"m_FileID": fid, "m_PathID": pid})
                ch = True
        return ch

    edited = 0
    # 1. theme gate: add big/small to EVERY theme's allowedEnemies so a cactus
    #    spawns regardless of the level's theme. allowedEnemies = [{minDate,
    #    gameObjects:[PPtr]}]; the cactus file id is resolved per serialized file.
    tf_nodes = gen.nodes("ThemeFilter")
    for tf in find_mono(env, "ThemeFilter"):
        try:
            tt = tf.read_typetree(tf_nodes)
        except Exception:
            continue
        ae = tt.get("allowedEnemies")
        if not isinstance(ae, list):
            continue
        fid = _shared2_fid(tf)
        if fid is None:
            continue                                # this file can't reference the cactus prefabs
        changed = False
        for grp in ae:
            gos = grp.get("gameObjects") if isinstance(grp, dict) else None
            if not isinstance(gos, list):
                continue
            have = {e.get("m_PathID") for e in gos if isinstance(e, dict)}
            for pid in add:
                if pid not in have:
                    gos.append({"m_FileID": fid, "m_PathID": pid})
                    changed = True
        if changed:
            tf.save_typetree(tt, tf_nodes)
            edited += 1
            log(f"[typetree] ThemeFilter {tt.get('m_Name') or ''}: allowed cactusbig + cactussmall")
    # 2. spawn-by-name registry: EnemyInstancer. Not being in these lists makes the
    #    engine fall back to backupEnemyBasic ("lips"), so add big/small next to med.
    ei_nodes = gen.nodes("EnemyInstancer")
    for ei in find_mono(env, "EnemyInstancer"):
        try:
            tt = ei.read_typetree(ei_nodes)
        except Exception:
            continue
        changed = False
        for fld in ("enemyFullList", "enemiesToSpawn"):
            if _augment_med(tt.get(fld)):
                changed = True
        for grp in (tt.get("masterEnemies") or []):
            if isinstance(grp, dict) and _augment_med(grp.get("enemies")):
                changed = True
        if changed:
            ei.save_typetree(tt, ei_nodes)
            edited += 1
            log("[typetree] EnemyInstancer: registered cactusbig + cactussmall (no more lips fallback)")
    return edited


def _shared2_index(reader):
    """1-based external file id for sharedassets2.assets from `reader`'s file (that's
    where every enemy prefab lives), or None if this file can't reference it."""
    try:
        for i, ext in enumerate(reader.assets_file.externals or []):
            if getattr(ext, "name", "").lower().endswith("sharedassets2.assets"):
                return i + 1
    except Exception:
        pass
    return None


def allow_all_elements_all_themes(env, gen: "TreeGen", *, log=print) -> int:
    """Make EVERY registered enemy spawnable in EVERY theme. The per-theme
    ThemeFilter.allowedEnemies is the engine's SPAWN POOL — at spawn it picks the
    enemy FROM this list, and anything not in it falls back to the "lips" basic
    enemy (that's why a code patch on isEnemyOnList does nothing). So we add the
    game's full enemy registry (EnemyInstancer.enemyFullList — all 88, all in
    sharedassets2) to every theme's allowedEnemies. All ThemeFilters live in one
    file and reference sharedassets2 by the same fid, so the sharedassets2 PATHIDS
    are reusable across themes with each theme's own sharedassets2 fid. Returns the
    number of ThemeFilters updated."""
    tfn = gen.nodes("ThemeFilter")
    # 1. the enemy set = the full registry (EnemyInstancer.enemyFullList) UNION every
    #    pathid any theme already allows. Both are sharedassets2 pathids. The union is
    #    needed because some enemies (e.g. woolyTrunkySr@17404) are referenced ONLY by
    #    a theme's allowedEnemies and are NOT in enemyFullList, and vice-versa.
    enemy_pids: set[int] = set()
    ein = gen.nodes("EnemyInstancer")
    for ei in find_mono(env, "EnemyInstancer"):
        try:
            tt = ei.read_typetree(ein)
        except Exception:
            continue
        exts = [getattr(x, "name", "") for x in (ei.assets_file.externals or [])]
        for p in (tt.get("enemyFullList") or []):
            if not isinstance(p, dict):
                continue
            fid, pid = p.get("m_FileID"), p.get("m_PathID")
            fn = "" if fid == 0 else (exts[fid - 1] if 0 < fid <= len(exts) else "")
            if pid is not None and fn.lower().endswith("sharedassets2.assets"):
                enemy_pids.add(pid)
    for tf in find_mono(env, "ThemeFilter"):
        try:
            tt = tf.read_typetree(tfn)
        except Exception:
            continue
        sa2 = _shared2_index(tf)
        if sa2 is None:
            continue
        for grp in (tt.get("allowedEnemies") or []):
            for e in (grp.get("gameObjects") or []) if isinstance(grp, dict) else []:
                if isinstance(e, dict) and e.get("m_FileID") == sa2 and e.get("m_PathID") is not None:
                    enemy_pids.add(e.get("m_PathID"))
    if not enemy_pids:
        log("[typetree] allow-all-in-every-theme: no enemies found; skipped")
        return 0
    # 2. add every enemy to every theme's allowedEnemies (each group / date range)
    edited = 0
    for tf in find_mono(env, "ThemeFilter"):
        try:
            tt = tf.read_typetree(tfn)
        except Exception:
            continue
        sa2 = _shared2_index(tf)
        groups = tt.get("allowedEnemies")
        if sa2 is None or not isinstance(groups, list) or not groups:
            continue
        changed = False
        for grp in groups:
            gos = grp.get("gameObjects") if isinstance(grp, dict) else None
            if not isinstance(gos, list):
                continue
            have = {e.get("m_PathID") for e in gos
                    if isinstance(e, dict) and e.get("m_FileID") == sa2}
            for pid in enemy_pids:
                if pid not in have:
                    gos.append({"m_FileID": sa2, "m_PathID": pid}); changed = True
        if changed:
            tf.save_typetree(tt, tfn)
            edited += 1
    # 3. spawn-by-name registry: getEnemyToSpawnFromRange -> findEnemyObject resolves
    #    the authored enemy name against EnemyInstancer's lists; an enemy not in them
    #    falls back to backupEnemyBasic ("lips"). Register every enemy in every list
    #    (enemyFullList / enemiesToSpawn / masterEnemies groups) so any placed enemy
    #    resolves. Uses EnemyInstancer's own sharedassets2 fid.
    reg = 0
    for ei in find_mono(env, "EnemyInstancer"):
        try:
            tt = ei.read_typetree(ein)
        except Exception:
            continue
        sa2 = _shared2_index(ei)
        if sa2 is None:
            continue
        changed = False

        def _add_all(lst):
            nonlocal changed
            if not isinstance(lst, list):
                return
            have = {e.get("m_PathID") for e in lst
                    if isinstance(e, dict) and e.get("m_FileID") == sa2}
            for pid in enemy_pids:
                if pid not in have:
                    lst.append({"m_FileID": sa2, "m_PathID": pid}); changed = True

        _add_all(tt.get("enemyFullList"))
        _add_all(tt.get("enemiesToSpawn"))
        for grp in (tt.get("masterEnemies") or []):
            if isinstance(grp, dict):
                _add_all(grp.get("enemies"))
        if changed:
            ei.save_typetree(tt, ein)
            reg += 1
    log(f"[typetree] allow-all-enemies-in-every-theme: {len(enemy_pids)} enemies "
        f"-> {edited} ThemeFilter(s) + {reg} EnemyInstancer(s) updated")
    return edited + reg


def scale_cactus_variants(env, gen: "TreeGen", *, log=print) -> int:
    """cactusbig / cactussmall crash on spawn in this game version while cactusmed
    works (their prefabs are otherwise identical to med — only the unique giant /
    small sprite sets differ). Rebuild big/small FROM med: copy med's sprites and
    collider onto them, then scale the transform so big stands ~7 blocks and small
    ~3 (med = 5). They then spawn med's proven art at the right sizes instead of
    the crashing variants. Returns the number of prefabs rebuilt."""
    by_name: dict[str, list] = {}
    for o in env.objects:
        if o.type.name == "GameObject":
            try:
                by_name.setdefault(o.read().m_Name, []).append(o)
            except Exception:
                pass
    if "cactusmed" not in by_name:
        return 0
    anim_nodes = gen.nodes("EnemyAnimWalking")
    SPRITE_ARRAYS = ("walkingSprites", "appearingSprites", "disappearingSprites",
                     "dyingSprites", "stompedSprites")

    def _readers(go):
        for c in getattr(go.read(), "m_Components", []):
            ref = c.component if hasattr(c, "component") else c
            try:
                yield ref.deref()
            except Exception:
                continue

    def _find(go, typ=None, cls=None):
        for r in _readers(go):
            if typ and r.type.name == typ:
                return r
            if cls and r.type.name == "MonoBehaviour" and script_class(r) == cls:
                return r
        return None

    med = by_name["cactusmed"][0]
    m_anim = _find(med, cls="EnemyAnimWalking")
    m_sr = _find(med, typ="SpriteRenderer")
    m_box = _find(med, typ="BoxCollider2D")
    m_tr = _find(med, typ="Transform")
    if not (m_anim and m_sr and m_box and m_tr):
        log("[typetree] cactusmed components incomplete; cactus scaling skipped")
        return 0
    m_anim_tt = m_anim.read_typetree(anim_nodes)
    m_sprite = m_sr.read_typetree().get("m_Sprite")
    m_box_tt = m_box.read_typetree()
    m_scale = m_tr.read_typetree().get("m_LocalScale", {"x": 1.0, "y": 1.0, "z": 1.0})

    edited = 0
    for name, factor in (("cactusbig", 1.4), ("cactussmall", 0.6)):
        for go in by_name.get(name, []):
            anim = _find(go, cls="EnemyAnimWalking")
            sr = _find(go, typ="SpriteRenderer")
            box = _find(go, typ="BoxCollider2D")
            tr = _find(go, typ="Transform")
            if not (anim and sr and box and tr):
                continue
            att = anim.read_typetree(anim_nodes)
            for k in SPRITE_ARRAYS:
                if k in m_anim_tt and k in att:
                    att[k] = m_anim_tt[k]
            anim.save_typetree(att, anim_nodes)
            st = sr.read_typetree()
            if m_sprite is not None:
                st["m_Sprite"] = m_sprite
            sr.save_typetree(st)
            bt = box.read_typetree()
            for k in ("m_Size", "m_Offset", "m_SpriteTilingProperty"):
                if k in m_box_tt:
                    bt[k] = m_box_tt[k]
            box.save_typetree(bt)
            tt = tr.read_typetree()
            tt["m_LocalScale"] = {"x": m_scale["x"] * factor,
                                  "y": m_scale["y"] * factor,
                                  "z": m_scale.get("z", 1.0)}
            tr.save_typetree(tt)
            edited += 1
            log(f"[typetree] {name}: rebuilt from cactusmed, scale ×{factor} "
                f"(~{int(round(5 * factor))} blocks)")
    return edited


def force_powerup_box_grappling_hook(env, gen: "TreeGen", *, log=print) -> int:
    """Set every RewardPowerupBox.displayPowerups[*].type to GRAPPLING_HOOK (13) so
    a placed "Powerup reward" box always grants the grappling hook through the
    game's real powerup activation (correct metal-hook sprite). Pairs with the
    `grappling_hook` .so patches (which un-gate the box: claimable, no ad, never
    'used'); the game persists powerups across death, so one claim = permanent on
    any character. Returns the number of display entries retyped.
    """
    nodes = gen.nodes("RewardPowerupBox")
    boxes = find_mono(env, "RewardPowerupBox")
    if not boxes:
        log("[grappling_hook] no RewardPowerupBox in bundle; data edit skipped")
        return 0
    edited = 0
    for obj in boxes:
        tree = obj.read_typetree(nodes)
        dp = tree.get("displayPowerups")
        if not dp:
            continue
        for entry in dp:
            if "type" in entry:
                entry["type"] = 13          # EmptySlot.PowerUp.GRAPPLING_HOOK
        obj.save_typetree(tree, nodes)
        edited += len(dp)
    log(f"[grappling_hook] powerup box -> always GRAPPLING_HOOK "
        f"({edited} display entries)")
    return edited


def clone_skin_onto_lick(env, gen: "TreeGen", source_id: int,
                         lick_id: int = 1, *, log=print) -> int:
    """Overwrite Lick's CharacterPack visuals with character `source_id`'s, while
    preserving Lick's characterID and grappleAnim. With force_character -> Lick,
    the player then LOOKS like source_id but keeps the working grappling hook.
    """
    import copy
    nodes = gen.nodes("CharacterManager")
    mgrs = find_mono(env, "CharacterManager")
    if not mgrs:
        raise LookupError("no CharacterManager MonoBehaviour found")
    edited = 0
    for obj in mgrs:
        tree = obj.read_typetree(nodes)
        packs = tree.get("characterPacks")
        if not packs:
            continue
        src = next((p for p in packs if p.get("characterID") == source_id), None)
        lick = next((p for p in packs if p.get("characterID") == lick_id), None)
        if not src or not lick:
            log(f"[grapple-skin] character {source_id} or Lick pack not found; skipped")
            continue
        for f in SKIN_FIELDS:
            if f in src and f in lick:
                lick[f] = copy.deepcopy(src[f])
        obj.save_typetree(tree, nodes)
        edited += 1
    log(f"[grapple-skin] put character {source_id}'s look onto Lick "
        f"(keeps grapple)")
    return edited


def grapple_use_jump(env, gen: "TreeGen", *, log=print) -> int:
    """Make the grappling-hook pose show each character's JUMP sprite.

    The player has a dedicated grapple animation (Player.grappleSprites / .grapple)
    fed from CharacterPack.grappleAnim (List<Sprite>) via Player.SetCharacter().
    grappleAnim is empty for every non-Lick character (grapple was Lick-only), so
    when you grapple with the powerup the game falls back to the fall sprite. Point
    each pack's grappleAnim at a single-frame list holding its own jumpSprite, so
    grappling shows the jump pose instead."""
    import copy
    nodes = gen.nodes("CharacterManager")
    mgrs = find_mono(env, "CharacterManager")
    if not mgrs:
        raise LookupError("no CharacterManager MonoBehaviour found")
    edited = chars = 0
    for obj in mgrs:
        tree = obj.read_typetree(nodes)
        packs = tree.get("characterPacks") or []
        changed = False
        for p in packs:
            if p.get("grappleAnim"):        # already has a real grapple (Lick) -> keep
                continue
            js = p.get("jumpSprite")
            if not js or not js.get("m_PathID"):   # no jump sprite -> leave as-is
                continue
            p["grappleAnim"] = [copy.deepcopy(js)]
            chars += 1
            changed = True
        if changed:
            obj.save_typetree(tree, nodes)
            edited += 1
    log(f"[grapple-pose] grappling now uses the jump sprite ({chars} characters)")
    return edited


def script_class(obj) -> str | None:
    """m_ClassName of a MonoBehaviour ObjectReader, or None if unreadable.

    Uses check_read=False so the base MonoBehaviour parses even though the
    custom script has no type tree yet.
    """
    if obj.type.name != "MonoBehaviour":
        return None
    try:
        base = obj.read(check_read=False)
        ms = base.m_Script
        if ms is None:
            return None
        return ms.read().m_ClassName
    except Exception:
        return None


def find_mono(env, cls: str) -> list:
    """All ObjectReaders in `env` whose MonoBehaviour script class == cls."""
    return [o for o in env.objects if script_class(o) == cls]


def flag_style_checkpoints(env, gen: "TreeGen", *, log=print) -> int:
    """Reskin every checkpoint Chest as a raised FLAG, and make it non-blocking.

    Keeps the real checkpoint trigger (Chest/Door proximity + auto-checkpoint)
    100% intact — only the *appearance* and *collision* change:
      * `Chest.animationSprites` -> the 12 `flag_open-*` sprite frames (renders
        at the flag's native, smaller size).
      * the chest's `BoxCollider2D` (`Chest.bc`) -> `m_IsTrigger = true`, so the
        player passes through instead of being blocked (the collider stays live
        for detection). Pair with VIP-auto mode so the Door auto-opens too.

    Global: applies to all 4 checkpoint chests (advert / generic / premium /
    premium-fruit). Returns the number of chests reskinned.
    """
    frames = []
    for o in env.objects:
        if o.type.name == "Sprite":
            nm = str(getattr(o.read(), "m_Name", "") or "")
            m = re.fullmatch(r"flag_open-(\d+)", nm)
            if m:
                frames.append((int(m.group(1)), o.path_id))
    if not frames:
        log("[flag_checkpoints] no flag_open sprites found — skipped")
        return 0
    frames.sort()
    ppt = [{"m_FileID": 0, "m_PathID": pid} for _, pid in frames]
    nodes = gen.nodes("Chest")
    by_id = {o.path_id: o for o in env.objects}
    n = 0
    for c in find_mono(env, "Chest"):
        tree = c.read_typetree(nodes)
        tree["animationSprites"] = list(ppt)          # chest anim -> flag frames
        c.save_typetree(tree, nodes)                  # MUST pass nodes (IL2CPP: no embedded tree)
        bc = by_id.get((tree.get("bc") or {}).get("m_PathID"))
        if bc is not None:                            # collider -> non-blocking trigger
            ct = bc.read_typetree()
            ct["m_IsTrigger"] = True
            bc.save_typetree(ct)
        n += 1
    log(f"[flag_checkpoints] reskinned {n} chest(s) as flags ({len(ppt)} frames), colliders -> trigger")
    return n


def override_level_lists(env, gen: TreeGen, overrides: dict[str, list[str]],
                         *, log=print) -> int:
    """Replace ordered-list fields on the `Level` MonoBehaviour (the master key).

    `overrides` maps a Level field name (e.g. "endChunksList") to the full list
    of chunk-path strings it should hold. Only the listed fields are touched;
    every other field round-trips unchanged. Returns the number of Level
    instances edited (normally 1). Raises if the field is absent or not a list.
    """
    nodes = gen.nodes("Level")
    levels = find_mono(env, "Level")
    if not levels:
        raise LookupError("no Level MonoBehaviour found in bundle")
    edited = 0
    for obj in levels:
        tree = obj.read_typetree(nodes)
        for fld, value in overrides.items():
            if fld not in tree:
                raise KeyError(f"Level has no serialized field {fld!r}")
            if not isinstance(tree[fld], list):
                raise TypeError(f"Level.{fld} is not a list (got "
                                f"{type(tree[fld]).__name__})")
            old = len(tree[fld])
            tree[fld] = list(value)
            log(f"[typetree] Level.{fld}: {old} -> {len(value)} entries")
        obj.save_typetree(tree, nodes)
        edited += 1
    return edited
