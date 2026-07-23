"""
elements.py — the registry of configurable level ELEMENTS (traps + enemies).

Each entry drives one editor "sidebar" panel that appears when a matching tile is
selected, letting the author tune that element. Three mechanisms:

  "variant"  — the element ships as several directional sprites (e.g. spikes
               single/horizontal/vertical, AshSpout LEFT/RIGHT/UP). The panel is
               a picker; choosing one just swaps the placed TOKEN. No build edit.

  "fields"   — the element's behaviour lives in serialized MonoBehaviour fields
               (Conveyor.right, RotatingBlockTrap.m_RotationDuration, Cannon
               .m_Frequency…). The panel exposes those knobs; the build writes
               them onto the prefab (core/typetree.override_mono_fields). Each
               distinct config claims one CARRIER token (all carriers share the
               element's art, so overriding one doesn't disturb the presets).

  "mace"     — the special parametric Mace (firebar / spike-ball / log). Same as
               "fields" but the friendly knobs are mapped through core/firebar.py.

To add an element: append a dict here. The backend (studio/app.place_element) and
the UI (schema-driven panel) are generic — no other code changes needed.
"""

from __future__ import annotations

from . import firebar

# --- mechanism: variant (directional token swap) ---------------------------
_VARIANTS = [
    {"kind": "spikes", "label": "🔻 Spikes", "mechanism": "variant",
     "variants": [{"token": "spikes_single", "label": "single ▲"},
                  {"token": "spikes_hori", "label": "horizontal ⇄"},
                  {"token": "spikes_vert", "label": "vertical ↕"}]},
    {"kind": "ashspout", "label": "🌋 Ash spout", "mechanism": "variant",
     "variants": [{"token": "AshSpout_UP", "label": "up ▲"},
                  {"token": "AshSpout_LEFT", "label": "left ◀"},
                  {"token": "AshSpout_RIGHT", "label": "right ▶"}]},
    {"kind": "wallflower", "label": "🌼 Wall flower", "mechanism": "variant",
     "variants": [{"token": "WallFlower_UP", "label": "up ▲"},
                  {"token": "WallFlower_LEFT", "label": "left ◀"},
                  {"token": "WallFlower_RIGHT", "label": "right ▶"}]},
    {"kind": "wallflower_evil", "label": "🌺 Evil wall flower", "mechanism": "variant",
     "variants": [{"token": "WallFlowerEvil_UP", "label": "up ▲"},
                  {"token": "WallFlowerEvil_LEFT", "label": "left ◀"},
                  {"token": "WallFlowerEvil_RIGHT", "label": "right ▶"}]},
    {"kind": "greenpipe", "label": "🟢 Pipe", "mechanism": "variant",
     "variants": [{"token": "greenpipe_up", "label": "up ▲"},
                  {"token": "greenpipe_down", "label": "down ▼"},
                  {"token": "greenpipe_left", "label": "left ◀"},
                  {"token": "greenpipe_right", "label": "right ▶"}]},
    # ---- cannons: one per look, direction picked with a button ----------------
    # each cannon tile bakes its firing direction in, so the "direction" variants
    # just swap to the correctly-oriented tile. ghost/barrel/blue fire horizontally
    # (left/right); red fires vertically (up/down).
    {"kind": "cannon_ghost", "label": "💥 Cannon · ghost", "mechanism": "variant",
     "variants": [{"token": "trap126", "label": "right ▶"},
                  {"token": "trap127", "label": "left ◀"}]},
    {"kind": "cannon_barrel", "label": "💥 Cannon · barrel", "mechanism": "variant",
     "variants": [{"token": "trap128", "label": "right ▶"},
                  {"token": "trap129", "label": "left ◀"}]},
    {"kind": "cannon_blue", "label": "💥 Cannon · blue", "mechanism": "variant",
     "variants": [{"token": "trap130", "label": "right ▶"},
                  {"token": "trap131", "label": "left ◀"}]},
    {"kind": "cannon_red", "label": "💥 Cannon · red", "mechanism": "variant",
     "variants": [{"token": "trap133", "label": "up ▲"},
                  {"token": "trap132", "label": "down ▼"}]},
    # ---- checkpoint FLAG + RESPAWN point (paired, author-placed) --------------
    # Two placeable markers that both paint a HomingMissileCannon (the only prefab
    # with built-in proximity detection). The shipped native mod tells them apart
    # by firing direction and makes them passive (no shooting):
    #   • `homingcannonUp`   = 🚩 flag    — the checkpoint (fireDir points UP)
    #   • `homingcannonDown` = 🟢 respawn — where the player lands (fireDir DOWN)
    # On the pump, when a flag's checkpoint is reached the native mod moves that
    # checkpoint's respawn height onto the `respawn` marker in the SAME chunk. Both
    # tokens are reserved here (removed from the 🎯 Homing cannon carriers below).
    {"kind": "respawn_flag", "label": "🚩 flag", "mechanism": "variant",
     "variants": [{"token": "homingcannonUp", "label": "flag 🚩"}]},
    {"kind": "respawn_point", "label": "🟢 respawn", "mechanism": "variant",
     "variants": [{"token": "homingcannonDown", "label": "respawn 🟢"}]},
]

