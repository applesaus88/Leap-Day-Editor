"""
sprites.py — resolve a chunk tile/enemy token to its real sprite image.

Chunk tokens (e.g. "generic_06", "fruit0") are PREFAB names. The art is found
by: token -> GameObject prefab -> its SpriteRenderer.m_Sprite -> Sprite.image
(SpriteRenderer is a built-in Unity type, readable without IL2CPP type trees).
A few tokens have a same-named Sprite directly; that's the fallback.

Images are returned as base64 PNG data URIs (small 16x16-ish pixel art), cached
per token, so the editor can paint with the actual game art.
"""

from __future__ import annotations

import base64
import io
import math

from PIL import Image

# Curated `properties` -> asset-name aliases for enemies whose property string
# doesn't match any GameObject/Sprite (the game maps these in code, so there's
# no naming rule). Only verified-correct entries belong here.
ALIAS = {
    "sawblade": "saw-0",
    "flippers": "flipper_1",
    "balloon": "balloon_1",
    "yolkcannon": "yolk_cannon",
    "bulletcannon": "bullet",
    "spring": "spring_0",
}

# Max trusted within-cell sprite offset (in cells). A child sprite further than
# this from the cell origin is almost always an effect/sub-part the resolver's
# DFS hit, not the tile's art, so its offset is ignored rather than applied.
OFFSET_LIMIT = 2.5


