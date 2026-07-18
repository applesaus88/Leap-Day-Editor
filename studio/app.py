"""
app.py — Leap Day Mod Studio desktop app (pywebview shell).

A single Python process: the window renders ui/index.html, and the JS calls
into the `Api` class below via `window.pywebview.api.*`. The Api wraps the
core engine (bundle/chunkfmt/modbuild) so the whole studio is one
language with direct access to UnityPy. This is a LEVEL EDITOR only — it has
no ability to change game physics or compiled code.

Run:  python3 studio/app.py
"""

from __future__ import annotations

import json
import os
import sys
import threading

import webview

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.bundle import Bundle
from core.chunkfmt import Chunk, Enemy, Conn, Path, Autotile, encode_grid, decode_grid, EMPTY
from core.project import Project
from core.sprites import SpriteResolver
from core import modbuild

CATALOG = os.path.join(ROOT, "tiles", "catalog.json")
SPRITE_OVERRIDES = os.path.join(ROOT, "tiles", "sprite_overrides.json")
BUILD_DIR = os.path.join(ROOT, "build")
JAN_INDEX = os.path.join(ROOT, "tools", "january_chunks", "january_index.json")
THEME_INDEX = os.path.join(ROOT, "tools", "january_chunks", "theme_index.json")


class Api:
    def __init__(self):
        self.project = Project()
        self.project_path: str | None = None
        self.xapk_path: str | None = None
        self.bundle: Bundle | None = None        # read-only source of original chunks
        self.sprites: SpriteResolver | None = None
        self._catalog = json.load(open(CATALOG)) if os.path.exists(CATALOG) else {}
        # hand-authored sprite fixes (draw anchor / direction arrow per token),
        # baked into the editor permanently — applied by the SpriteResolver.
        self._sprite_overrides = (json.load(open(SPRITE_OVERRIDES))
                                  if os.path.exists(SPRITE_OVERRIDES) else {})
        # captured per-date chunk index (tools/capture_january.py) — what chunks
        # each calendar day actually loads, so a day can be edited surgically.
        self._jan = json.load(open(JAN_INDEX)) if os.path.exists(JAN_INDEX) else {}
        # captured per-date background theme (tools/capture_january.py hooks
        # Level.CalculateTheme) — {date: themeIndex}; absent until a capture runs.
        self._themes = json.load(open(THEME_INDEX)) if os.path.exists(THEME_INDEX) else {}
        self._window = None

    # ---- catalog / project state -----------------------------------------
    def get_catalog(self):
        return self._catalog

    def get_state(self):
        return {
            "project_name": self.project.name,
            "project_path": self.project_path,
            "xapk_path": self.xapk_path,
            "bundle_loaded": self.bundle is not None,
            "edited_levels": sorted(self.project.levels.keys()),
            "library": sorted(self.project.library.keys()),
            "force_date": self.project.force_date,
            "force_theme": self.project.force_theme,
            "force_character": self.project.force_character,
            "checkpoint_fruit_cost": self.project.checkpoint_fruit_cost,
            "force_checkpoint_mode": self.project.force_checkpoint_mode,
            "flag_checkpoints": self.project.flag_checkpoints,
            "grapple_skin": self.project.grapple_skin,
            "character_names": __import__("core.sopatch", fromlist=["x"]).CHARACTER_NAMES,
            "vip_unlock": "vip_unlock" in self.project.patches,
            "grappling_hook": "grappling_hook" in self.project.patches,
            "enforce_width": self.project.settings.get("enforce_width", True),
            # global "fast spawn/fire rate" multiplier applied to every enemy's
            # cadence (spawnTimer / Cupid.pauseTime) via the native lib. 1 = off.
            "spawn_mult": self.project.settings.get("spawn_mult", 1),
            # power mode unlocks advanced/risky tools (free placement, longer
            # levels, raw/dev editing). Off by default = the friendly, guarded UI.
            "power_mode": self.project.settings.get("power_mode", False),
            # rename the package so a build installs ALONGSIDE the stock game, and
            # strip its ad/billing/Play components (defaults: off / on-with-clone).
            "clone_package": self.project.settings.get("clone_package", False),
            "strip_store": self.project.settings.get("strip_store", True),
            # 🎵 keep music+background offscreen (wide chunks) — playtest agent.
            "keep_music_bg": self.project.keep_music_bg,
            "bg_mode": self.project.bg_mode,     # "full" scenery everywhere / "bare" sky only
            "smooth_camera": self.project.smooth_camera,   # wide chunks: camera follows the player continuously
            "lock_camera_y": self.project.lock_camera_y,   # lock the camera Y to every chunk
            "lock_y_cap_top": self.project.lock_y_cap_top, # lock-Y: cap the top too (box in) vs bottom-only (see next chunk)
            "brick_dead_sides": self.project.brick_dead_sides,  # build: brick the dead side columns of wide chunks
            "hide_timer": self.project.hide_timer,         # playtest: hide the run timer
            "hide_progress": self.project.hide_progress,   # playtest: hide the progression bar
            # 🚩 respawn-flag mode — cannon→checkpoint agent armed at playtest.
            "respawn_flags": self.project.respawn_flags,
            # per-carrier firebar render config so the canvas can draw the arm
            # (length/direction) + a spin/swing indicator instead of a lone ball.
            "firebars": self._firebar_render_map(),
            "firebar_dot": self._dot_uri(),     # the real firebar ball sprite (tile_fire-2)
            # per-INDIVIDUAL-enemy tuning ("chunk|sx|sy" -> {projectile,health,walk,h}),
            # applied in-process by an embedded libnativemod.so (core/nativemod.py).
            "enemy_tuning": self.project.enemy_tuning,
            # global axe-boomerang tunables {range,speed,spin} (blank = baked default)
            "axe": self.project.axe,
            "projectiles": __import__("core.elements", fromlist=["x"])._ALL_PROJECTILES,
            # dev-mode: global baseline launch speeds per (enemy class, projectile)
            # combo, and the enemy classes whose launch speed the mod can actually
            # set (a placement's shootmult scales these). See core/nativemod.py.
            "shoot_bakes": __import__("core.nativemod", fromlist=["x"]).load_shoot_bakes(),
            "shoot_classes": __import__("core.nativemod", fromlist=["x"]).SHOOT_CLASSES,
            # every enemy/item that shoots or spawns, for the per-enemy dev bake panel
            # ({label, cls}, unique runtime class; pool-fed enemies share ObjectPool
            # so they're collapsed to one). Derived from elements._SHOOTERS.
            "shoot_enemies": self._shoot_enemies(),
        }

    def _shoot_enemies(self):
        sh = __import__("core.elements", fromlist=["x"])._SHOOTERS
        seen, out = set(), []
        for s in sh:
            c = s.get("cls")
            if not c or c in seen:
                continue
            seen.add(c)
            out.append({"label": s.get("label", c), "cls": c})
        return out

    def _dot_uri(self):
        """Data-URI PNG of the firebar ball sprite (tile_fire-2 — a red ring; over
        the dark grid it reads grey-centred, like the game). Cached."""
        if not self.sprites:
            return None
        if not getattr(self, "_dot_cache", None):
            try:
                rec = self.sprites.get("tile_fire-2")
                self._dot_cache = rec.get("uri") if rec else None
            except Exception:
                self._dot_cache = None
        return self._dot_cache

    @staticmethod
    def _fb_render_from_fields(f):
        """Mace fields -> a friendly {length,double,start,clockwise,circular} the
        editor canvas uses to draw the firebar arm + rotation indicator."""
        import math
        from core import firebar
        prog = f.get("progress", 0.0)
        start = min(firebar.START_RAD, key=lambda k: abs(
            ((firebar.START_RAD[k] - prog + math.pi) % (2 * math.pi)) - math.pi))
        return {"length": int(f.get("chainLengthInTiles", 3)),
                "double": bool(f.get("doubleMace")),
                "start": start,
                "clockwise": f.get("angularSpeed", -2) < 0,
                "circular": bool(f.get("circularMovement", 1))}

    def _firebar_render_map(self):
        return {t: self._fb_render_from_fields(f)
                for t, f in self.project.firebars.items()}

    def set_setting(self, key, value):
        """Toggle a studio preference (persisted in the .ldmod settings block)."""
        self.project.settings[key] = value
        return self.get_state()

    def set_axe(self, settings):
        """Global axe-boomerang tunables {range,speed,spin}. Blank/missing keys keep
        the .so's baked default. Applied by libnativemod.so to every thrown axe."""
        clean = {}
        for k in ("range", "speed", "spin", "hang"):
            v = (settings or {}).get(k)
            if v not in (None, ""):
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
        self.project.axe = clean
        return self.get_state()

    def set_force_character(self, idx):
        """Force the player to always play as character `idx` (None/'' = off).
        Applied as a .so patch (CharacterManager.get_CurrentCharacter -> idx)."""
        from core import sopatch
        if idx is None or idx == "":
            self.project.force_character = None
        else:
            i = int(idx)
            if not (0 <= i < len(sopatch.CHARACTER_NAMES)):
                return {"error": f"character id {i} out of range"}
            self.project.force_character = i
        return self.get_state()

    def set_checkpoint_fruit_cost(self, n):
        """Set how many fruits EVERY checkpoint costs to unlock (stock 20).
        None/'' = leave the game default. Applied as a .so patch that changes the
        gate, the deduction, AND the number drawn on the sign, so they agree.
        Global (all checkpoints the same); range 1..4095."""
        from core import sopatch
        if n is None or n == "":
            self.project.checkpoint_fruit_cost = None
            return self.get_state()
        try:
            i = int(n)
        except (TypeError, ValueError):
            return {"error": f"'{n}' is not a number"}
        if not (0 <= i <= sopatch.CHECKPOINT_FRUIT_COST_MAX):
            return {"error": f"fruit cost must be 0..{sopatch.CHECKPOINT_FRUIT_COST_MAX}"}
        self.project.checkpoint_fruit_cost = i
        return self.get_state()

    def set_checkpoint_mode(self, mode):
        """Pin EVERY checkpoint to one mode (None/'' = game default):
        0=FREE, 1=AUTO (VIP free auto-unlock — no fruits/no ad), 2=FRUIT (pay).
        .so patch stubbing both mode getters. Needs an on-device playtest to
        confirm which mode gives the free-unlock you want."""
        from core import sopatch
        if mode is None or mode == "":
            self.project.force_checkpoint_mode = None
            return self.get_state()
        try:
            m = int(mode)
        except (TypeError, ValueError):
            return {"error": f"'{mode}' is not a mode"}
        if m not in sopatch.CHECKPOINT_MODES:
            return {"error": f"mode must be one of {sorted(sopatch.CHECKPOINT_MODES)}"}
        self.project.force_checkpoint_mode = m
        return self.get_state()

    def set_flag_checkpoints(self, on):
        """Reskin checkpoint chests as flags + make them non-blocking (bundle
        typetree edit at build). The checkpoint trigger stays intact; best paired
        with VIP-auto mode. Needs an on-device playtest."""
        self.project.flag_checkpoints = bool(on)
        return self.get_state()

    def set_grapple_skin(self, idx):
        """Make the grappling hook playable as character `idx`: clone its look onto
        Lick's pack and force playing as Lick (so the grapple works). None = off."""
        from core import sopatch
        if idx is None or idx == "":
            self.project.grapple_skin = None
        else:
            i = int(idx)
            if not (0 <= i < len(sopatch.CHARACTER_NAMES)):
                return {"error": f"character id {i} out of range"}
            self.project.grapple_skin = i
        return self.get_state()

    def set_vip_unlock(self, on):
        """Toggle the VIP/characters/no-ads unlock patch in the project."""
        if on and "vip_unlock" not in self.project.patches:
            self.project.patches.append("vip_unlock")
        elif not on:
            self.project.patches = [p for p in self.project.patches if p != "vip_unlock"]
        return self.get_state()

    def set_grappling_hook(self, on):
        """Toggle the permanent-grappling-hook patch (IsGrapplingHookActive->true:
        every character can grapple from spawn, no pickup). Mechanic-correct; the
        hook renders as the bare pink line, not the animated metal-hook sprite."""
        if on and "grappling_hook" not in self.project.patches:
            self.project.patches.append("grappling_hook")
        elif not on:
            self.project.patches = [p for p in self.project.patches if p != "grappling_hook"]
        return self.get_state()

    # ---- by-date authoring (uses the captured chunk index) ----------------
    def get_calendar(self):
        """The days we have a chunk capture for, with edit progress + which one
        the build is currently locked to."""
        days = []
        for date, v in self._jan.items():
            if date.startswith("_"):
                continue
            edited = sum(1 for c in v["editable"] if c in self.project.levels)
            days.append({"date": date, "editable": len(v["editable"]),
                         "exclusive": len(v["exclusive"]), "edited": edited})
        days.sort(key=lambda d: d["date"])
        return {"days": days, "force_date": self.project.force_date}

    def get_day_chunks(self, date):
        """The chunks a given date loads — exclusive (only this day) first —
        each flagged whether it's edited in this mod."""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        excl = set(v["exclusive"])
        chunks = [{"name": c, "exclusive": c in excl,
                   "edited": c in self.project.levels} for c in v["editable"]]
        chunks.sort(key=lambda x: (not x["exclusive"], x["name"]))
        return {"date": date, "force_date": self.project.force_date,
                "chunks": chunks, "shared": self._jan.get("_shared", [])}

    def lock_date(self, date):
        """Lock the build to always load `date` (force_date) and ensure VIP is
        unlocked so popups don't interrupt. Pass '' to clear the lock."""
        self.project.force_date = date or None
        if date and "vip_unlock" not in self.project.patches:
            self.project.patches.append("vip_unlock")
        return self.get_state()

    # ---- theme (background atmosphere) -----------------------------------
    def _theme_native(self, date):
        """The theme the game natively uses for `date` (from a CalculateTheme
        capture), or None if we haven't captured it."""
        v = self._themes.get(date) if date else None
        return v if isinstance(v, int) else (v or {}).get("theme") if v else None

    def get_themes(self):
        """The theme roster + the project's current forced theme. Lets the UI
        show a day's theme and offer to reassign it."""
        from core import sopatch
        return {"names": sopatch.THEME_NAMES,
                "force_theme": self.project.force_theme,
                "captured": bool(self._themes)}

    def set_day_theme(self, date, theme_index):
        """Reassign the background theme for the day being built. Theme only
        changes the atmosphere (gameplay tiles render in every theme); it's
        applied to the locked day, so this also locks `date`. Pass '' to clear."""
        if theme_index in ("", None):
            self.project.force_theme = None
        else:
            self.project.force_theme = int(theme_index)
            if date:
                self.lock_date(date)
        return self.get_state()

    # ---- ordered level shaping (reorder / add / delete chunks) ------------
    @staticmethod
    def _struct_role(g):
        if g.get("is_checkpoint"):
            return "checkpoint"
        if g.get("is_end"):
            return "end"
        if g.get("is_endzone"):
            return "endzone"
        return "special"

    def _sequence_rows(self, v, gen, desired, anchors, base, removed=None, custom=None):
        """Build the reorder-editor rows by interleaving structural items at
        their anchored heights (so checkpoints ride up with inserts) — the same
        layout the build compiles, so the preview matches the level.

        Structural rows carry `struct_ord` (their ordinal, matching the build's
        drop index) and a `removed` flag; gameplay rows carry `checkpoint` when
        the author flagged that slot as a custom checkpoint."""
        from core import override
        removed = set(removed or [])
        custom = set(custom or [])
        seq = override.interleave_sequence(gen, desired, anchors)
        rows = []
        start = next((s for s in v["sequence"] if s["role"] == "start"), None)
        if start:
            rows.append({"role": "start", "name": start["name"],
                         "edited": start["name"] in self.project.levels})
        struct_ord = 0
        for e in seq:
            if e["role"] == "struct":
                so = struct_ord
                struct_ord += 1
                nm = e["gen"]["name"].split("/")[-1]
                rows.append({"role": self._struct_role(e["gen"]), "name": nm,
                             "edited": nm in self.project.levels,
                             "struct_ord": so, "removed": so in removed})
            else:
                gi = e["gp"]
                name = desired[gi] if gi < len(desired) else ""
                orig = e["gen"]["name"].split("/")[-1] if e["gen"] else ""
                rows.append({"role": "gameplay", "name": name, "orig": orig,
                             "gp": gi, "edited": bool(name) and name in self.project.levels,
                             "deleted": not name, "extra": gi >= base,
                             "checkpoint": gi in custom})
        return rows

    def get_day_sequence(self, date):
        """The day's full ordered level (start, gameplay, checkpoints, end)
        reflecting the current desired order, for the reorder editor."""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        slots = v["slots"]
        base = len(slots)
        stored = list(self.project.day_orders.get(date) or slots)
        # EXPERIMENTAL longer levels: keep the captured gameplay count, but allow
        # the authored list to grow past it (extra chunks are appended past the
        # level's native length — may or may not render; see compile_day_build).
        ln = max(base, len(stored))
        desired = (stored + [""] * ln)[:ln]
        gen = v.get("genlist")
        anchors = self._struct_anchors(date)
        removed_cp = self.project.day_removed_structs.get(date)
        custom_cp = self.project.day_custom_checkpoints.get(date)
        # Use the riding layout (checkpoints shift up with inserts) once a chunk
        # has been inserted OR a checkpoint added/removed; otherwise keep the
        # original fixed-position walk so untouched days render exactly as captured.
        if gen and anchors is not None and (date in self.project.day_structs
                                            or removed_cp or custom_cp):
            rows = self._sequence_rows(v, gen, desired, anchors, base,
                                       removed=removed_cp, custom=custom_cp)
        else:                                       # no insert / no capture -> fixed
            rows, gi, struct_ord = [], 0, 0
            for s in v["sequence"]:
                if s["role"] == "gameplay":
                    name = desired[gi] if gi < len(desired) else ""
                    rows.append({"role": "gameplay", "name": name, "orig": s["name"],
                                 "gp": gi, "edited": bool(name) and name in self.project.levels,
                                 "deleted": not name, "checkpoint": False})
                    gi += 1
                elif s["role"] == "start":
                    rows.append({"role": "start", "name": s["name"],
                                 "edited": s["name"] in self.project.levels})
                else:                               # checkpoint / end / special
                    rows.append({"role": s["role"], "name": s["name"],
                                 "edited": s["name"] in self.project.levels,
                                 "struct_ord": struct_ord, "removed": False})
                    struct_ord += 1
            for gi in range(base, len(desired)):    # appended EXTRA gameplay slots
                name = desired[gi]
                if not name:
                    continue
                rows.append({"role": "gameplay", "name": name, "orig": "", "gp": gi,
                             "edited": name in self.project.levels, "deleted": False,
                             "extra": True})
        return {"date": date, "rows": rows, "order": desired, "slots": slots,
                "base_slots": base,
                "force_date": self.project.force_date,
                "native_theme": self._theme_native(date),
                "force_theme": self.project.force_theme}

    # ---- checkpoints: remove one, or flag a slot as a custom checkpoint ----
    def remove_day_checkpoint(self, date, struct_ord):
        """Drop the structural item at `struct_ord` (a checkpoint) from this day's
        built level — the override is emitted without it. Reversible."""
        lst = self.project.day_removed_structs.setdefault(date, [])
        if int(struct_ord) not in lst:
            lst.append(int(struct_ord))
        return self.get_day_sequence(date)

    def restore_day_checkpoint(self, date, struct_ord):
        """Undo remove_day_checkpoint for one structural item."""
        lst = self.project.day_removed_structs.get(date)
        if lst and int(struct_ord) in lst:
            lst.remove(int(struct_ord))
            if not lst:
                self.project.day_removed_structs.pop(date, None)
        return self.get_day_sequence(date)

    def toggle_custom_checkpoint(self, date, gp_index, on=True):
        """Flag/unflag a gameplay slot as a CUSTOM checkpoint (emitted with
        isCheckpoint:1). Note: this registers a checkpoint at that position; a
        full checkpoint ROOM needs the checkpoint tiles (Door/TwoChests) authored
        into that chunk — flagging alone marks the respawn point. Needs playtest."""
        gi = int(gp_index)
        lst = self.project.day_custom_checkpoints.setdefault(date, [])
        if on and gi not in lst:
            lst.append(gi)
        elif not on and gi in lst:
            lst.remove(gi)
        if not self.project.day_custom_checkpoints.get(date):
            self.project.day_custom_checkpoints.pop(date, None)
        return self.get_day_sequence(date)

    # how far past the captured gameplay length you may author (EXPERIMENTAL).
    EXTRA_SLOTS = 16

    def set_day_order(self, date, order):
        """Store the desired gameplay order for a date (names; '' = deleted).
        Length may exceed the day's native slot count by up to EXTRA_SLOTS — extra
        chunks are appended past the level's native length (experimental, may not
        render). Trailing empties are trimmed."""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        n = len(v["slots"])
        order = list(order)
        while len(order) > n and not order[-1]:     # trim trailing empties past base
            order.pop()
        cap = n + self.EXTRA_SLOTS
        order = order[:cap]
        self.project.day_orders[date] = order
        if order == list(v["slots"]):               # exact reset to native order ->
            self.project.day_structs.pop(date, None)  # baseline checkpoint anchors
        return self.get_day_sequence(date)

    def restore_day(self, date):
        """Fully restore a day to its ORIGINAL captured state — drop the reorder
        AND remove every chunk edit/override for chunks this day uses. Use this to
        recover a day that crashes or renders wrong: it reverts to the vanilla
        level the game ships (which loads), so you can start that day over.
        Only touches chunks belonging to this day; other days' edits are left."""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        daychunks = {s["name"] for s in v.get("sequence", [])}
        daychunks.update(v.get("slots", []))
        daychunks.update(g["name"].split("/")[-1] for g in v.get("genlist", []))
        daychunks.update(self.project.day_orders.get(date, []))   # chunks placed into this day
        self.project.day_orders.pop(date, None)
        self.project.day_structs.pop(date, None)
        self.project.sequences.pop(date, None)
        removed = [n for n in list(self.project.levels) if n in daychunks]
        for n in removed:
            self.project.levels.pop(n, None)
        out = self.get_day_sequence(date)
        out["removed_edits"] = removed
        return out

    def replace_chunk(self, target, source):
        """Overwrite chunk `target`'s content with chunk `source`'s — used to
        replace structural chunks (start / checkpoint / end / special) in place.
        `source` may be a game chunk, an edited level, or a custom-library chunk.
        Pass source='' to clear the override (restore the original)."""
        if not source:
            self.project.levels.pop(target, None)
        else:
            self.project.levels[target] = self._resolve_xml(source)
        return {"ok": True, "edited_levels": sorted(self.project.levels.keys())}

    def _struct_anchors(self, date):
        """The day's structural-item anchors (one gameplay-count per checkpoint/
        special/finish, in gen order) in CURRENT coordinates. Stored once a chunk
        is inserted; otherwise the captured baseline. Returns None if the day has
        no gen-list capture (falls back to fixed structural positions)."""
        from core import override
        gen = (self._jan.get(date) or {}).get("genlist")
        if not gen:
            return None
        base = override.baseline_struct_anchors(gen)
        stored = self.project.day_structs.get(date)
        if stored is not None and len(stored) == len(base):
            return list(stored)
        return base

    def insert_day_chunk(self, date, gp_index, name):
        """Insert chunk `name` into the day's gameplay order at position
        `gp_index`, pushing later chunks down. The sequence GROWS (the last chunk
        is no longer dropped) — extra chunks past the level's native length are
        appended (experimental, may not render). Every structural item above the
        insertion point (checkpoints, specials, the finish) rides UP with it, so
        the whole level shifts — not just the gameplay chunks. `name` may be a
        game chunk, an edited level, or a custom-library chunk."""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        slots = v["slots"]
        desired = list(self.project.day_orders.get(date) or slots)
        if len(desired) < len(slots):
            desired = (desired + [""] * len(slots))[:len(slots)]
        i = max(0, min(int(gp_index), len(desired)))
        # bump every checkpoint/special anchored ABOVE the insertion point so it
        # rides up with the chunks it sits over (anchors are in current coords).
        anchors = self._struct_anchors(date)
        if anchors is not None:
            self.project.day_structs[date] = [a + 1 if a > i else a
                                              for a in anchors]
        desired.insert(i, name)                       # grows the level (no truncation)
        return self.set_day_order(date, desired)

    def delete_day_chunk(self, date, gp_index):
        """Remove the gameplay chunk at `gp_index` and slide everything ABOVE it
        DOWN one — the inverse of insert. Checkpoints/specials above the removed
        chunk ride down with it. (The day's generator slot count is fixed, so the
        top of the level reverts to its native chunk; like longer levels this is
        experimental — playtest. To leave a climbable GAP instead of removing the
        slot, use ⇄ -> empty.)"""
        v = self._jan.get(date)
        if not v:
            return {"error": f"no capture for {date}"}
        slots = v["slots"]
        desired = list(self.project.day_orders.get(date) or slots)
        if len(desired) < len(slots):
            desired = (desired + [""] * len(slots))[:len(slots)]
        i = int(gp_index)
        if not (0 <= i < len(desired)):
            return {"error": "slot out of range"}
        # drop every checkpoint/special anchored ABOVE the removed chunk by one so
        # it rides down with the chunks it sits over (anchors are in current coords).
        anchors = self._struct_anchors(date)
        if anchors is not None:
            self.project.day_structs[date] = [a - 1 if a > i else a
                                              for a in anchors]
        desired.pop(i)
        return self.set_day_order(date, desired)

    # ---- custom chunk library (from-scratch chunks, not tied to a game name)
    def get_library(self):
        """The user's custom chunks (name + size) for the library panel."""
        out = []
        for nm, xml in sorted(self.project.library.items()):
            try:
                c = Chunk.parse(xml)
                out.append({"name": nm, "w": c.w, "h": c.h})
            except Exception:
                out.append({"name": nm, "w": 0, "h": 0})
        return {"library": out}

    def new_library_chunk(self, name, w=14, h=19):
        """Create a blank custom chunk under `name` and return it for editing."""
        name = (name or "").strip()
        if not name:
            return {"error": "name required"}
        if name in self.project.library:
            return {"error": f"a custom chunk named {name!r} already exists"}
        c = Chunk.empty(int(w), int(h))
        self.project.library[name] = c.to_xml()
        return self._chunk_to_dict(name, c)

    def load_library_chunk(self, name):
        """Open a custom-library chunk on the canvas."""
        xml = self.project.library.get(name)
        if xml is None:
            return {"error": f"no custom chunk {name!r}"}
        return self._chunk_to_dict(name, Chunk.parse(xml))

    def save_library_chunk(self, name, payload, enemy_tuning=None):
        """Save the open canvas back into the custom-chunk library."""
        name = (name or "").strip()
        if not name:
            return {"error": "name required"}
        self.project.library[name] = self._chunk_from_payload(payload).to_xml()
        self._commit_enemy_tuning(name, payload, enemy_tuning)
        return self.get_library()

    def rename_library_chunk(self, old, new):
        new = (new or "").strip()
        if old not in self.project.library:
            return {"error": f"no custom chunk {old!r}"}
        if not new or (new != old and new in self.project.library):
            return {"error": "invalid or duplicate name"}
        self.project.library[new] = self.project.library.pop(old)
        # repoint any day orders that referenced the old name
        for date, order in self.project.day_orders.items():
            self.project.day_orders[date] = [new if n == old else n for n in order]
        return self.get_library()

    def remove_library_chunk(self, name):
        self.project.library.pop(name, None)
        return self.get_library()

    # ---- universal firebar (one parametric Mace instead of 17 presets) ----
    def place_firebar(self, settings):
        """Resolve firebar `settings` (length/double/clockwise/start/circular) to
        a carrier mace TOKEN to paint. Identical configs share a carrier; each new
        distinct config claims a fresh one and records its Mace field override
        (applied to the prefab at build time). Returns the token + a summary."""
        from core import firebar
        kind = (settings or {}).get("kind") or "firebar"
        pool = firebar.POOLS.get(kind, firebar.CARRIERS)
        fb = firebar.from_settings(settings or {})
        fields = fb.fields()
        # reuse a carrier of the SAME kind that already holds this exact config
        for tok, f in self.project.firebars.items():
            if f == fields and firebar.kind_of(tok) == kind:
                return {"token": tok, "summary": fb.summary(), "config": settings,
                        "reused": True, "render": self._fb_render_from_fields(fields)}
        free = [c for c in pool if c not in self.project.firebars]
        if not free:
            return {"error": f"too many distinct {kind}s (max {len(pool)} "
                    "per build) — reuse an existing one or clear some"}
        tok = free[0]
        self.project.firebars[tok] = fields
        return {"token": tok, "summary": fb.summary(), "config": settings,
                "reused": False, "render": self._fb_render_from_fields(fields)}

    def list_firebars(self):
        """Current firebar carriers + their human summary (for a manage panel)."""
        from core import firebar
        import math
        out = []
        for tok, f in self.project.firebars.items():
            # reconstruct a readable summary from the stored Mace fields
            start = min(firebar.START_RAD, key=lambda k: abs(
                ((firebar.START_RAD[k] - f.get("progress", 0) + math.pi) % (2*math.pi)) - math.pi))
            fbo = firebar.Firebar(length=f.get("chainLengthInTiles", 3),
                                  double=bool(f.get("doubleMace")),
                                  clockwise=f.get("angularSpeed", -2) < 0,
                                  start=start, circular=bool(f.get("circularMovement", 1)))
            out.append({"token": tok, "summary": fbo.summary()})
        return {"firebars": out}

    def clear_firebar(self, token):
        self.project.firebars.pop(token, None)
        return self.list_firebars()

    # ---- generalized element panels (traps + enemies) --------------------
    def get_element_panels(self):
        """The configurable-element registry for the UI to render sidebars, plus
        a token->kind map so the editor knows which panel a selected tile opens."""
        from core import elements
        ui = []
        for p in elements.PANELS:
            e = {"kind": p["kind"], "label": p["label"],
                 "mechanism": p["mechanism"]}
            if p["mechanism"] == "variant":
                e["variants"] = p["variants"]
            elif p["mechanism"] == "fields":
                e["fields"] = p["fields"]
                if p.get("enemy"):               # shooting-enemy projectile panels
                    e["enemy"] = True
                    e["carriers"] = p["carriers"]  # the enemy tokens this panel serves
            ui.append(e)
        return {"panels": ui, "token_kind": elements.TOKEN_KIND}

    def _elem_summary(self, panel, fields):
        out = []
        for f in panel.get("fields", []):
            v = fields.get(f["key"])
            if isinstance(v, dict) and "__prefab__" in v:   # projectile/prefab select
                v = v["__prefab__"]
            if f["type"] == "bool":
                out.append(f["label"] if v else "no " + f["label"])
            else:
                out.append(f"{f['label']} {v}")
        return " · ".join(out)

    def place_element(self, kind, settings):
        """Resolve an element panel's `settings` to a token to paint. Variant
        panels just pick a token; 'mace' routes to the firebar machinery; 'fields'
        claims a carrier and records its serialized-field override for the build."""
        from core import elements
        panel = elements.BY_KIND.get(kind)
        if not panel:
            return {"error": f"unknown element {kind!r}"}
        settings = settings or {}
        if panel["mechanism"] == "variant":
            valid = {v["token"] for v in panel["variants"]}
            tok = settings.get("token")
            if tok not in valid:
                tok = panel["variants"][0]["token"]
            # placing a flag or respawn marker arms the checkpoint→respawn system
            if kind in ("respawn_flag", "respawn_point"):
                self.project.respawn_flags = True
            return {"token": tok, "kind": kind, "summary": tok}
        if panel["mechanism"] == "mace":
            return self.place_firebar({**settings, "kind": kind})
        # fields mechanism — one palette entry, customise everything here. The
        # carrier (and thus SPRITE) is chosen from the picked style's slots (or all
        # carriers if the element has no styles); each distinct config claims a
        # free slot, identical configs share one.
        cls = panel["cls"]
        fields = elements.field_values(panel, settings)
        used = set(self.project.element_overrides) | set(self.project.firebars)
        # direction pools (cannon): __aim__ picks a pool of correctly-oriented
        # carriers, so several independent same-direction cannons can coexist;
        # __look__ is a preference (falls back to any free slot in that direction).
        pools = panel.get("pools")
        if pools:
            aim = settings.get("__aim__") or next(iter(pools))
            pool = pools.get(aim) or panel["carriers"]
            for t in pool:                       # identical config -> share a slot
                ov = self.project.element_overrides.get(t)
                if ov and ov.get("cls") == cls and ov.get("fields") == fields:
                    return {"token": t, "kind": kind, "reused": True,
                            "summary": self._elem_summary(panel, fields)}
            look = settings.get("__look__") or "any"
            pref = panel.get("look_carrier", {}).get(f"{aim}/{look}")
            if pref and pref not in used:
                tok = pref
            else:
                free = [c for c in pool if c not in used]
                if not free:
                    return {"error": f"no free {aim} cannon slot (max "
                            f"{len(pool)} per direction) — reuse or clear one"}
                tok = free[0]
            self.project.element_overrides[tok] = {"cls": cls, "fields": fields}
            return {"token": tok, "kind": kind,
                    "summary": self._elem_summary(panel, fields)}
        # an explicit __carrier__ (e.g. an oriented cannon) configures THAT tile
        # directly — its baked direction/muzzle stay correct.
        carrier = str(settings.get("__carrier__") or "")
        if carrier in panel["carriers"]:
            self.project.element_overrides[carrier] = {"cls": cls, "fields": fields}
            return {"token": carrier, "kind": kind,
                    "summary": self._elem_summary(panel, fields)}
        styles = panel.get("styles")
        pool = (styles.get(settings.get("__style__")) if styles else None) \
            or panel["carriers"]
        for t in pool:
            ov = self.project.element_overrides.get(t)
            if ov and ov.get("cls") == cls and ov.get("fields") == fields:
                return {"token": t, "kind": kind, "reused": True,
                        "summary": self._elem_summary(panel, fields)}
        used = set(self.project.element_overrides) | set(self.project.firebars)
        free = [c for c in pool if c not in used]
        if not free:
            return {"error": f"too many distinct {kind} configs (max "
                    f"{len(pool)}) — reuse one or clear some"}
        tok = free[0]
        self.project.element_overrides[tok] = {"cls": cls, "fields": fields}
        return {"token": tok, "kind": kind,
                "summary": self._elem_summary(panel, fields)}

    def clear_element(self, token):
        self.project.element_overrides.pop(token, None)
        return {"ok": True}

    # --- per-individual-enemy tuning (projectile / health / walk speed) -------
    # Unlike element_overrides (per-TOKEN, global to an enemy type), this targets
    # ONE placed enemy by (chunk, sx, sy). It can't be baked into level text, so a
    # build compiles + embeds libnativemod.so (core/nativemod.py) that edits just
    # that instance at runtime. `h` (chunk height) is stored so the native side
    # can convert sy (row-from-top) to the rowFromBottom match coordinate.
    #
    # Tuning is CHUNK DATA: it's committed when the chunk is saved (not per enemy),
    # and reconciled against the chunk's actual enemies — a tuning whose (sx,sy) no
    # longer holds an enemy is dropped. `_commit_enemy_tuning` does that reconcile.
    def _commit_enemy_tuning(self, chunk, payload, incoming):
        """Replace this chunk's enemy tunings with the reconciled `incoming` set:
        keep only tunings whose (sx,sy) still holds an enemy in the saved chunk,
        with a real field set, and refresh the stored chunk height."""
        cells = {(int(round(e["sx"])), int(round(e["sy"])))
                 for e in payload.get("enemies", [])}
        h = len(payload.get("grid") or []) or None
        prefix = f"{chunk}|"
        # clear this chunk's existing tunings, then re-add the valid incoming ones
        for k in [k for k in self.project.enemy_tuning if k.startswith(prefix)]:
            self.project.enemy_tuning.pop(k, None)
        for key, rec in (incoming or {}).items():
            if not str(key).startswith(prefix):
                continue
            try:
                _, sx, sy = key.rsplit("|", 2)
                sx, sy = int(sx), int(sy)
            except ValueError:
                continue
            if (sx, sy) not in cells:            # enemy no longer exists -> drop it
                continue
            clean = {}
            proj = (rec.get("projectile") or "").strip()
            if proj:
                clean["projectile"] = proj
            if rec.get("health") not in (None, ""):
                clean["health"] = int(float(rec["health"]))
            if rec.get("walk") not in (None, ""):
                clean["walk"] = float(rec["walk"])
            sm = rec.get("shootmult")
            if sm not in (None, "") and float(sm) != 1.0:   # 1.0 = no change, don't store
                clean["shootmult"] = float(sm)
            fm = rec.get("firemult")
            if fm not in (None, "") and float(fm) != 1.0:
                clean["firemult"] = float(fm)
            if not clean:                        # nothing actually tuned -> no record
                continue
            clean["h"] = int(h or rec.get("h") or 0)
            if not clean["h"]:
                continue
            self.project.enemy_tuning[key] = clean

    def _chunk_enemy_tuning(self, chunk):
        prefix = f"{chunk}|"
        return {k: v for k, v in self.project.enemy_tuning.items()
                if k.startswith(prefix)}

    def save_shoot_bakes(self, bakes):
        """DEV MODE: persist the global (enemy class, projectile) -> baseline launch
        speed table. Keys are "class|projectile"; blanks are dropped. Returns the
        cleaned table so the UI can resync."""
        nativemod = __import__("core.nativemod", fromlist=["x"])
        clean = {}
        for key, spd in (bakes or {}).items():
            key = str(key)
            if "|" not in key or spd in (None, ""):
                continue
            try:
                clean[key] = float(spd)
            except (TypeError, ValueError):
                continue
        nativemod.save_shoot_bakes(clean)
        return clean

    def _resolve_xml(self, name):
        if not name:
            from core.dayorder import EMPTY_CORRIDOR
            return EMPTY_CORRIDOR
        if name in self.project.levels:
            return self.project.levels[name]
        if name in self.project.library:          # custom from-scratch chunks
            return self.project.library[name]
        if self.bundle and self.bundle.has_text(name):
            return self.bundle.get_text(name)
        from core.dayorder import EMPTY_CORRIDOR
        return EMPTY_CORRIDOR

    def _chunk_path_index(self):
        """Cache of captured chunk paths: (exact {bare_name: full_path},
        list[(bare_name, dir)]). Built once from every gen list / paths block."""
        if getattr(self, "_chunk_paths", None) is not None:
            return self._chunk_paths
        exact, dirs = {}, []
        for v in self._jan.values():
            if not isinstance(v, dict):
                continue
            for g in v.get("genlist", []):
                p = g["name"]
                if "/" in p:
                    nm = p.split("/")[-1]
                    exact.setdefault(nm, p); dirs.append((nm, p.rsplit("/", 1)[0]))
            for p in v.get("paths", []):
                if "/" in p:
                    nm = p.split("/")[-1]
                    exact.setdefault(nm, p); dirs.append((nm, p.rsplit("/", 1)[0]))
        self._chunk_paths = (exact, dirs)
        return self._chunk_paths

    def _infer_dir(self, name):
        """Infer an UNcaptured chunk's directory from captured chunks that share
        the longest name prefix (e.g. s17_aaron_manhole_MED_6 -> Levels/v117 from
        its s17_aaron_manhole_* siblings; s4_..._cannon -> its cannon subfolder).
        Returns the best-voted directory, or None if no strong family match."""
        from collections import Counter
        _, dirs = self._chunk_path_index()
        votes = Counter()
        for nm, d in dirs:
            i = 0
            while i < len(nm) and i < len(name) and nm[i] == name[i]:
                i += 1
            if i >= 8:                       # require a real shared family prefix
                votes[d] += i                # weight by prefix length
        return votes.most_common(1)[0][0] if votes else None

    def _full_path(self, name):
        """Resolve a chunk name to the override's chunkPath. Prefer the captured
        full 'Levels/vNNN/...' path; for an uncaptured chunk, INFER its versioned
        directory from same-family captured chunks (so out-of-the-natural-rotation
        chunks still get a real path the loader resolves, instead of a bare name
        that can fail to load -> blank slot). Falls back to the bare name."""
        if not name:
            return None
        if "/" in name:
            return name
        exact, _ = self._chunk_path_index()
        if name in exact:                            # captured full path
            return exact[name]
        known = (self.bundle and self.bundle.has_text(name)) or \
                name in (self._catalog.get("all_chunk_names") or [])
        if not known:
            return None
        d = self._infer_dir(name)                    # inferred versioned dir
        return f"{d}/{name}" if d else name

    def _resolve_override(self, name):
        """For override compilation, resolve a slot's chunk to either:
          ('chunk', full_path) — a REAL game chunk, placed by its OWN name. If
             it was also edited, its edit lives in project.levels and modbuild
             overwrites that chunk's own TextAsset, so the edit shows wherever
             it's pinned — no need to hijack the slot's natural chunk (doing so
             made an edited chunk collide with the natural name elsewhere ->
             duplicates).
          ('custom', xml) — a from-scratch LIBRARY chunk with no real name; it
             needs the slot's natural chunk as a carrier (handled by caller).
        """
        if not name:
            return None
        if name in self.project.library:           # no real chunk -> needs a carrier
            return ("custom", self.project.library[name])
        p = self._full_path(name)
        if p:                                       # real chunk (edited or not) -> own name
            return ("chunk", p)
        if name in self.project.levels:             # edited but unknown path -> carrier
            return ("custom", self.project.levels[name])
        return None

    def _resolved_project(self):
        """A copy of the project with each authored day compiled for the build.

        Preferred path: the day's captured gen list -> `Level.overrideChunksNew`
        (the generator stamps our exact chunks over the seed pick by position —
        robust, no element-selection swap). Custom content also overwrites the
        slot's natural TextAsset and is force-pinned by the override. Days with
        NO gen list capture fall back to the legacy TextAsset-overwrite reshaping.
        """
        import copy
        from core import dayorder, override
        proj = copy.deepcopy(self.project)
        self._sanitize_levels(proj)          # never feed custom-named levels to modbuild
        # Build any date that was reordered, plus the locked day if any of its
        # slots were edited (a pure edit doesn't set a day_order, but we still
        # want it pinned so the element-selection can't drop it).
        dates = set(self.project.day_orders)
        dates |= set(self.project.day_removed_structs)   # removed checkpoints
        dates |= set(self.project.day_custom_checkpoints)  # added checkpoints
        fd = self.project.force_date
        if fd:
            v = self._jan.get(fd) or {}
            if any(s in self.project.levels for s in v.get("slots", [])):
                dates.add(fd)
        if not dates:
            return proj
        ov_entries, legacy = [], {}
        for date in dates:
            gen = (self._jan.get(date) or {}).get("genlist")
            desired = self.project.day_orders.get(date)
            removed_cp = self.project.day_removed_structs.get(date)
            custom_cp = self.project.day_custom_checkpoints.get(date)
            if gen:
                if desired is None:                 # edited but not reordered ->
                    desired = [g["name"].split("/")[-1]  # pin the natural sequence
                               for g in override.gameplay_slots(gen)]
                base = len(override.gameplay_slots(gen))
                # insert (grew) OR delete (shifted checkpoints, tracked in
                # day_structs) OR checkpoint add/remove -> restamp the WHOLE level.
                if (len(desired) > base or date in self.project.day_structs
                        or removed_cp or custom_cp):
                    anchors = self._struct_anchors(date)
                    entry, levels = override.compile_day_build_full(
                        date, gen, desired, anchors, self._resolve_override,
                        removed_structs=removed_cp, custom_checkpoints=custom_cp)
                else:                               # pure reorder/edit -> light path
                    entry, levels = override.compile_day_build(
                        date, gen, desired, self._resolve_override)
                proj.levels.update(levels)
                if entry:
                    ov_entries.append(entry)
            elif desired is not None:
                legacy[date] = desired
        if ov_entries:
            proj.overrides = dict(proj.overrides)
            proj.overrides["overrideChunksNew"] = ov_entries
        if legacy:
            tmp = copy.copy(self.project)
            tmp.day_orders = legacy
            proj.levels.update(
                dayorder.compile_day_orders(tmp, self._jan, self._resolve_xml))
        self._normalize_widths(proj)
        return proj

    def _normalize_widths(self, proj):
        """Snap every chunk the build uses to a legal width (14/28/42). The game
        grid is 14 wide; a 15-wide chunk (222 stock chunks + some edits) doesn't
        render in a standard slot. Crops/pads to the nearest legal width. Covers
        edited chunks and any off-grid game chunk placed via an override. Gated by
        the project's `enforce_width` setting (default on)."""
        if not proj.settings.get("enforce_width", True):
            return proj
        from core.chunkfmt import Chunk, snap_width
        def fix(content):
            try:
                ch = Chunk.parse(content)
                tgt = snap_width(ch.w)
                return content if ch.w == tgt else ch.set_width(tgt).to_xml()
            except Exception:
                return content
        for name in list(proj.levels):                  # edited chunks
            proj.levels[name] = fix(proj.levels[name])
        for e in proj.overrides.get("overrideChunksNew", []):   # placed game chunks
            for c in e["chunks"]:
                nm = c["chunkPath"].split("/")[-1]
                if nm in proj.levels or not (self.bundle and self.bundle.has_text(nm)):
                    continue
                orig = self.bundle.get_text(nm)
                fixed = fix(orig)
                if fixed != orig:                       # only overwrite if we changed it
                    proj.levels[nm] = fixed
        return proj

    def new_project(self, name):
        # default to the full premium experience (VIP, all characters, no ads) —
        # it's what you want while building/playtesting; remove via the project if not.
        self.project = Project(name=name or "Untitled Leap Day Mod",
                               patches=["vip_unlock"])
        self.project_path = None
        return self.get_state()

    def set_project_name(self, name):
        """Persist the name typed in the toolbar field into the project so it's
        saved with the .ldmod and shown again when the mod is re-opened."""
        self.project.name = (name or "").strip() or "Untitled Leap Day Mod"
        return {"ok": True, "project_name": self.project.name}

    def save_project(self):
        path = self.project_path or self._save_dialog("mymod.ldmod")
        if not path:
            return {"error": "cancelled"}
        if not path.endswith(".ldmod"):
            path += ".ldmod"
        self.project.save(path)
        self.project_path = path
        return {"saved": path}

    def load_project(self):
        sel = self._open_dialog(("Leap Day mod (*.ldmod)", "*.ldmod"))
        if not sel:
            return {"error": "cancelled"}
        self.project = Project.load(sel[0])
        self.project_path = sel[0]
        # Older mods never persisted the name field (default in the file). Show
        # the file's own name so the toolbar reflects which mod is open.
        if not self.project.name or self.project.name == "Untitled Leap Day Mod":
            base = os.path.splitext(os.path.basename(sel[0]))[0]
            if base:
                self.project.name = base
        return self.get_state()

    # ---- game source (xapk) ----------------------------------------------
    def pick_xapk(self):
        sel = self._open_dialog(("Leap Day package (*.xapk;*.apk)", "*.xapk;*.apk"))
        if not sel:
            return {"error": "cancelled"}
        return self.load_xapk(sel[0])

    def load_xapk(self, path):
        """Extract data.unity3d (+ the .so and metadata, read-only, so the
        sprite resolver can find art held in custom scripts) and index chunks."""
        import zipfile, tempfile, io
        tmp = os.path.join(tempfile.gettempdir(), "leapday_data.unity3d")
        so_bytes = meta_bytes = None
        with zipfile.ZipFile(path) as z:
            base = next(n for n in z.namelist() if n.endswith("com.nitrome.leapday.apk"))
            bdata = z.read(base)
            arm = next((n for n in z.namelist() if n.endswith("config.arm64_v8a.apk")), None)
            if arm:
                with zipfile.ZipFile(io.BytesIO(z.read(arm))) as az:
                    try:
                        so_bytes = az.read("lib/arm64-v8a/libil2cpp.so")
                    except KeyError:
                        pass
        with zipfile.ZipFile(io.BytesIO(bdata)) as bz:
            with bz.open(modbuild.DATA_ENTRY) as dsrc, open(tmp, "wb") as out:
                out.write(dsrc.read())
            try:
                meta_bytes = bz.read("assets/bin/Data/Managed/Metadata/global-metadata.dat")
            except KeyError:
                pass
        self.bundle = Bundle(tmp)
        self.sprites = SpriteResolver(self.bundle.env, so_bytes, meta_bytes,
                                      overrides=self._sprite_overrides)
        self.xapk_path = path
        return self.get_state()

    def get_sprites(self, tokens):
        """token -> base64 PNG data URI (or null). Used by the editor to paint
        with the real game art instead of colored placeholders."""
        if not self.sprites:
            return {}
        return self.sprites.get_many(tokens)

    # ---- dev-mode sprite fixes (draw anchor / direction arrow) ------------
    def get_sprite_overrides(self):
        """All hand-authored per-token sprite fixes (kept in
        tiles/sprite_overrides.json), for the dev panel to show."""
        return self._sprite_overrides

    def list_art_names(self):
        """All resolvable art source names so the dev panel can pick the correct
        art when auto-resolution is wrong. Includes (a) every named Sprite +
        GameObject in the bundle, AND (b) every catalog token (tiles + enemy
        `properties`). Tokens matter because an enemy's visible art is usually a
        child sprite with an obscure name (or assembled from parts) — picking the
        TOKEN resolves it through the full prefab/composite pipeline, so the art
        you actually see in-game is selectable even when its raw Sprite isn't."""
        names = set(self.sprites.list_art_names()) if self.sprites else set()
        for t in self._catalog.get("tiles", []):
            if t.get("name"):
                names.add(t["name"])
        for e in self._catalog.get("enemies", []):
            if e.get("properties"):
                names.add(e["properties"])
        return sorted(names)

    def preview_sprite_override(self, token, data):
        """Resolve `token` AS IF `data` were its override, WITHOUT persisting —
        lets the dev panel live-preview a different art source (or any field)
        before Save. Returns the freshly-resolved record."""
        if not self.sprites:
            return {"rec": None}
        saved = self.sprites._overrides.get(token)
        data = dict(data or {})
        # keep the block's baked orientation (rotimg/flip) while previewing an
        # anchor/offset tweak — the anchor editor doesn't send those fields.
        if saved:
            for k in ("rotimg", "flip"):
                if k not in data and saved.get(k) is not None:
                    data[k] = saved[k]
        self.sprites.set_override(token, data)        # also drops the cache entry
        rec = self.sprites.get(token)
        if saved is not None:                          # restore prior state
            self.sprites._overrides[token] = saved
        else:
            self.sprites._overrides.pop(token, None)
        self.sprites._cache.pop(token, None)
        return {"rec": rec}

    def _save_sprite_overrides(self):
        os.makedirs(os.path.dirname(SPRITE_OVERRIDES), exist_ok=True)
        with open(SPRITE_OVERRIDES, "w") as f:
            json.dump(self._sprite_overrides, f, indent=2, sort_keys=True)

    def set_sprite_override(self, token, data):
        """Bake a draw-anchor / rotation / arrow fix for `token` into the editor:
        persist it to disk and apply it live. `data` keys (all optional): px, py
        (pivot fraction 0..1), ox, oy (within-cell offset in cells), rot (sprite
        rotation in degrees), arrow (degrees 0=up/45=up-left/…/315=up-right, or
        'cw'/'ccw' for a spin marker; '' clears it), art (a different sprite/
        GameObject name to draw instead of the token's own). An empty `data`
        clears the whole override. Returns the freshly-resolved record."""
        clean = SpriteResolver._clean_override(data)
        # a directional block's baked orientation (rotimg/flip) is not sent by the
        # anchor/offset editor — carry it over so fixing a cannon's placement
        # doesn't wipe its rotation. (An empty `data` still clears everything.)
        if clean:
            prev = self._sprite_overrides.get(token) or {}
            for k in ("rotimg", "flip"):
                if k not in clean and prev.get(k) is not None:
                    clean[k] = prev[k]
            self._sprite_overrides[token] = clean
        else:
            self._sprite_overrides.pop(token, None)
        self._save_sprite_overrides()
        rec = None
        if self.sprites:
            self.sprites.set_override(token, clean)
            rec = self.sprites.get(token)
        return {"ok": True, "token": token, "rec": rec,
                "overrides": self._sprite_overrides}

    def clear_sprite_override(self, token):
        """Drop `token`'s override (restore the resolver's automatic placement)
        and return its freshly-resolved record."""
        self._sprite_overrides.pop(token, None)
        self._save_sprite_overrides()
        rec = None
        if self.sprites:
            self.sprites.clear_override(token)
            rec = self.sprites.get(token)
        return {"ok": True, "token": token, "rec": rec,
                "overrides": self._sprite_overrides}

    def list_chunks(self, active_only=True):
        if active_only:
            names = self._catalog.get("active_season_chunks", [])
        else:
            # every actual <level> chunk across all themes/seasons (catalog's
            # all_chunk_names is pre-filtered to level chunks, so no junk assets)
            names = self._catalog.get("all_chunk_names", [])
        if self.bundle:
            names = [n for n in names if self.bundle.has_text(n)]
        return names

    # ---- level editing ----------------------------------------------------
    def load_chunk(self, name):
        """Return a chunk as an editor-friendly grid + enemies + meta.
        Prefers an already-edited version from the project."""
        xml = self.project.levels.get(name)
        if xml is None and self.bundle and self.bundle.has_text(name):
            xml = self.bundle.get_text(name)
        if xml is None:
            return {"error": f"chunk {name} unavailable (load your .xapk first)"}
        c = Chunk.parse(xml)
        if self.project.settings.get("enforce_width", True):   # edit at a legal width (14/28/42)
            from core.chunkfmt import snap_width
            tgt = snap_width(c.w)
            if c.w != tgt:
                c.set_width(tgt)
        return self._chunk_to_dict(name, c)

    def blank_chunk(self, name, w=14, h=19):
        return self._chunk_to_dict(name, Chunk.empty(w, h))

    @staticmethod
    def _chunk_from_payload(payload) -> Chunk:
        return Chunk(
            w=len(payload["grid"][0]),
            h=len(payload["grid"]),
            active=payload["grid"],
            difficulty=payload.get("difficulty", 1),
            bg_color=payload.get("bg_color", 0),
            bg=payload.get("bg"),
            fg=payload.get("fg"),
            grid2=payload.get("grid2"),
            enemies=[Enemy(e["sx"], e["sy"], e["properties"]) for e in payload.get("enemies", [])],
            conns=[Conn(c2["sx"], c2["sy"], c2["mx"], c2["my"]) for c2 in payload.get("conns", [])],
            paths=[Path(p["x"], p["y"], [list(pt) for pt in p["pts"]]) for p in payload.get("paths", [])],
            autotiles=[Autotile(a["x"], a["y"], a["v"]) for a in payload.get("autotiles", [])],
            extra_blocks=payload.get("extra_blocks", []),
        )

    def _is_real_chunk(self, name):
        """True if `name` is an EXISTING game chunk (one the build can overwrite)."""
        return bool((self.bundle and self.bundle.has_text(name))
                    or name in (self._catalog.get("all_chunk_names") or []))

    def _sanitize_levels(self, proj):
        """Move any custom-named levels (NOT real game chunks) out of proj.levels
        into proj.library — the build can only OVERWRITE existing chunks, so a
        brand-new name (e.g. 'picture') would crash modbuild. In the library it's
        usable by INSERTING it into a day (it rides an existing slot). Returns the
        moved names so the caller can tell the user."""
        if not self.bundle:
            return []
        moved = []
        for name in list(proj.levels):
            if not self._is_real_chunk(name):
                proj.library.setdefault(name, proj.levels.pop(name))
                moved.append(name)
        return moved

    def save_level(self, name, payload, enemy_tuning=None):
        """payload: {grid, enemies, conns, paths, autotiles, difficulty, bg_color, bg, fg, extra_blocks}
        enemy_tuning: {"chunk|sx|sy": {projectile,health,walk,h}} — the per-enemy
        tunings for THIS chunk, committed + reconciled against its enemies here.

        If `name` is a REAL game chunk, overwrite it (project.levels). If it's a
        brand-new name, the game can't add a chunk file by that name — so route it
        to the custom-chunk LIBRARY instead, where it's used by INSERTING it into a
        day (it then rides an existing slot as its carrier)."""
        xml = self._chunk_from_payload(payload).to_xml()
        # per-enemy tuning travels with the chunk: commit + prune orphans on save
        self._commit_enemy_tuning(name, payload, enemy_tuning)
        et = self._chunk_enemy_tuning(name)
        if self.bundle and not self._is_real_chunk(name):
            self.project.library[name] = xml
            return {"saved": name, "to_library": True, "enemy_tuning": et,
                    "edited_levels": sorted(self.project.levels.keys())}
        self.project.levels[name] = xml
        return {"saved": name, "to_library": False, "enemy_tuning": et,
                "edited_levels": sorted(self.project.levels.keys())}

    def set_respawn_flags(self, on=True):
        """Toggle the 🚩 respawn-flag agent. When on, Playtest launches the game
        under the cannon→checkpoint Frida agent so HomingMissileCannon placements
        (the 🚩 Respawn Flag element) act as touch-to-save checkpoints. Placing a
        flag turns this on automatically; turn it off to playtest without it."""
        self.project.respawn_flags = bool(on)
        return {"ok": True, "respawn_flags": self.project.respawn_flags}

    def set_keep_music_bg(self, on=True):
        """Toggle the offscreen music+background keep-alive. Applied at playtest
        via a Frida agent (tools/horizontal/musicbg.js): music/SFX keep playing
        un-muffled and the background is handled when the player walks off the
        main-path column (wide 28/42 chunks are 2–3 screens wide)."""
        self.project.keep_music_bg = bool(on)
        return {"ok": True, "keep_music_bg": self.project.keep_music_bg}

    def set_bg_mode(self, mode="full"):
        """Background style for the keep-alive agent: 'full' clones the theme
        scenery onto every screen (animated); 'bare' strips the scenery on all
        screens, leaving just the moon/stars sky."""
        self.project.bg_mode = "bare" if str(mode) == "bare" else "full"
        return {"ok": True, "bg_mode": self.project.bg_mode}

    def set_smooth_camera(self, on=True):
        """Toggle the smooth camera. Applied at playtest by the same Frida agent
        (tools/horizontal/musicbg.js): on chunks wider than one screen (>14
        tiles) the camera tracks the player continuously instead of snapping
        between the fixed screen columns."""
        self.project.smooth_camera = bool(on)
        return {"ok": True, "smooth_camera": self.project.smooth_camera}

    def set_lock_camera_y(self, on=True):
        """Toggle the per-chunk camera Y lock. Applied at playtest by the same
        Frida agent (tools/horizontal/musicbg.js): the camera is framed on the
        current chunk's Y band on EVERY chunk (any width, any column) so it never
        scrolls into a vertical neighbour. Short chunks bottom-anchor; taller
        chunks follow the player, contained within the chunk."""
        self.project.lock_camera_y = bool(on)
        return {"ok": True, "lock_camera_y": self.project.lock_camera_y}

    def set_lock_y_cap_top(self, on=True):
        """Modifier for the per-chunk camera Y lock: whether to cap the TOP too.
        On (default) → the view is boxed inside the chunk. Off → cap only the
        bottom (no dead space below) and leave the top open, so you can see the
        chunk you're about to jump up into."""
        self.project.lock_y_cap_top = bool(on)
        return {"ok": True, "lock_y_cap_top": self.project.lock_y_cap_top}

    def set_brick_dead_sides(self, on=True):
        """Toggle bricking the dead SIDE columns of wide (>14) custom chunks at
        BUILD time (not playtest). Fills only columns with no gameplay on a wide
        chunk's edges with brick (generic_06), so its side screens read as wall
        instead of empty background; the play area is left untouched. Baked into
        the built game — reversible by toggling off and rebuilding."""
        self.project.brick_dead_sides = bool(on)
        return {"ok": True, "brick_dead_sides": self.project.brick_dead_sides}

    def set_hide_timer(self, on=True):
        """Hide the run timer at playtest (via the Frida agent)."""
        self.project.hide_timer = bool(on)
        return {"ok": True, "hide_timer": self.project.hide_timer}

    def set_hide_progress(self, on=True):
        """Hide the progression bar at playtest (via the Frida agent)."""
        self.project.hide_progress = bool(on)
        return {"ok": True, "hide_progress": self.project.hide_progress}

    def _wants_playtest_agent(self):
        """True if any playtest-agent feature is on (music/bg keep-alive, smooth
        camera, lock camera Y, hide timer, hide progress) — all ride the same
        musicbg agent."""
        p = self.project
        return bool(p.keep_music_bg or p.smooth_camera or p.lock_camera_y
                    or p.hide_timer or p.hide_progress)

    def _musicbg_agent_path(self):
        agent = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "tools", "horizontal", "musicbg.js")
        if not os.path.exists(agent):
            raise FileNotFoundError("tools/horizontal/musicbg.js missing — compile "
                                    "it (cd tools/horizontal && npx frida-compile "
                                    "musicbg.ts -o musicbg.js)")
        return agent

    def _musicbg_src(self):
        """musicbg.js with the feature flags injected. It's a frida-compile 📦
        bundle (byte-length manifest), so every swap must be length-preserving:
        each token is replaced with its value padded to the token's own length,
        and the agent trims it back. Flags:
          @@BG_MODE@@   (11) -> "full"/"bare"  background style
          @@KEEPMBG@@   (11) -> "on"/"off"     music+bg+timer keep-alive
          @@SMOOTHCAM@@ (13) -> "on"/"off"     smooth camera on wide chunks
          @@LOCKYCAM@@  (12) -> "on"/"off"     lock camera Y to every chunk
          @@CAPTOP@@    (10) -> "on"/"off"     lock-Y: cap the top too (box in)
          @@HIDETIMER@@ (13) -> "on"/"off"     hide the run timer
          @@HIDEPROG@@  (12) -> "on"/"off"     hide the progression bar"""
        src = open(self._musicbg_agent_path()).read()

        def _fill(token, value):
            return (value + " " * len(token))[:len(token)]

        mode = getattr(self.project, "bg_mode", None) or "full"
        keep = "on" if getattr(self.project, "keep_music_bg", False) else "off"
        smooth = "on" if getattr(self.project, "smooth_camera", False) else "off"
        lock_y = "on" if getattr(self.project, "lock_camera_y", False) else "off"
        cap_top = "on" if getattr(self.project, "lock_y_cap_top", True) else "off"
        hide_t = "on" if getattr(self.project, "hide_timer", False) else "off"
        hide_p = "on" if getattr(self.project, "hide_progress", False) else "off"
        src = src.replace("@@BG_MODE@@", _fill("@@BG_MODE@@", mode))
        src = src.replace("@@KEEPMBG@@", _fill("@@KEEPMBG@@", keep))
        src = src.replace("@@SMOOTHCAM@@", _fill("@@SMOOTHCAM@@", smooth))
        src = src.replace("@@LOCKYCAM@@", _fill("@@LOCKYCAM@@", lock_y))
        src = src.replace("@@CAPTOP@@", _fill("@@CAPTOP@@", cap_top))
        src = src.replace("@@HIDETIMER@@", _fill("@@HIDETIMER@@", hide_t))
        src = src.replace("@@HIDEPROG@@", _fill("@@HIDEPROG@@", hide_p))
        return src

    def _launch_musicbg(self, log):
        """Spawn the installed game under Frida with the offscreen music+background
        keep-alive agent. Keeps the session on self so the script isn't GC'd."""
        from core import apkbuild
        device = self._frida_device(log)
        pid = device.spawn([apkbuild.PKG])
        session = device.attach(pid)
        script = session.create_script(self._musicbg_src())
        script.on("message", lambda m, d: log(f"[musicbg] {m.get('payload', m)}"))
        script.load()
        device.resume(pid)
        self._mb_session, self._mb_script = session, script  # keep alive
        log(f"[musicbg] agent attached (bg={getattr(self.project, 'bg_mode', 'full')})")

    def _frida_device(self, log):
        """Push+start frida-server (reusing tools/horizontal/frida-server) and
        return a reachable USB device, or raise. Shared by the agent launchers."""
        import frida, subprocess, time
        from core import apkbuild
        fs = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "tools", "horizontal", "frida-server")
        if not os.path.exists(fs):
            raise FileNotFoundError("tools/horizontal/frida-server missing")
        subprocess.run([apkbuild.ADB, "root"], capture_output=True); time.sleep(1)
        subprocess.run([apkbuild.ADB, "push", fs, "/data/local/tmp/frida-server"],
                       capture_output=True)
        subprocess.run([apkbuild.ADB, "shell", "chmod 755 /data/local/tmp/frida-server"],
                       capture_output=True)
        subprocess.run([apkbuild.ADB, "shell", "pkill -f frida-server"],
                       capture_output=True)
        subprocess.Popen([apkbuild.ADB, "shell", "/data/local/tmp/frida-server", "-D"])
        last = ""
        for _ in range(20):
            time.sleep(0.5)
            alive = subprocess.run([apkbuild.ADB, "shell", "pidof frida-server"],
                                   capture_output=True, text=True).stdout.strip()
            if not alive:
                last = "frida-server not running on device"; continue
            try:
                return frida.get_usb_device(timeout=1)
            except Exception as e:
                last = f"frida device not ready ({e})"
        raise RuntimeError(f"frida-server unreachable — {last}. Is the emulator up?")

    def _launch_cannon_checkpoint(self, log):
        """Spawn the installed game under Frida with the cannon→checkpoint agent
        (tools/cannon_checkpoint/agent.js): every HomingMissileCannon placement (the
        🚩 Respawn Flag element) becomes a touch-to-save respawn flag — open→flap
        animation, chunk-relative respawn, no missile. If the music+background
        keep-alive is also on its agent is co-loaded in the same session."""
        from core import apkbuild
        root = os.path.dirname(os.path.dirname(__file__))
        agent = os.path.join(root, "tools", "cannon_checkpoint", "agent.js")
        if not os.path.exists(agent):
            raise FileNotFoundError("tools/cannon_checkpoint/agent.js missing — "
                                    "compile agent.ts first (tools/cannon_checkpoint/run.sh)")
        device = self._frida_device(log)
        pid = device.spawn([apkbuild.PKG])
        session = device.attach(pid)
        script = session.create_script(open(agent).read())
        script.on("message", lambda m, d: log(f"[flags] {m.get('payload', m)}"))
        script.load()
        # co-load the music+background / smooth-camera agent if either is on.
        if self._wants_playtest_agent():
            mscript = session.create_script(self._musicbg_src())
            mscript.on("message", lambda m, d: log(f"[musicbg] {m.get('payload', m)}"))
            mscript.load()
            self._mb_session, self._mb_script = session, mscript
            log(f"[musicbg] agent co-attached (bg={getattr(self.project, 'bg_mode', 'full')})")
        device.resume(pid)
        self._cp_session, self._cp_script = session, script  # keep alive
        log("[flags] respawn-flag agent attached")

    def playtest(self, payload=None):
        """Build the CURRENT mod (your edited chunks + day-order reshaping +
        date lock) into a throwaway APK, install it, and launch the game so you
        land on your locked day's level. If a chunk is open it's saved first.

        This installs the real, surgical mod (overwrites only the chunks your
        locked day uses) — not the old flood — so what you see is what ships."""
        if not self.xapk_path:
            return {"error": "load your Leap Day .xapk first"}
        from core import apkbuild
        logs: list[str] = []
        if payload:                                  # save the open chunk into the mod
            nm = payload["name"]; xml = self._chunk_from_payload(payload).to_xml()
            target = (self.project.library if (self.bundle and not self._is_real_chunk(nm))
                      else self.project.levels)
            target[nm] = xml
        if "vip_unlock" not in self.project.patches:  # always playtest with the unlocks on
            self.project.patches.append("vip_unlock")
        moved = self._sanitize_levels(self.project)  # custom-named levels -> library
        if moved:
            logs.append("[note] custom chunk(s) " + ", ".join(moved) + " moved to "
                        "Custom chunks — Insert them into a day to place them.")
        proj = self._resolved_project()
        if proj.is_empty():
            return {"error": "nothing to playtest — pick a date, edit/reorder its "
                             "chunks (or lock a date) first"}
        if not apkbuild.ensure_device(log=logs.append):
            return {"error": "no emulator and couldn't auto-start one. Open Android "
                             "Studio's Device Manager and start an emulator, then retry.",
                    "log": logs}
        try:
            out = os.path.join(BUILD_DIR, "playtest")
            summary = modbuild.build(self.xapk_path, proj, out, install=False, log=logs.append)
            to_install = [os.path.join(summary["signed_dir"], s) for s in summary["signed"]
                          if "armeabi" not in s]                 # base + arm64
            logs.append("[playtest] installing…")
            apkbuild.install(to_install, keep_data=True)
            hz_warn = None
            if self.project.respawn_flags:
                # the checkpoint agent also co-loads the music+bg agent if on.
                logs.append("[playtest] respawn flags — launching via cannon→checkpoint agent…")
                try:
                    self._launch_cannon_checkpoint(logs.append)
                except Exception as e:
                    hz_warn = (f"Respawn-flag mode did NOT apply: {e}. Launched normally "
                               f"instead. Fix: compile tools/cannon_checkpoint/agent.js, "
                               f"RESTART the studio, ensure the emulator is up, then Playtest again.")
                    logs.append("[flags] !!! " + hz_warn)
                    apkbuild.launch()
            elif self._wants_playtest_agent():
                feat = " + ".join(
                    (["🎵 keep music+bg offscreen"] if self.project.keep_music_bg else [])
                    + (["🎥 smooth camera"] if self.project.smooth_camera else [])
                    + (["🔒 lock camera Y"] if self.project.lock_camera_y else [])
                    + (["⏱ hide timer"] if self.project.hide_timer else [])
                    + (["📊 hide progress bar"] if self.project.hide_progress else [])).strip()
                logs.append(f"[playtest] {feat} — launching via agent…")
                try:
                    self._launch_musicbg(logs.append)
                except Exception as e:
                    # Do NOT quietly fake it — a normal launch here plays the stock
                    # muffle/cutoff / snapping camera and looks like the setting failed.
                    hz_warn = (f"Playtest agent did NOT apply: {e}. Launched "
                               f"normally instead. Fix: RESTART the studio (new code), make "
                               f"sure the emulator is running, then Playtest again.")
                    logs.append("[musicbg] !!! " + hz_warn)
                    apkbuild.launch()
            else:
                apkbuild.launch()
            tip = f"loaded {proj.force_date}" if proj.force_date else "launched"
            logs.append(f"[playtest] {tip} — your edits are live (tap to play)")
            res = {"ok": True, "log": logs, "force_date": proj.force_date,
                   "levels": summary.get("levels_applied")}
            if hz_warn:
                res["warn"] = hz_warn
            return res
        except Exception as e:
            return {"error": str(e), "log": logs}

    # ---- by-date sequences + demo playtest -------------------------------
    def get_sequences(self):
        """All authored date->[chunk names] sequences + the active demo list."""
        return {"sequences": self.project.sequences, "demo": self.project.demo}

    def set_sequence(self, date, names):
        """Author the ordered chunk list for a date (the level for that day)."""
        names = [n for n in (names or []) if n]
        if names:
            self.project.sequences[str(date)] = names
        else:
            self.project.sequences.pop(str(date), None)
        return self.get_sequences()

    def set_demo(self, names):
        """Set the active demo sequence (what the playtest loop overwrites)."""
        self.project.demo = [n for n in (names or []) if n]
        return self.get_sequences()

    def preview_sequence(self, names, first_at_bottom=True):
        """Stack a sequence into one chunk and return it as an editor grid so the
        UI can show the assembled level before playtesting."""
        from core import playtest
        if not self.bundle:
            return {"error": "load your Leap Day .xapk first"}
        try:
            xml = playtest.resolve_sequence(list(names), self.project,
                                            self.bundle, first_at_bottom=first_at_bottom)
        except Exception as e:
            return {"error": str(e)}
        return self._chunk_to_dict("__sequence__", Chunk.parse(xml))

    def get_day_thumbs(self, date):
        """Compact active-layer grids (+ enemy marker positions) for every chunk
        in a day's sequence, keyed by chunk name — so the day panel can draw a
        small thumbnail per row (a vertical filmstrip of the level)."""
        seq = self.get_day_sequence(date)
        if seq.get("error"):
            return seq
        thumbs = {}
        for r in seq["rows"]:
            nm = r.get("name")
            if not nm or nm in thumbs:
                continue
            xml = (self.project.levels.get(nm)
                   or (self.bundle.get_text(nm) if self.bundle and self.bundle.has_text(nm) else None)
                   or self.project.library.get(nm))
            if xml is None:
                thumbs[nm] = {"w": 0, "h": 0, "grid": [], "enemies": []}
                continue
            try:
                c = Chunk.parse(xml)
                thumbs[nm] = {"w": c.w, "h": c.h, "grid": c.active,
                              "enemies": [{"sx": e.sx, "sy": e.sy} for e in c.enemies]}
            except Exception:
                thumbs[nm] = {"w": 0, "h": 0, "grid": [], "enemies": []}
        return {"thumbs": thumbs}

    def chunk_thumbs(self, names):
        """Active-layer grids (+ enemy positions) for a list of chunk names — for
        the visual chunk PICKER's lazy thumbnails. Resolves edited > bundle >
        library; unknown names are skipped."""
        out = {}
        for nm in (names or []):
            if not nm or nm in out:
                continue
            xml = (self.project.levels.get(nm)
                   or (self.bundle.get_text(nm) if self.bundle and self.bundle.has_text(nm) else None)
                   or self.project.library.get(nm))
            if xml is None:
                continue
            try:
                c = Chunk.parse(xml)
                out[nm] = {"w": c.w, "h": c.h, "grid": c.active,
                           "bg": c.bg, "fg": c.fg, "bg_color": c.bg_color,
                           "enemies": [{"sx": e.sx, "sy": e.sy, "properties": e.properties}
                                       for e in c.enemies]}
            except Exception:
                pass
        return {"thumbs": out}

    def preview_day(self, date):
        """Stack a day's FULL ordered level (start + gameplay-in-current-order +
        checkpoints + end) into one chunk grid for a read-only 'see the whole
        level' view. Reuses preview_sequence; the start sits at the bottom (the
        player climbs up)."""
        seq = self.get_day_sequence(date)
        if seq.get("error"):
            return seq
        names = [r["name"] for r in seq["rows"] if r.get("name")]
        if not names:
            return {"error": f"no chunks to preview for {date}"}
        out = self.preview_sequence(names, first_at_bottom=True)
        if isinstance(out, dict) and not out.get("error"):
            out["preview_date"] = date
            out["chunk_count"] = len(names)
        return out

    def playtest_sequence(self, names, scope="mid", first_at_bottom=True):
        """Build the stacked sequence into a throwaway APK that floods today's
        run, install + launch so you land in it. Also sets it as the demo so the
        watch loop keeps overwriting the same level. Carries the project's
        ordered-list overrides / .so patches (e.g. the VIP-popup patch)."""
        if not self.xapk_path:
            return {"error": "load your Leap Day .xapk first"}
        from core import apkbuild, playtest
        names = [n for n in (names or []) if n]
        if not names:
            return {"error": "add at least one chunk to the sequence"}
        logs: list[str] = []
        if not apkbuild.ensure_device(log=logs.append):
            return {"error": "no emulator running and couldn't auto-start one.",
                    "log": logs}
        self.project.demo = names
        try:
            out = os.path.join(BUILD_DIR, "playtest")
            summary = playtest.playtest_sequence(
                self.xapk_path, names, self.project, self.bundle, self._catalog,
                out, scope=scope, first_at_bottom=first_at_bottom, log=logs.append)
            return {"ok": True, "log": logs, "flooded": summary.get("flooded"),
                    "sequence": names}
        except Exception as e:
            return {"error": str(e), "log": logs}

    def remove_level(self, name):
        self.project.levels.pop(name, None)
        return {"edited_levels": sorted(self.project.levels.keys())}

    def preview_xml(self, name):
        return {"xml": self.project.levels.get(name, "")}

    # ---- build ------------------------------------------------------------
    def adb_status(self):
        from core import apkbuild
        return {"devices": apkbuild.adb_devices()}

    def build(self, install=False):
        if not self.xapk_path:
            return {"error": "load your Leap Day .xapk first"}
        if self.project.is_empty():
            return {"error": "nothing to build — edit a level first"}
        logs: list[str] = []
        moved = self._sanitize_levels(self.project)   # custom-named levels -> library
        if moved:
            logs.append("[note] custom chunk(s) " + ", ".join(moved) + " moved to "
                        "Custom chunks — Insert them into a day to place them "
                        "(a new-named chunk can't be built standalone).")
        if install:
            from core import apkbuild
            apkbuild.ensure_device(log=logs.append)   # auto-start emulator if none
        try:
            summary = modbuild.build(self.xapk_path, self._resolved_project(),
                                     BUILD_DIR, install=install, log=logs.append)
            summary["log"] = logs
            return summary
        except Exception as e:
            return {"error": str(e), "log": logs}

    # ---- helpers ----------------------------------------------------------
    def _chunk_to_dict(self, name, c: Chunk):
        return {
            "name": name,
            "w": c.w, "h": c.h,
            "grid": c.active,
            "bg": c.bg, "fg": c.fg, "grid2": c.grid2,
            "enemies": [{"sx": e.sx, "sy": e.sy, "properties": e.properties} for e in c.enemies],
            "conns": [{"sx": cn.sx, "sy": cn.sy, "mx": cn.mx, "my": cn.my} for cn in c.conns],
            "paths": [{"x": p.x, "y": p.y, "pts": p.pts} for p in c.paths],
            "autotiles": [{"x": a.x, "y": a.y, "v": a.v} for a in c.autotiles],
            "difficulty": c.difficulty,
            "bg_color": c.bg_color,
            "extra_blocks": c.extra_blocks,
        }

    def _open_dialog(self, file_filter):
        return self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=(file_filter[0] if isinstance(file_filter, tuple) else file_filter,)
        )

    def _save_dialog(self, default):
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=default)
        return res if isinstance(res, str) else (res[0] if res else None)


def main():
    api = Api()
    window = webview.create_window(
        "Leap Day Mod Studio",
        os.path.join(HERE, "ui", "index.html"),
        js_api=api,
        width=1280, height=860, min_size=(1000, 700),
    )
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()