# --- mechanism: mace (parametric firebar / spike-ball / log) ----------------
# knobs are the firebar UI (parts / start / motion / double); carriers = POOLS.
_MACE = [
    {"kind": "firebar", "label": "🔥 Firebar", "mechanism": "mace",
     "carriers": firebar.CARRIERS},
    {"kind": "spikeball", "label": "⚫ Spike ball", "mechanism": "mace",
     "carriers": firebar.SPIKEBALL_CARRIERS},
    {"kind": "log", "label": "🪵 Log trap", "mechanism": "mace",
     "carriers": firebar.LOG_CARRIERS},
]

# --- mechanism: fields (raw serialized-field override) ----------------------
_FIELDS = [
    {"kind": "rotating_block", "label": "🔄 Rotating block", "mechanism": "fields",
     "cls": "RotatingBlockTrap",
     "carriers": ["trap_rotating_block2", "trap_rotating_block3",
                  "trap_rotating_block4", "trap_rotating_block5",
                  "trap_rotating_block6", "trap_rotating_block7",
                  "trap_rotating_block8"],
     "fields": [
         {"key": "m_RotationDuration", "type": "number", "default": 0.4,
          "min": 0.1, "max": 3, "step": 0.1, "label": "rotate time (s)"},
         {"key": "m_RotationInterval", "type": "number", "default": 1.5,
          "min": 0, "max": 6, "step": 0.1, "label": "wait between (s)"},
         {"key": "m_Clockwise", "type": "bool", "default": True,
          "label": "clockwise"},
     ]},
    {"kind": "conveyor", "label": "➡️ Conveyor", "mechanism": "fields",
     "cls": "Conveyor",
     "carriers": ["trap106", "trap107", "trap108", "trap109", "trap110", "trap111"],
     "fields": [
         {"key": "right", "type": "bool", "default": True,
          "label": "move right (off = left)"},
     ]},
]

# --- mechanism: fields on SHOOTING ENEMIES (projectile swap) ---------------
# Every shooting enemy carries its projectile as a serialized GameObject field on
# its own MonoBehaviour (WoolyTrunky.Snowball, Cupid.Arrow, …). Swapping that
# field's PPtr makes the enemy fire a different projectile — the same prefab-swap
# recipe as the cannon. Unlike traps there's ONE prefab per enemy, so the override
# attaches to the enemy's own token (via __carrier__) and affects every placement
# of that enemy in the build. All ten projectiles are offered for each.
_ALL_PROJECTILES = [
    {"value": "Fireball", "label": "🔥 fireball"},
    {"value": "Snowball", "label": "❄️ snowball"},
    {"value": "big_snowball", "label": "❄️ big snowball"},
    {"value": "Coconut", "label": "🥥 coconut"},
    {"value": "Bullet", "label": "• bullet"},
    {"value": "axe", "label": "🪓 axe"},
    {"value": "HomingMissile", "label": "🚀 homing missile"},
    {"value": "GiantCrabFishBulletAnimated", "label": "🐟 fish"},
    {"value": "fly", "label": "🪰 fly"},
    {"value": "AcidBall", "label": "🟢 acid ball"},
    {"value": "KingBullet", "label": "👑 king bullet"},
]


def _shooter(kind, label, cls, carriers, field, default, flabel="shoots"):
    """A projectile-swap panel for a shooting enemy: `field` is the class's
    projectile GameObject field (or an ObjectPool's `prefab` for pool-fed
    shooters, cls='ObjectPool'); the picked prefab is written onto it at build."""
    return {"kind": kind, "label": label, "mechanism": "fields", "cls": cls,
            "carriers": carriers, "enemy": True, "proj_field": field,
            "fields": [{"key": field, "type": "select", "coerce": "prefab",
                        "default": default, "label": flabel,
                        "options": _ALL_PROJECTILES}]}


