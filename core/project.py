"""
project.py — the .ldmod project model.

A .ldmod file is the *distributable mod*. It contains ONLY user-authored level
content — never any original game bytes — so it can be shared freely:

    {
      "format": 1,
      "name": "My Leap Day Levels",
      "levels": {                      # chunk-name -> authored chunk XML (overwrites)
        "s9_jk_blob_01": "<level w=...>...</level>",
        ...
      },
      "overrides": {                   # Level serialized ordered-list overrides
        "endChunksList": ["Levels/endchunks/end_mix_01", ...]
      },
      "patches": ["vip_popup"]         # named libil2cpp.so behaviour patches
    }

Core capability is the LEVEL EDITOR: it edits the game's text level "chunks".
Two optional advanced sections support targeted custom-level injection and
playtesting:

  * overrides : replace serialized ordered-list fields on the `Level`
    MonoBehaviour (e.g. pin `endChunksList` so the generator can only pick the
    end chunks we choose). Applied via type trees (core/typetree.py).
  * patches   : named byte patches to the user's own libil2cpp.so
    (core/sopatch.py) — currently just `vip_popup`, which stops the VIP/
    subscription popup from interrupting launch/playtest.

Everything stored here is authored config (chunk XML, chunk-path strings, patch
names) — never any original game bytes — so a .ldmod stays freely shareable.

The studio edits this object; core/modbuild.py applies it to the user's own
.xapk to produce a signed, installable game.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

FORMAT = 1


@dataclass
class Project:
    name: str = "Untitled Leap Day Mod"
    levels: dict[str, str] = field(default_factory=dict)        # chunk name -> XML
    library: dict[str, str] = field(default_factory=dict)       # custom-name -> XML (from-scratch chunks)
    sequences: dict[str, list[str]] = field(default_factory=dict)  # date -> [chunk names]
    day_orders: dict[str, list[str]] = field(default_factory=dict)  # date -> desired gameplay order
    day_structs: dict[str, list[int]] = field(default_factory=dict)  # date -> per-structural-item gameplay anchor (rides checkpoints up on insert)
    day_removed_structs: dict[str, list[int]] = field(default_factory=dict)  # date -> structural-item ordinals to DROP (remove a checkpoint from the day)
    day_custom_checkpoints: dict[str, list[int]] = field(default_factory=dict)  # date -> gameplay-slot indices to flag as a checkpoint (custom checkpoint)
    firebars: dict[str, dict] = field(default_factory=dict)          # carrier mace token -> Mace field overrides (universal firebar)
    element_overrides: dict[str, dict] = field(default_factory=dict)  # carrier token -> {"cls","fields"} (rotating block / conveyor / cannon / …)
    enemy_tuning: dict[str, dict] = field(default_factory=dict)       # "chunk|sx|sy" -> {"projectile","health","walk","h"}: per-INDIVIDUAL-enemy tuning, applied in-process by an embedded libnativemod.so (core/nativemod.py)
    axe: dict = field(default_factory=dict)                           # global axe-boomerang tunables {"range","speed","spin"} (blank = baked default); applied by libnativemod.so when any enemy throws the axe projectile
    demo: list[str] = field(default_factory=list)               # active playtest sequence
    overrides: dict[str, list[str]] = field(default_factory=dict)  # Level field -> list
    patches: list[str] = field(default_factory=list)            # .so patch names
    force_date: str | None = None                               # 'YYYY-MM-DD' to lock the daily level
    force_theme: int | None = None                              # preset theme index to force on the locked day
    force_character: int | None = None                          # character id to force the player to play as (CharacterManager id)
    checkpoint_fruit_cost: int | None = None                    # fruits every checkpoint costs to unlock (stock 20); None = leave default. .so patch, global.
    force_checkpoint_mode: int | None = None                    # pin PremiumCheckpoint.MODE: 0=FREE 1=AUTO(vip free-unlock) 2=FRUIT; None = leave default. .so patch.
    flag_checkpoints: bool = False                              # reskin checkpoint chests as flags + non-blocking collider (bundle typetree edit); trigger stays intact
    grapple_skin: int | None = None                             # character id whose LOOK is cloned onto Lick (play-as-Lick = that look + grapple)
    keep_music_bg: bool = False                                 # playtest Frida agent: music/SFX stay un-muffled + the background handled off the main-path column (wide 28/42 chunks)
    bg_mode: str = "full"                                       # background style when keep_music_bg is on: "full" = clone the theme scenery onto every screen; "bare" = strip the scenery everywhere (moon/stars sky only)
    smooth_camera: bool = False                                 # playtest Frida agent: on wide (>14-tile) chunks the camera tracks the player continuously instead of snapping between screen columns
    lock_camera_y: bool = False                                 # playtest Frida agent: lock the camera Y to EVERY chunk (frame the current chunk, never scroll into a vertical neighbour)
    lock_y_cap_top: bool = True                                 # lock_camera_y modifier: cap the TOP too (box the view in). False = cap only the bottom, leave the top open to see the chunk you're jumping into
    brick_dead_sides: bool = False                              # build: fill the dead SIDE columns of wide (>14) custom chunks with brick (generic_06) so their side screens read as wall instead of empty background
    hide_timer: bool = False                                    # playtest Frida agent: hide the run timer (SpeedrunCanvas.timerGO)
    hide_progress: bool = False                                 # playtest Frida agent: hide the progression bar (SpeedrunCanvas.lineParent)
    respawn_flags: bool = False                                 # arm the cannon→checkpoint agent at playtest (🚩 Respawn Flag placements become touch-to-save checkpoints)
    settings: dict = field(default_factory=lambda: {"enforce_width": True})  # studio prefs
    format: int = FORMAT

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path) as fh:
            data = json.load(fh)
        if data.get("format") != FORMAT:
            raise ValueError(f"unsupported .ldmod format {data.get('format')}")
        return cls(
            name=data.get("name", "Untitled"),
            levels=data.get("levels", {}),
            library=data.get("library", {}),
            sequences=data.get("sequences", {}),
            day_orders=data.get("day_orders", {}),
            day_structs=data.get("day_structs", {}),
            day_removed_structs=data.get("day_removed_structs", {}),
            day_custom_checkpoints=data.get("day_custom_checkpoints", {}),
            firebars=data.get("firebars", {}),
            element_overrides=data.get("element_overrides", {}),
            enemy_tuning=data.get("enemy_tuning", {}),
            axe=data.get("axe", {}),
            demo=data.get("demo", []),
            overrides=data.get("overrides", {}),
            patches=data.get("patches", []),
            force_date=data.get("force_date"),
            force_theme=data.get("force_theme"),
            force_character=data.get("force_character"),
            checkpoint_fruit_cost=data.get("checkpoint_fruit_cost"),
            force_checkpoint_mode=data.get("force_checkpoint_mode"),
            flag_checkpoints=data.get("flag_checkpoints", False),
            grapple_skin=data.get("grapple_skin"),
            # (older .ldmod files may carry "horizontal"/"horizontal_mode" — the
            # horizontal experiment was removed from the editor; keys are ignored)
            keep_music_bg=data.get("keep_music_bg", False),
            bg_mode=data.get("bg_mode", "full"),
            smooth_camera=data.get("smooth_camera", False),
            lock_camera_y=data.get("lock_camera_y", False),
            lock_y_cap_top=data.get("lock_y_cap_top", True),
            brick_dead_sides=data.get("brick_dead_sides", False),
            hide_timer=data.get("hide_timer", False),
            hide_progress=data.get("hide_progress", False),
            respawn_flags=data.get("respawn_flags", False),
            settings=data.get("settings") or {"enforce_width": True},
        )

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=1)

    def is_empty(self) -> bool:
        # `library` alone doesn't count — custom chunks only affect a build once a
        # day_order references them; an unused library shouldn't make a build.
        return not (self.levels or self.overrides or self.patches
                    or self.force_date or self.force_theme or self.day_orders
                    or self.firebars or self.element_overrides
                    or self.enemy_tuning
                    or self.force_character is not None
                    or self.grapple_skin is not None
                    or self.checkpoint_fruit_cost is not None
                    or self.force_checkpoint_mode is not None
                    or self.flag_checkpoints
                    or self.respawn_flags
                    or self.smooth_camera
                    or self.lock_camera_y
                    or self.hide_timer
                    or self.hide_progress)