class SpriteResolver:
    def __init__(self, env, so_bytes: bytes | None = None,
                 metadata_bytes: bytes | None = None,
                 unity_version: str = "2022.3.62f1",
                 overrides: dict | None = None):
        self.env = env
        self._go: dict | None = None       # name -> GameObject reader
        self._spr: dict | None = None      # name -> Sprite reader (lazy)
        self._by_id: dict | None = None    # path_id -> object reader (lazy)
        self._cache: dict[str, str | None] = {}
        # Hand-authored per-token fixes (from tiles/sprite_overrides.json): the
        # resolver gets most art/pivots right, but some composite/placeholder
        # tiles need a nudged draw anchor or a manual direction arrow. These are
        # merged onto the resolved record in get(); see set_override/clear_override.
        self._overrides: dict[str, dict] = dict(overrides or {})
        # READ-ONLY type-tree support, only for *finding* sprites that live in
        # custom script components (e.g. a Mace trap's ballPrefab). This never
        # writes anything — it only reads sprite references for the preview.
        self._so = so_bytes
        self._meta = metadata_bytes
        self._uver = unity_version
        self._gen = None                   # TypeTreeGenerator (lazy)
        self._nodes: dict = {}             # class -> typetree nodes (json)
        self._gen_failed = False

    # ---- type-tree (read-only, for script-held sprites) -------------------
    def _generator(self):
        if self._gen is not None or self._gen_failed:
            return self._gen
        if not (self._so and self._meta):
            self._gen_failed = True
            return None
        try:
            from TypeTreeGeneratorAPI import TypeTreeGenerator
            g = TypeTreeGenerator(self._uver, "AssetsTools")
            g.load_il2cpp(self._so, self._meta)
            self._gen = g
        except Exception:
            self._gen_failed = True
        return self._gen

    def _typetree(self, cls: str):
        if cls in self._nodes:
            return self._nodes[cls]
        g = self._generator()
        nodes = None
        if g is not None:
            try:
                import json as _json
                nodes = _json.loads(g.get_nodes_as_json("Assembly-CSharp", cls))
            except Exception:
                nodes = None
        self._nodes[cls] = nodes
        return nodes

    def _index_id(self):
        if self._by_id is None:
            self._by_id = {o.path_id: o for o in self.env.objects}

    # ---- lazy name indexes ------------------------------------------------
    def _index_go(self):
        if self._go is not None:
            return
        self._go = {}
        for o in self.env.objects:
            if o.type.name == "GameObject":
                try:
                    self._go.setdefault(o.read().m_Name, o)
                except Exception:
                    pass

    def _index_spr(self):
        if self._spr is not None:
            return
        self._spr = {}
        for o in self.env.objects:
            if o.type.name == "Sprite":
                try:
                    self._spr.setdefault(o.read().m_Name, o)
                except Exception:
                    pass

    # ---- resolution -------------------------------------------------------
    @staticmethod
    def _components(go_data):
        out = []
        for comp in getattr(go_data, "m_Components", []):
            for getter in (lambda: comp.component, lambda: comp):
                try:
                    out.append(getter().read())
                    break
                except Exception:
                    continue
        return out

    @staticmethod
    def _ctype(c):
        try:
            return c.object_reader.type.name
        except Exception:
            return type(c).__name__

    def _scale_x(self, comps) -> float:
        for c in comps:
            if self._ctype(c) in ("Transform", "RectTransform"):
                s = getattr(c, "m_LocalScale", None)
                if s is not None:
                    try:
                        return float(s.x)
                    except Exception:
                        return 1.0
        return 1.0

    def _local_pos(self, comps) -> tuple[float, float]:
        """This GameObject transform's local (x, y) position in world units.
        Leap Day positions some sprites off the cell origin (a spring sits above
        its candle, a book sits beside the bar); the editor must apply that or the
        art lands at the wrong spot. 1 unit == 1 cell (CELL px)."""
        for c in comps:
            if self._ctype(c) in ("Transform", "RectTransform"):
                p = getattr(c, "m_LocalPosition", None)
                if p is None:
                    return 0.0, 0.0
                try:
                    return float(p.x), float(p.y)
                except Exception:
                    return 0.0, 0.0
        return 0.0, 0.0

    def _rot_z(self, comps) -> float:
        """Local Z rotation (degrees) of this GameObject's transform. Many
        directional assets (springs, fans, …) share ONE sprite and differ only
        by the prefab transform's z-rotation — e.g. trap120/121/122/123 are all
        `spring_0` rotated 0/-90/180/90. The resolver must read that rotation,
        else every variant draws identically."""
        for c in comps:
            if self._ctype(c) in ("Transform", "RectTransform"):
                q = getattr(c, "m_LocalRotation", None)
                if q is None:
                    return 0.0
                try:
                    x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
                except Exception:
                    return 0.0
                return math.degrees(math.atan2(2 * (w * z + x * y),
                                               1 - 2 * (y * y + z * z)))
        return 0.0

    def _sprite_in_go(self, go_data, depth=0, seen=None, scale_x=1.0, rot=0.0,
                      ox=0.0, oy=0.0):
        """Find the first SpriteRenderer sprite on this GameObject or any
        descendant (enemy art often lives on a child). Returns (sprite, flip,
        rot, ox, oy) where flip means the cumulative transform scale.x (or
        SpriteRenderer flipX) is negative — Leap Day faces things left/right by
        mirroring with scale.x = -1, not by separate sprites — rot is the
        cumulative z-rotation (degrees) down to the sprite node, and (ox, oy) is
        the cumulative local-position offset in world units/cells (so a sprite
        the prefab shifts off the cell origin draws where the game puts it).
        Built-in types only."""
        if seen is None:
            seen = set()
        comps = self._components(go_data)
        sx = scale_x * self._scale_x(comps)
        rz = rot + self._rot_z(comps)
        # the ROOT's own local position is the prefab's scene position — the game
        # overrides it by placing the prefab at the cell, so ignore it. Only
        # CHILD offsets (relative to the root) shift the art within the cell.
        if depth == 0:
            ax, ay = ox, oy
        else:
            lx, ly = self._local_pos(comps)
            ax, ay = ox + scale_x * lx, oy + ly      # parent scale mirrors x offset
        for c in comps:
            if self._ctype(c) == "SpriteRenderer":
                sp = getattr(c, "m_Sprite", None)
                if sp:
                    try:
                        s = sp.read()
                        # skip the blank "empty" placeholder many prefabs put on
                        # the root — the real art is on a child.
                        if s is not None and getattr(s, "m_Name", "") != "empty":
                            fx = bool(getattr(c, "m_FlipX", False))
                            return s, (sx < 0) ^ fx, rz, ax, ay
                    except Exception:
                        pass
        if depth >= 5:
            return None, False, 0.0, 0.0, 0.0
        for c in comps:
            if self._ctype(c) in ("Transform", "RectTransform"):
                for ch in getattr(c, "m_Children", []):
                    try:
                        chtr = ch.read()
                        pid = chtr.m_GameObject.m_PathID
                        if pid in seen:
                            continue
                        seen.add(pid)
                        res = self._sprite_in_go(chtr.m_GameObject.read(),
                                                 depth + 1, seen, sx, rz, ax, ay)
                        if res[0] is not None:
                            return res
                    except Exception:
                        continue
        return None, False, 0.0, 0.0, 0.0

    def _sprite_via_prefab(self, go_obj):
        return self._sprite_in_go(go_obj.read())

    _SPRITE_HINTS = ("sprite", "ball", "head", "body", "main", "icon")

    @staticmethod
    def _collect_pptrs(value, name, out, depth=0):
        if depth > 6:
            return
        if isinstance(value, dict):
            if "m_PathID" in value:
                pid = value.get("m_PathID")
                if pid:
                    out.append((name, pid))
                return
            for k, v in value.items():
                SpriteResolver._collect_pptrs(v, k, out, depth + 1)
        elif isinstance(value, list):
            for v in value:
                SpriteResolver._collect_pptrs(v, name, out, depth + 1)

    def _sprite_via_script(self, go_data, seen=None):
        """When a token's GameObject only has the blank `empty` placeholder, its
        real art lives in a custom script's prefab/sprite reference (e.g. a Mace
        trap's ballPrefab -> mace_ball). Read that reference via type tree and
        resolve it. Read-only — never writes."""
        if self._generator() is None:
            return None, False, 0.0, 0.0, 0.0
        if seen is None:
            seen = set()
        self._index_id()
        for comp in getattr(go_data, "m_Components", []):
            obj = comp.component if hasattr(comp, "component") else comp
            try:
                r = obj.deref()
                if r.type.name != "MonoBehaviour":
                    continue
                cls = r.read(check_read=False).m_Script.read().m_ClassName
            except Exception:
                continue
            nodes = self._typetree(cls)
            if not nodes:
                continue
            try:
                tree = r.read_typetree(nodes)
            except Exception:
                continue
            pptrs: list = []
            self._collect_pptrs(tree, "", pptrs)
            # prefer fields that look like the main visual
            pptrs.sort(key=lambda np: 0 if any(h in np[0].lower() for h in self._SPRITE_HINTS) else 1)
            for _name, pid in pptrs:
                if pid in seen:
                    continue
                seen.add(pid)
                ref = self._by_id.get(pid)
                if ref is None:
                    continue
                try:
                    if ref.type.name == "Sprite":
                        return ref.read(), False, 0.0, 0.0, 0.0
                    if ref.type.name == "GameObject":
                        sp, flip, rot, ox, oy = self._sprite_in_go(ref.read())
                        if sp is not None:
                            return sp, flip, rot, ox, oy
                except Exception:
                    continue
        return None, False, 0.0, 0.0, 0.0

    @staticmethod
    def _to_record(sprite) -> dict | None:
        try:
            img = sprite.image
        except Exception:
            return None
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        # pivot (fraction from bottom-left); the game positions a tile so its
        # pivot sits at the cell origin, which is why wide/odd sprites overflow
        # in a specific direction. Default to top-left if unavailable.
        px, py = 0.0, 1.0
        try:
            piv = sprite.m_Pivot
            px, py = float(piv.x), float(piv.y)
        except Exception:
            pass
        return {"uri": uri, "w": img.width, "h": img.height, "px": px, "py": py}

    @staticmethod
    def _rotate_record(rec: dict, angle: float) -> dict:
        """Bake a `@<deg>` rotation into the sprite: rotate the image about its
        pivot and recompute the pivot so a normal pivot-blit lands it exactly
        where the game draws it. (Doing it here keeps the editor's draw simple
        and correct — canvas-side rotation of corner-pivoted bars was off.)"""
        raw = base64.b64decode(rec["uri"].split(",", 1)[1])
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        w, h, px, py = rec["w"], rec["h"], rec["px"], rec["py"]
        im2 = im.rotate(-angle, expand=True, resample=Image.NEAREST)
        pvx, pvy = px * w, (1 - py) * h            # pivot pixel (from top-left)
        a = math.radians(-angle); ca, sa = math.cos(a), math.sin(a)
        ox, oy = pvx - w / 2, pvy - h / 2
        nx = ox * ca - oy * sa + im2.width / 2     # pivot pixel in rotated image
        ny = ox * sa + oy * ca + im2.height / 2
        buf = io.BytesIO(); im2.save(buf, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        return {**rec, "uri": uri, "w": im2.width, "h": im2.height,
                "px": nx / im2.width, "py": 1 - ny / im2.height}

    @staticmethod
    def _mirror_record(rec: dict) -> dict:
        """Horizontally flip a sprite (and its pivot) for left/right facing."""
        raw = base64.b64decode(rec["uri"].split(",", 1)[1])
        im = Image.open(io.BytesIO(raw)).convert("RGBA").transpose(Image.FLIP_LEFT_RIGHT)
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return {**rec, "uri": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                "px": 1.0 - rec["px"]}

    # Sprites the prefab carries as a stand-in, NOT the real art — when the
    # resolved SpriteRenderer sprite is one of these (and doesn't match the
    # token), fall back to a token-named sprite. Matched by exact name or prefix.
    # A real differently-named sprite (e.g. `basic`->`lips_run_0`, `IceBlob`->
    # `ice_blob`) is NOT a placeholder, so it's trusted as-is.
    _PLACEHOLDER_EXACT = {"empty", "snotbubble", "cable_placeholder"}
    _PLACEHOLDER_PREFIX = ("generic_", "tile_", "city-move-platform",
                           "rotate_block", "autotile")

    def _is_placeholder(self, name: str) -> bool:
        n = (name or "").lower()
        if n in self._PLACEHOLDER_EXACT or "placeholder" in n:
            return True
        return any(n.startswith(p) for p in self._PLACEHOLDER_PREFIX)

    # frames that aren't the "resting/idle" look — skip when picking a token's
    # representative sprite so we don't land on a death/hit/effect/sub-part frame.
    _BAD_FRAME = ("death", "die", "dead", "hit", "extend", "_to_", "origin",
                  "shadow", "flash", "placeholder", "select", "icon", "_text",
                  "button", "spawn", "appear", "explo", "particle", "selfie")
    # frames that ARE a good resting look — preferred when several match a token.
    _GOOD_FRAME = ("idle", "_run", "running", "walk", "move", "fly", "spin",
                   "loop", "anim")

    @staticmethod
    def _norm(s: str) -> str:
        return s.lower().replace("_", "").replace("-", "")

    def _token_named_sprite(self, base: str, loose: bool = False):
        """A sprite named after the token (e.g. bird -> bird_01), excluding
        end-state frames; the shortest/earliest such frame (the idle look). Used
        to override a wrong placeholder root sprite. With loose=True, match
        case- and separator-insensitively (wall_flower -> wallflower_idle_0,
        totem_snake -> totem-snake-..., conveyor -> conveyor_left_0) — used only
        as a last resort for a token that otherwise resolves to nothing."""
        self._index_spr()
        if loose:
            nb = self._norm(base)
            cands = [n for n in self._spr
                     if self._norm(n).startswith(nb) and len(self._norm(n)) > len(nb)
                     and not any(k in n.lower() for k in self._BAD_FRAME)]
        else:
            bl = base.lower()
            cands = [n for n in self._spr
                     if n.lower().startswith(bl) and len(n) > len(base)
                     and n[len(base)] in "_-0123456789"
                     and not any(k in n.lower() for k in self._BAD_FRAME)]
        if not cands:
            return None
        # prefer an idle/run-type frame, then the shortest/earliest name.
        cands.sort(key=lambda n: (0 if any(g in n.lower() for g in self._GOOD_FRAME)
                                  else 1, len(n), n))
        return self._spr[cands[0]].read()

    # ---- composite tiles -------------------------------------------------
    # Some tiles have NO single sprite that matches the game — the in-game
    # visual is assembled at runtime from sub-prefabs (a Mace = peg + chain x N
    # + ball; a single ball/placeholder sprite is unrecognisable). For those we
    # stitch the parts into one representative image, keyed by the prefab's
    # behaviour script class.
    def _script_classes(self, go_data) -> list:
        out = []
        for comp in getattr(go_data, "m_Components", []):
            obj = comp.component if hasattr(comp, "component") else comp
            try:
                r = obj.deref() if hasattr(obj, "deref") else obj
                if r.type.name == "MonoBehaviour":
                    out.append(r.read(check_read=False).m_Script.read().m_ClassName)
            except Exception:
                continue
        return out

    def _read_script_tree(self, go_data, cls):
        nodes = self._typetree(cls)
        if not nodes:
            return None
        for comp in getattr(go_data, "m_Components", []):
            obj = comp.component if hasattr(comp, "component") else comp
            try:
                r = obj.deref()
                if r.type.name != "MonoBehaviour":
                    continue
                if r.read(check_read=False).m_Script.read().m_ClassName != cls:
                    continue
                return r.read_typetree(nodes)
            except Exception:
                continue
        return None

    def _part_image(self, pid):
        """PIL image for a referenced sub-prefab/sprite (peg, chain, ball...)."""
        if not pid:
            return None
        self._index_id()
        ref = self._by_id.get(pid)
        if ref is None:
            return None
        try:
            if ref.type.name == "Sprite":
                return ref.read().image.convert("RGBA")
            if ref.type.name == "GameObject":
                sp = self._sprite_in_go(ref.read())[0]
                if sp is not None:
                    return sp.image.convert("RGBA")
        except Exception:
            return None
        return None

    def _composite_mace(self, go_data):
        if self._generator() is None:
            return None
        tree = self._read_script_tree(go_data, "Mace")
        if tree is None:
            return None
        pid = lambda f: (tree.get(f) or {}).get("m_PathID", 0)
        peg = self._part_image(pid("pegPrefab"))
        chain = self._part_image(pid("chainPrefab"))
        ball = self._part_image(pid("ballPrefab"))
        if ball is None and chain is None:
            return None
        n = max(1, min(int(tree.get("chainLengthInTiles") or 3), 6))
        TILE = 16
        parts, y = [], 0                         # vertical: peg, chain x n, ball
        if peg is not None:
            parts.append((peg, y)); y += peg.height
        else:
            y += TILE
        if chain is not None:
            for _ in range(n):
                parts.append((chain, y)); y += chain.height
        else:
            y += n * TILE
        if ball is not None:
            parts.append((ball, y)); y += ball.height
        W = max(im.width for im, _ in parts)
        canvas = Image.new("RGBA", (W, y), (0, 0, 0, 0))
        for im, yt in parts:
            canvas.paste(im, ((W - im.width) // 2, yt), im)
        buf = io.BytesIO(); canvas.save(buf, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        # pivot = the peg (the anchored tile), top-centre, so the mace hangs DOWN
        peg_w = peg.width if peg is not None else TILE
        return {"uri": uri, "w": W, "h": y,
                "px": ((W - peg_w) // 2) / W, "py": 1.0}

    # script class -> composite builder
    def _composite(self, go_data):
        classes = self._script_classes(go_data)
        if "Mace" in classes:
            return self._composite_mace(go_data)
        return None

    def _resolve_base(self, base: str) -> dict | None:
        base = ALIAS.get(base, base)
        rec = None
        try:
            self._index_go()
            go_data = self._go[base].read() if base in self._go else None
            if go_data is not None:                 # composite (mace, ...) wins
                comp = self._composite(go_data)
                if comp is not None:
                    return comp
            sp, flip, rot, ox, oy = None, False, 0.0, 0.0, 0.0
            if go_data is not None:
                sp, flip, rot, ox, oy = self._sprite_in_go(go_data)
                if sp is None:                      # art held in a custom script
                    sp, flip, rot, ox, oy = self._sprite_via_script(go_data)
            if sp is None:
                self._index_spr()
                if base in self._spr:
                    sp = self._spr[base].read()
            # last resort: a token with NO prefab/sprite of its own (many trap
            # `enemy` props — conveyor, firecannon, wall_flower, laseremitter… —
            # whose art lives under a similarly-named sprite). Match loosely
            # (case/separator-insensitive) so the editor shows representative art
            # instead of a blank. Only when nothing above resolved.
            if sp is None:
                alt = self._token_named_sprite(base, loose=True)
                if alt is not None:
                    sp = alt
            # guard against a wrong PLACEHOLDER on the prefab root (e.g. the
            # `bird` prefab's root sprite is `snotbubble`, `spike` is `generic_08`):
            # only when the resolved sprite is a known placeholder AND unrelated to
            # the token, prefer a token-named sprite. A real differently-named
            # sprite (`basic`->`lips_run_0`, `IceBlob`->`ice_blob`) is the actual
            # art the prefab ships, so it's trusted — that's what the game draws.
            if sp is not None:
                try:
                    related = sp.m_Name.lower().startswith(base.lower())
                    placeholder = self._is_placeholder(sp.m_Name)
                except Exception:
                    related, placeholder = True, False
                if not related and placeholder:
                    alt = self._token_named_sprite(base)
                    if alt is not None:
                        sp, flip, rot, ox, oy = alt, False, 0.0, 0.0, 0.0
            if sp is not None:
                rec = self._to_record(sp)
                # bake the prefab transform's z-rotation (screen rotation is the
                # negative of Unity's y-up z-euler) so directional variants face
                # the way the game draws them — same mechanism as `@angle` tokens.
                if rec is not None and abs(rot) > 0.5:
                    rec = self._rotate_record(rec, -rot)
                if rec is not None and flip:
                    rec = self._mirror_record(rec)
                # prefab local-position offset (world units == cells): the game
                # draws this sprite shifted from the cell origin. Carry it so the
                # editor blit matches (screen y is down, so negate oy). Only trust
                # SMALL offsets as genuine within-cell shifts — a large value is
                # usually an effect/sub-part child the DFS hit, not the tile's
                # art, and applying it would fling the sprite across the level.
                if rec is not None and (abs(ox) > 0.05 or abs(oy) > 0.05) \
                        and abs(ox) <= OFFSET_LIMIT and abs(oy) <= OFFSET_LIMIT:
                    rec = {**rec, "ox": ox, "oy": -oy}
        except Exception:
            rec = None
        return rec

    # ---- hand-authored overrides -----------------------------------------
    @staticmethod
    def _clean_override(data: dict | None) -> dict:
        """Normalize a raw override payload into the stored/applied form: px/py
        (pivot 0..1), ox/oy (within-cell offset, cells) and rot (sprite rotation,
        degrees, 0 dropped) are floats; arrow is either degrees (0=up, 45=up-left,
        90=left … 315=up-right) or the strings 'cw'/'ccw' (a clockwise / counter-
        clockwise spin marker). Blank/None fields are dropped."""
        data = data or {}
        clean: dict = {}
        for k in ("px", "py", "ox", "oy"):
            v = data.get(k)
            if v not in (None, ""):
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
        rv = data.get("rot")
        if rv not in (None, ""):
            try:
                f = float(rv) % 360
                if f:                       # 0/360 == no rotation -> omit
                    clean["rot"] = f
            except (TypeError, ValueError):
                pass
        a = data.get("arrow")
        if a not in (None, ""):
            if a in ("cw", "ccw"):
                clean["arrow"] = a
            else:
                try:
                    clean["arrow"] = float(a)
                except (TypeError, ValueError):
                    pass
        if data.get("flip"):             # baked horizontal mirror
            clean["flip"] = True
        rimg = data.get("rotimg")
        if rimg not in (None, ""):       # baked image rotation (degrees)
            try:
                clean["rotimg"] = float(rimg) % 360
            except (TypeError, ValueError):
                pass
        art = data.get("art")
        if art not in (None, ""):        # draw a DIFFERENT sprite/GameObject's art
            clean["art"] = str(art)
        return clean

    def _apply_override(self, token: str, rec: dict | None) -> dict | None:
        """Merge a per-token override (draw anchor px/py, within-cell offset
        ox/oy, sprite rotation rot, manual direction/spin arrow) onto a resolved
        record. The override wins — it's the editor's escape hatch for sprites the
        resolver can't place or orient automatically. rot/arrow are passed through
        for the editor to render."""
        ov = self._overrides.get(token)
        if not ov or rec is None:
            return rec
        rec = dict(rec)
        # baked orientation (into the image, so palette thumbnails show it too):
        # `flip` = horizontal mirror (left-facing cannons), `rotimg` = rotate the
        # image N° (e.g. an up-firing cannon whose base art points down).
        if ov.get("flip"):
            rec = self._mirror_record(rec)
        if ov.get("rotimg") is not None:
            rec = self._rotate_record(rec, float(ov["rotimg"]))
        for k in ("px", "py", "ox", "oy"):
            if ov.get(k) is not None:
                rec[k] = float(ov[k])
        if ov.get("rot") is not None:
            rec["rot"] = float(ov["rot"])
        if ov.get("arrow") is not None:
            rec["arrow"] = ov["arrow"]
        rec["ov"] = True            # hand-authored: editor uses pivot placement
        return rec

    def set_override(self, token: str, data: dict | None) -> None:
        """Set (or, with an empty/blank data, clear) a token's override and drop
        its cache entry so the next get() reflects it. The caller persists the
        full override dict to disk."""
        clean = self._clean_override(data)
        if clean:
            self._overrides[token] = clean
        else:
            self._overrides.pop(token, None)
        self._cache.pop(token, None)

    def clear_override(self, token: str) -> None:
        self._overrides.pop(token, None)
        self._cache.pop(token, None)

    def list_art_names(self) -> list[str]:
        """All resolvable art source names (Sprites + GameObjects), sorted — for
        the editor's "pick the correct sprite" search when the auto-resolved art
        is wrong. Either kind works as an override `art` value."""
        self._index_go()
        self._index_spr()
        return sorted(set(self._go) | set(self._spr))

    def get(self, token: str) -> dict | None:
        """Return {uri, w, h, px, py, angle, ox?, oy?, arrow?} for a token's
        sprite, or None.

        Tokens may carry a rotation suffix `name@<deg>` (e.g. firebars are
        `trap117@180`); we resolve the base sprite and pass the angle through so
        the editor can draw it rotated about its pivot — exactly as the game
        positions it. w/h are native pixel dims (so multi-cell sprites scale).
        A hand-authored override (sprite_overrides.json) is merged in last."""
        if token in self._cache:
            return self._cache[token]
        rec = None
        # an override may REDIRECT to a different sprite/GameObject ("art") when
        # the resolver picked the wrong one for this token — resolve that instead.
        ov = self._overrides.get(token)
        source = (ov.get("art") if ov else None) or token
        # `properties` can list options as `a#b#c` (the game picks one) — preview
        # the first that has art. A tile may also carry a `@<deg>` rotation.
        for opt in source.split("#"):
            base, angle = opt, 0.0
            if "@" in base:
                b, _, ang = base.rpartition("@")
                try:
                    angle, base = float(ang), b
                except ValueError:
                    pass
            r = self._resolve_base(base)
            if r is not None:
                rec = self._rotate_record(r, angle) if angle else r
                break
        rec = self._apply_override(token, rec)
        self._cache[token] = rec
        return rec

    def get_many(self, tokens) -> dict[str, dict | None]:
        return {t: self.get(t) for t in tokens}