_SHOOTERS = [
    _shooter("shooter_wooly", "🐘 Wooly trunky", "WoolyTrunky",
             ["woolyTrunky"], "Snowball", "Snowball"),
    _shooter("shooter_woolysr", "🐘 Big wooly trunky", "BigWoolyTrunky",
             ["woolyTrunkySr"], "Snowball", "Snowball"),
    _shooter("shooter_cupid", "💘 Cupid", "Cupid",
             ["valentinesCupid"], "Arrow", "Arrow"),
    _shooter("shooter_boomerang", "🪃 Totem boomeranger", "totemBoomeranger",
             ["totemBoomeranger"], "axe", "axe"),
    # NOTE: `homingcannonUp` (🚩 flag) and `homingcannonDown` (🟢 respawn) are
    # reserved for the checkpoint markers (see _VARIANTS), so the normal homing
    # cannon offers Left/Right only.
    _shooter("shooter_homingcannon", "🎯 Homing cannon", "HomingMissileCannon",
             ["homingcannonLeft",
              "homingcannonRight"], "HomingMissilePF", "HomingMissile"),
    _shooter("shooter_ghostpot", "👻 Ghost pot", "HomingGhostCauldron",
             ["GhostPotUp", "GhostPotUpBig", "GhostPotLeft", "GhostPotLeftBig",
              "GhostPotRight", "GhostPotRightBig"], "HomingGhostPF", "HomingGhost"),
    _shooter("shooter_crab", "🦀 Giant crab", "GiantCrab",
             ["GiantCrab"], "BulletPF", "GiantCrabFishBulletAnimated"),
    _shooter("shooter_skeleton", "💀 Skeleton soldier", "EnemyAnimSkullSoldier",
             ["skullsoldier"], "arrowPrefab", "Arrow"),
    _shooter("shooter_motherblob", "🟢 Mother blob", "MotherBlob",
             ["MotherBlob"], "smallBlopPrefab", "SmallBlob"),
    # trunky's projectile is a NON-null objectToShoot (baked 'bird'); worm's is
    # its own Worm.fly field — both direct swaps.
    _shooter("shooter_trunky", "🐘 Trunky", "EnemyWalking",
             ["trunky"], "objectToShoot", "bird"),
    _shooter("shooter_worm", "🪱 Worm", "Worm",
             ["worm"], "fly", "fly"),
    # asteroids "shoot" by breaking into a smaller asteroid (direct prefab field).
    _shooter("shooter_asteroid", "☄️ Asteroid", "Asteroid",
             ["AsteroidBig", "AsteroidSmall"], "mediumAsteroidPrefab",
             "AsteroidMedium", flabel="breaks into"),
    # ObjectPool-fed shooters: each of these enemies has exactly ONE ObjectPool,
    # so writing that pool's `prefab` (cls='ObjectPool') swaps what it fires.
    # NOTE: Manhole monster is intentionally NOT a projectile-swap shooter — it
    # can't fire different projectiles, so no "shoots" option is offered for it.
    _shooter("shooter_metaltrunky", "🐘 Metal trunky", "ObjectPool",
             ["MetalTrunky"], "prefab", "AcidBall"),
    _shooter("shooter_turtle", "🐢 Turtle snail", "ObjectPool",
             ["TurtleSnail"], "prefab", "TurtleSpike"),
    _shooter("shooter_palmtree", "🌴 Palm tree", "ObjectPool",
             ["PalmTreeSmall", "PalmTreeBig", "PalmTreeBiggest"], "prefab",
             "Coconut"),
]

PANELS = _VARIANTS + _MACE + _FIELDS + _SHOOTERS
BY_KIND = {p["kind"]: p for p in PANELS}

# token -> kind, so the UI can tell which panel a selected tile opens.
TOKEN_KIND: dict[str, str] = {}
for _p in PANELS:
    _toks = ([v["token"] for v in _p["variants"]] if _p["mechanism"] == "variant"
             else _p.get("carriers", []))
    for _t in _toks:
        TOKEN_KIND[_t] = _p["kind"]


def kind_of_token(token: str) -> str | None:
    """Which element panel (if any) a placed token belongs to."""
    return TOKEN_KIND.get(str(token or "").split("@")[0])


# a "select" field with coerce="dir2" maps a direction word to a Vector2.
DIR2 = {"right": {"x": 1.0, "y": 0.0}, "left": {"x": -1.0, "y": 0.0},
        "up": {"x": 0.0, "y": 1.0}, "down": {"x": 0.0, "y": -1.0}}


def field_values(panel: dict, settings: dict) -> dict:
    """Coerce UI `settings` to serialized field values for a 'fields' panel.
    A `coerce="prefab"` select becomes a {"__prefab__": name} marker that
    core/modbuild resolves to the target GameObject's PPtr at build time."""
    out = {}
    for f in panel.get("fields", []):
        if f["key"].startswith("__"):
            continue                       # meta (e.g. __style__) — not a game field
        v = settings.get(f["key"], f.get("default"))
        t, coerce = f["type"], f.get("coerce")
        if t == "bool":
            v = bool(v)
        elif t == "number":
            v = float(v)
            if float(v).is_integer() and f.get("step", 1) >= 1:
                v = int(v)
        elif t == "select":
            if coerce == "dir2":
                v = DIR2.get(v, DIR2["right"])
            elif coerce == "prefab":
                v = {"__prefab__": v}
        out[f["key"]] = v
    return out
