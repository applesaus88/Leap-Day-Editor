"""
sopatch.py — targeted byte patches inside libil2cpp.so (arm64, v1.142.2).

This is NOT the old difficulty-constant system (gravity/jump/checkpoint) — that
was intentionally removed. This module carries only *behaviour* patches that
support the level-injection workflow, applied to the user's own libil2cpp.so.

Each patch is located at a known file offset (from the Il2CppDumper dump) and is
written with a guard: the bytes currently at that offset must match what we
expect before we overwrite them, so a binary-version mismatch is detected and
skipped rather than corrupting the file. All edits are byte-exact.

PATCHES
-------
vip_popup : TitleScreen.IsVIPPopupActive() @ file offset 0x1409F14.
    The title screen gates play on a chain of Is*PopupActive() checks; the VIP /
    subscription popup intermittently interrupts launch and playtest navigation
    (the flaky close-X tap). We stub the function to `return false`
    (`mov w0, wzr; ret`) so the popup never blocks the title screen.
    Prologue we expect: `stp x30, x21, [sp, #-0x20]!` = FE 57 BE A9.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

# AArch64 little-endian encodings
MOV_W0_WZR = bytes.fromhex("e0031f2a")   # mov w0, wzr
MOV_W0_1 = bytes.fromhex("20008052")     # mov w0, #1
RET = bytes.fromhex("c0035fd6")          # ret
RET_FALSE = MOV_W0_WZR + RET             # bool stub: return false
RET_TRUE = MOV_W0_1 + RET                # bool stub: return true
NOP = bytes.fromhex("1f2003d5")          # nop (kill one instruction)


@dataclass(frozen=True)
class Patch:
    name: str
    offset: int
    expect: bytes      # bytes that must currently be present (version guard)
    replace: bytes     # bytes to write
    desc: str = ""


# Registry of available .so patches. Each name maps to one or more byte patches
# (a "group") applied together.
PATCHES: dict[str, list[Patch]] = {
    # NOTE: raising the per-level chunk count is NOT a simple patch. GetNumberOfChunks
    # (RVA 0x138B958) returns (masterPool size + 15) — the `add w0,w8,#0xf` at
    # 0x138BA3C — which is why a day tops out ~31. Bumping that immediate was tested
    # and the level GENERATOR HANGS on the loading screen: it can't produce more
    # connectable chunks than the day's pool provides, so it loops forever. Making
    # >31 work would need feeding the generator extra chunks (deep rework), not a
    # count bump. Left here as a record so it isn't re-attempted naively.
    "vip_popup": [
        Patch("vip_popup", 0x1409F14, bytes.fromhex("fe57bea9"), RET_FALSE,
              "Stub TitleScreen.IsVIPPopupActive() -> false"),
    ],
    # Make the game treat the player as a VIP / premium subscriber: the canonical
    # PremiumStatus.IsUserPremium() returns true, so VIP-gated content unlocks and
    # the subscription upsells self-suppress; hard-stub the title-screen upsell;
    # and force every character UNLOCKED (the premium/special characters like Lick
    # are otherwise gated by play-day counters + an online ownership check —
    # CharacterManager.IsCharacterLock — independent of the premium flag).
    "vip_unlock": [
        Patch("vip_unlock:IsUserPremium", 0x13E9260, bytes.fromhex("ff0301d1"),
              RET_TRUE, "PremiumStatus.IsUserPremium() -> true"),
        Patch("vip_unlock:ShouldShowTitleScreenUpsell", 0x13E97FC,
              bytes.fromhex("fe0f1ef8"), RET_FALSE,
              "PremiumSubscription.ShouldShowTitleScreenUpsell() -> false"),
        Patch("vip_unlock:IsCharacterLock_int", 0x14528A4, bytes.fromhex("fe57bea9"),
              RET_FALSE, "CharacterManager.IsCharacterLock(int) -> false (unlock all)"),
        Patch("vip_unlock:IsCharacterLock_pack", 0x145282C, bytes.fromhex("fe4fbfa9"),
              RET_FALSE, "CharacterManager.IsCharacterLock(pack) -> false"),
        Patch("vip_unlock:IsCharacterSilhouetteLock", 0x1452848, bytes.fromhex("fe57bea9"),
              RET_FALSE, "CharacterManager.IsCharacterSilhouetteLock(int) -> false"),
        # special characters (Lick, etc.) are also gated by play-day counters /
        # online unlock — with a frozen date + no wifi those never trigger, so
        # force "specials are unlockable" true.
        Patch("vip_unlock:CanUnlockSpecialCharacters", 0x12A3A0C, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "CharacterSubscription.CanUnlockSpecialCharacters() -> true"),
        Patch("vip_unlock:CanUnlockSpecialCharacters_i", 0x12A3A60, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "CharacterSubscription.CanUnlockSpecialCharacters(int) -> true"),
        # the selector HIDES special characters unless CanShowSubscriptionCharacters()
        # (a date+internet+subscription gate) passes — force it so they're visible.
        Patch("vip_unlock:CanShowSubscriptionCharacters", 0x1454F68, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "CharacterSelector.CanShowSubscriptionCharacters() -> true (show specials)"),
        # master visibility gate — its initial check is a per-character date/event
        # window that still hides some chars; force true so EVERY character shows.
        Patch("vip_unlock:CanShowCharacterByIndex", 0x1454DB8, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "CharacterSelector.CanShowCharacterByIndex(int) -> true (show ALL)"),
        # NOTE: do NOT patch isCharacterAlreadyAddedToUnlocked() true — it's used
        # while BUILDING the selector list (dup guard); forcing true makes the game
        # think every char is already added, so it adds none and the list empties
        # (Yolk + everyone vanish). Same caution for the egg/Yolk-pet specials:
        # GetUnlockDaysLeft/ShouldUnlockWithSub/IsCurrentCharSubOnly guesses did
        # NOT grant them and risked the list — left out. (Those specials are a
        # date-scheduled grant system; see project-january-chunk-capture memory.)
        # VIP subscription active everywhere it's checked (VIP-gated UI/features)
        Patch("vip_unlock:IsPrefsSubscriptionActive", 0x13E9124, bytes.fromhex("fe0f1df8"),
              RET_TRUE, "PremiumSubscription.IsPrefsSubscriptionActive() -> true"),
        # remove ads — gated separately from premium by AreAdsRemoved()
        Patch("vip_unlock:AdsManager.AreAdsRemoved", 0x154F000, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "AdsManager.AreAdsRemoved() -> true"),
        Patch("vip_unlock:IAPurchaser.AreAdsRemoved", 0x1550B18, bytes.fromhex("fe0f1ef8"),
              RET_TRUE, "IAPurchaser.AreAdsRemoved() (static) -> true"),
        Patch("vip_unlock:CanShowInterstitialAd", 0x1550580, bytes.fromhex("fe57bea9"),
              RET_FALSE, "AdsManager.CanShowInterstitialAd() -> false"),
        # force_date makes the game think it's a past date — it detects the
        # mismatch ("DATE IS INCORRECT" popup) and gates date-validated content
        # (special characters/events). Tell it the device date is always correct.
        Patch("vip_unlock:IsDevicesDateCorrect", 0x155C060, bytes.fromhex("ff0301d1"),
              RET_TRUE, "GameServices.IsDevicesDateCorrect() -> true (no date warning, ungate specials)"),
    ],
    # The daily generator drops a candidate chunk from the pool for the current
    # THEME unless all its elements (enemies/traps) are "wanted" and none are
    # "forbidden" for that theme — so a chunk with a theme-locked enemy (e.g.
    # `valentinesBlob` in Newyear) is discarded and a fallback chunk takes its
    # slot. The RUNTIME decision is in Level.filterChunks (isEnemyInWhatWeWant /
    # isTrapInWhatWeWant / isElementInForbidden) and ThemeFilter; force those so
    # every element passes in every theme and the authored chunk is kept.
    # (LevelOutput.ThemeData.AreAllElements* is the OFFLINE/build-time computation
    # of these lists, not the live gate — patched too as belt-and-suspenders.)
    "allow_all_elements": [
        # ★ THE DECISIVE PATCH: Level.filterChunks(CHUNK_SCAN_RESULT) reads the
        # scan verdict `result.shouldBeDiscarded` (byte 0) and, when set, jumps to
        # the discard path that drops the chunk from the pool (so a fallback chunk
        # takes its slot). NOP out that one branch so every chunk ALWAYS takes the
        # keep path — the theme/element check is completely disabled and the game
        # loads exactly the chunks we authored. (`tbnz w8,#0,#0x138bb18` ->`nop`)
        Patch("allow_all_elements:keepEveryChunk", 0x138B680,
              bytes.fromhex("c8240037"), NOP,
              "Level.filterChunks: never discard a chunk (NOP shouldBeDiscarded branch)"),
        # --- runtime gates (Level.filterChunks) — the live decision ---
        Patch("allow_all_elements:isEnemyInWhatWeWant", 0x138BD9C,
              bytes.fromhex("fe5fbda9"), RET_TRUE,
              "Level.isEnemyInWhatWeWant(name) -> true (every enemy wanted)"),
        Patch("allow_all_elements:isTrapInWhatWeWant", 0x138BEF0,
              bytes.fromhex("fe5fbda9"), RET_TRUE,
              "Level.isTrapInWhatWeWant(name) -> true (every trap wanted)"),
        Patch("allow_all_elements:isElementInForbidden", 0x138BCB0,
              bytes.fromhex("fe0f1cf8"), RET_FALSE,
              "Level.isElementInForbidden(name,type) -> false (nothing forbidden)"),
        Patch("allow_all_elements:shouldDiscardThisChunk", 0x13A6A3C,
              bytes.fromhex("ffc301d1"), RET_FALSE,
              "ThemeFilter.shouldDiscardThisChunk(r) -> false (never discard)"),
        # NOTE: the per-theme allowedEnemies/allowedTraps SPAWN pool is a data list,
        # not a code check — the engine picks the enemy FROM it, so forcing
        # isEnemyOnList/isTrapGroupOnList true does nothing. That gate is opened by a
        # typetree data edit instead (typetree.allow_all_elements_all_themes).
        # --- offline/build-time list computation (harmless extra coverage) ---
        Patch("allow_all_elements:NotForbidden", 0x13AE54C, bytes.fromhex("fe67bca9"),
              RET_TRUE, "LevelOutput.ThemeData.AreAllElementsNotForbidden() -> true"),
        Patch("allow_all_elements:Allowed", 0x13AE480, bytes.fromhex("fe67bca9"),
              RET_TRUE, "LevelOutput.ThemeData.AreAllElementsAllowed() -> true"),
    ],
    # Permanent grappling hook on ANY character, from spawn, no pickup. Stubbing
    # Player.IsGrapplingHookActive() to always-true runs the input handler so you
    # can grapple at will. The mechanic is correct and usable; the only limitation
    # is the hook draws as the bare pink line, not the animated metal-hook sprite
    # (that metal animation loads only via the powerup's REAL activation, which a
    # no-ads build can't reach: the reward box is ad-gated and won't open, and the
    # fruit-machine / save-restore grant paths don't route to it cleanly).
    "grappling_hook": [
        Patch("grappling_hook:IsGrapplingHookActive", 0x13B7170,
              bytes.fromhex("fe0f1ef8"), RET_TRUE,
              "Player.IsGrapplingHookActive() -> true (permanent grapple, any character)"),
    ],
}


def apply_patch(blob: bytearray, patch: Patch, *, log=print) -> bool:
    """Apply one patch to `blob` in place. Returns True if written.

    Skips (returns False) without modifying the blob if the guard bytes don't
    match — i.e. this isn't the .so version the offset was computed for.
    """
    cur = bytes(blob[patch.offset:patch.offset + len(patch.expect)])
    if cur != patch.expect:
        log(f"[sopatch] {patch.name} @ 0x{patch.offset:X}: expected "
            f"{patch.expect.hex()} but found {cur.hex()} — skipped "
            f"(version mismatch?)")
        return False
    blob[patch.offset:patch.offset + len(patch.replace)] = patch.replace
    log(f"[sopatch] {patch.name} @ 0x{patch.offset:X}: {patch.desc} "
        f"({patch.expect.hex()} -> {patch.replace.hex()})")
    return True


# ---- force_date: always load a specific calendar day's level ---------------
#
# Leap Day builds the daily level from the device date: calendar display,
# background theme, and the chunk-selection seed all derive from it (proven on
# the emulator). Freezing the three mscorlib DateTime getters that the game
# reads makes it ALWAYS generate one chosen day's level regardless of the real
# clock — no system-clock change needed, and a near-current date keeps TLS
# certs valid so networking still works.
#
# Each getter is overwritten with `movz/movk x0, <_dateData>; ret`, returning a
# fixed DateTime (ticks since 0001-01-01 in the low 62 bits, Kind in the top 2:
# Utc=0x40.., Local=0x80..). get_Today derives from get_Now and get_Now/UtcNow
# feed everything else, so freezing all three covers every date read.
_DATE_GETTERS = {
    # name        offset      guard (current prologue)   kind     time-of-day
    "get_Now":    (0x2212D9C, bytes.fromhex("ff0301d1"), "Local", "noon"),
    "get_Today":  (0x2213028, bytes.fromhex("fe0f1ef8"), "Local", "midnight"),
    "get_UtcNow": (0x2212EBC, bytes.fromhex("fe0f1ef8"), "Utc",   "noon"),
}
_KIND = {"Utc": 0x4000000000000000, "Local": 0x8000000000000000}


def _ticks(d: datetime.date, when: str) -> int:
    """.NET DateTime ticks (100ns since 0001-01-01) for a date, at midnight or
    noon. Noon is used for Now/UtcNow so any timezone offset stays on the day."""
    t = (d - datetime.date(1, 1, 1)).days * 864000000000
    if when == "noon":
        t += 12 * 3600 * 10**7
    return t


def _imm64_to_x0(value: int) -> bytes:
    """movz/movk sequence loading a 64-bit immediate into x0, then ret."""
    words = [(value >> (16 * i)) & 0xFFFF for i in range(4)]
    code = (0xD2800000 | (words[0] << 5)).to_bytes(4, "little")          # movz x0,#w0
    for i in (1, 2, 3):
        code += (0xF2800000 | (i << 21) | (words[i] << 5)).to_bytes(4, "little")  # movk x0,#wi,lsl 16i
    return code + RET


def force_date_patches(date_str: str) -> list[Patch]:
    """Patches that freeze the game's date to date_str ('YYYY-MM-DD')."""
    y, m, d = (int(x) for x in date_str.split("-"))
    day = datetime.date(y, m, d)
    patches = []
    for name, (off, guard, kind, when) in _DATE_GETTERS.items():
        data = _ticks(day, when) | _KIND[kind]
        patches.append(Patch(name=f"force_date:{name}", offset=off, expect=guard,
                             replace=_imm64_to_x0(data),
                             desc=f"{name} -> {date_str} ({kind})"))
    return patches


# ---- force_theme: pin the daily background theme ---------------------------
#
# The per-date background theme is computed by `Level.CalculateTheme(dateSeed)`
# (file offset 0x1375B94, arm64 v1.142.2) from a serialized theme cycle. Themes
# only change the BACKGROUND / atmosphere — gameplay tiles render in every theme
# (proven on the emulator) — so overwriting CalculateTheme's entry with a fixed
# `mov w0, #N; ret` makes the day render in theme N. Meaningful alongside a
# locked date (the build's force_date), since then every run is that one day.
#
# Theme N -> name (Level.Theme enum, from the il2cpp dump):
THEME_NAMES = [
    "Castle", "Spooky", "Waterfall", "Forest", "Magma", "Windy", "Desert",
    "Space", "Clockwork", "Tropical", "Totem", "City", "Beach", "Electricity",
    "Icetemple", "Snow", "Newyear", "Casino", "Swamp", "Toxic",
]
THEME_MAX = len(THEME_NAMES)  # Level.Theme.MAX == 20
_CALCULATE_THEME_OFFSET = 0x1375B94
_CALCULATE_THEME_GUARD = bytes.fromhex("ff4303d1")   # sub sp, sp, #0xd0 (prologue)


def force_theme_patch(theme_index: int) -> Patch:
    """A patch that pins Level.CalculateTheme() to always return `theme_index`."""
    n = int(theme_index)
    if not (0 <= n < THEME_MAX):
        raise ValueError(f"theme index {n} out of range 0..{THEME_MAX - 1}")
    replace = (0x52800000 | (n << 5)).to_bytes(4, "little") + RET  # mov w0,#n; ret
    return Patch(name=f"force_theme:{THEME_NAMES[n]}",
                 offset=_CALCULATE_THEME_OFFSET, expect=_CALCULATE_THEME_GUARD,
                 replace=replace,
                 desc=f"Level.CalculateTheme() -> {THEME_NAMES[n]} ({n})")


# ---- force_character: always play as a chosen character -------------------
#
# CharacterManager.Awake() initialises the equipped character at boot with
# `currentCharacter = PlayerPrefs.GetInt(key, 0)`:
#   0x144D68C: bl <PlayerPrefs.GetInt>   (w0 = saved char, default 0 = Yolk)
#   0x144D694: str w0, [x19, #0x5c]      (this.currentCharacter = w0)
# The player reads `currentCharacter` INLINE (the get_CurrentCharacter property
# has ZERO callers — il2cpp inlined it, which is why stubbing the getter did
# nothing). So we overwrite the GetInt call with `mov w0, #ID`: the field is
# forced to character ID at boot, before any player spawns, regardless of the
# saved selection. Forcing Lick (ID 1) also grants his grapple/tongue, since the
# ability is part of the character. (Found via work/dump + static xref/disasm.)
CHARACTER_NAMES = [
    "Yolk", "Lick", "La Beef", "Kepi", "Meep", "Smooch", "Solder_26", "Root",
    "Scalp", "Grill", "Zweiclops", "Logga", "Rasbunny", "Venus", "Croak", "Slab",
    "Whip", "Glug", "Flake", "Tuft", "Char", "Turret", "Barley", "Mcloud",
    "Victor", "Trunk", "Tickle", "Jock", "Shroom", "Bleep", "Felon", "Chuckles",
    "Helm", "Nibbler", "Buzz", "Ram", "Green_Ninja", "Gunbrick", "Hopswap",
    "Panic_Bot", "Sir_Gylbard", "Roller_Polar", "Rustbucket", "Silly_Sausage",
    "Stretch_Dungeon", "Ultimate_Briefcase", "Wick", "Suplex", "Puddle",
    "Magic Touch", "Beneath The Lighthouse", "Icebreaker", "Faucet", "Calamari",
    "Apple", "8bit Doves", "Dino Yolk", "Robot Yolk", "Muscle Yolk", "Hoodie",
    "Maul",
]
CHARACTER_MAX = len(CHARACTER_NAMES)  # 61
_FORCE_CHARACTER_OFFSET = 0x144D68C
_FORCE_CHARACTER_GUARD = bytes.fromhex("5b795094")   # bl <PlayerPrefs.GetInt> in Awake


def force_character_patch(char_id: int) -> Patch:
    """A patch that forces CharacterManager.Awake()'s boot init of
    `currentCharacter` to `char_id` (replacing the PlayerPrefs.GetInt call with
    `mov w0, #char_id`)."""
    n = int(char_id)
    if not (0 <= n < CHARACTER_MAX):
        raise ValueError(f"character id {n} out of range 0..{CHARACTER_MAX - 1}")
    replace = (0x52800000 | (n << 5)).to_bytes(4, "little")  # mov w0,#n
    name = CHARACTER_NAMES[n]
    return Patch(name=f"force_character:{name}",
                 offset=_FORCE_CHARACTER_OFFSET,
                 expect=_FORCE_CHARACTER_GUARD, replace=replace,
                 desc=f"Awake: currentCharacter = {name} ({n}) at boot")


# ---- checkpoint_fruit_cost: how many fruits a checkpoint costs to unlock ----
# The stock cost is Globals.CHECKPOINT_FRUIT_COST = 20, a compile-time const
# inlined into three ARM64 sites (all in the arm64 libil2cpp.so, guarded to the
# 1.142.2 build). Changing all three keeps the gate, the deduction, and the
# number drawn on the checkpoint sign in agreement:
#   1. Chest.HaveEnoughFruits @ 0x1438308: `cmp w8,#0x13` (player fruits > 19,
#      i.e. >= 20)  ->  `cmp w8,#(n-1)`.
#   2. Chest.OpenPremiumFruitChest @ 0x14388AC: `sub w9,w9,#0x14` (deduct 20
#      from the player fruit field +0x518)  ->  `sub w9,w9,#n`.
#   3. TwoChests.setCheckpointFruits caller @ 0x14F320C: `ldr w1,[x8,#0x54c]`
#      (loads the sign number field, then tail-calls Level.setCheckpointFruits
#      which draws it)  ->  `movz w1,#n`, so the sign always shows n.
# This is a GLOBAL cost — every checkpoint charges the same n. Per-checkpoint
# costs aren't reachable from a single const. imm12 caps n at 4095.
# offsets are FILE offsets (dump "Offset:" = RVA - 0x4000), what apply_patch indexes.
_CP_COST_GATE = (0x1434308, 0x71004D1F)   # RVA 0x1438308  cmp w8,#0x13
_CP_COST_SUB  = (0x14348AC, 0x51005129)   # RVA 0x14388AC  sub w9,w9,#0x14
_CP_COST_SIGN = (0x14EF20C, 0xB9454D01)   # RVA 0x14F320C  ldr w1,[x8,#0x54c]
CHECKPOINT_FRUIT_COST_STOCK = 20
CHECKPOINT_FRUIT_COST_MAX = 4095


def checkpoint_fruit_cost_patches(n: int) -> list[Patch]:
    """Three patches that set the per-checkpoint fruit cost to `n` (gate,
    deduction, and the sign number all in agreement). n=0 = free (always
    affordable)."""
    n = int(n)
    if not (0 <= n <= CHECKPOINT_FRUIT_COST_MAX):
        raise ValueError(f"checkpoint fruit cost {n} out of range "
                         f"0..{CHECKPOINT_FRUIT_COST_MAX}")
    if n == 0:
        # `cmp w8,#-1` isn't encodable; `cmn w8,#1` + the existing `cset w0,gt`
        # means fruits > -1 -> always affordable.
        gate = 0x3100051F                                 # cmn w8,#1
    else:
        gate = 0x71000000 | ((n - 1) & 0xFFF) << 10 | 0x11F   # cmp w8,#(n-1)
    sub  = 0x51000000 | (n & 0xFFF) << 10 | 0x129         # sub w9,w9,#n (n=0 -> -0)
    sign = 0x52800001 | (n & 0xFFFF) << 5                 # movz w1,#n
    return [
        Patch("checkpoint_fruit_cost:gate", _CP_COST_GATE[0],
              _CP_COST_GATE[1].to_bytes(4, "little"), gate.to_bytes(4, "little"),
              f"HaveEnoughFruits: require >= {n} fruits"),
        Patch("checkpoint_fruit_cost:deduct", _CP_COST_SUB[0],
              _CP_COST_SUB[1].to_bytes(4, "little"), sub.to_bytes(4, "little"),
              f"OpenPremiumFruitChest: deduct {n} fruits"),
        Patch("checkpoint_fruit_cost:sign", _CP_COST_SIGN[0],
              _CP_COST_SIGN[1].to_bytes(4, "little"), sign.to_bytes(4, "little"),
              f"setCheckpointFruits: sign shows {n}"),
    ]


# ---- force_checkpoint_mode: make every checkpoint use one mode ---------------
# PremiumCheckpoint.MODE { NONE=-1, FREE=0, AUTO=1, FRUIT=2 }. CURRENT_MODE is
# initialised from GetPlayerPrefsMode() (reads PlayerPrefs "auto_checkpoint") or
# GetDefaultMode() when the pref is unset. Stub BOTH getters to return the chosen
# mode and CURRENT_MODE is pinned regardless of prefs / premium state:
#   FREE  = "FREE UNLOCK!" (normally ad-gated; unreliable in a no-ads build)
#   AUTO  = auto-unlock when the player passes — no fruits, no ad (the VIP feel)
#   FRUIT = the fruit-price chest (the "pay N fruits" chest)
# Both getters share the prologue `str x30,[sp,#-0x20]!; stp x20,x19,[sp,#0x10]`.
_CP_MODE_GETTERS = (0x13E8008, 0x13E85B8)   # file offs: GetPlayerPrefsMode, GetDefaultMode
_CP_MODE_GUARD = bytes.fromhex("fe0f1ef8f44f01a9")
CHECKPOINT_MODES = {0: "free", 1: "auto", 2: "fruit"}


def checkpoint_mode_patches(mode: int) -> list[Patch]:
    """Two stubs that pin PremiumCheckpoint.CURRENT_MODE to `mode` (0/1/2)."""
    m = int(mode)
    if m not in CHECKPOINT_MODES:
        raise ValueError(f"checkpoint mode {m} not in {sorted(CHECKPOINT_MODES)}")
    stub = (0x52800000 | (m << 5)).to_bytes(4, "little") + (0xD65F03C0).to_bytes(4, "little")
    return [Patch(f"force_checkpoint_mode:{CHECKPOINT_MODES[m]}@{off:X}", off,
                  _CP_MODE_GUARD, stub,
                  f"getter -> return MODE {m} ({CHECKPOINT_MODES[m]})")
            for off in _CP_MODE_GETTERS]


def apply(so_path: str, names: list[str], *, force_date: str | None = None,
          force_theme: int | None = None, force_character: int | None = None,
          checkpoint_fruit_cost: int | None = None,
          force_checkpoint_mode: int | None = None,
          out_path: str | None = None, log=print) -> int:
    """Apply named patches (and optionally a force_date / force_theme) to the .so.

    Writes to out_path (or back to so_path if None). Returns how many patches
    were actually applied. Unknown names raise; guard-mismatched patches are
    skipped (counted as not applied).
    """
    with open(so_path, "rb") as f:
        blob = bytearray(f.read())
    applied = 0
    todo: list[Patch] = []
    for n in names:
        if n not in PATCHES:
            _unknown(n)
        todo += PATCHES[n]
    if force_date:
        todo += force_date_patches(force_date)
    if force_theme is not None:
        todo.append(force_theme_patch(force_theme))
    if force_character is not None:
        todo.append(force_character_patch(force_character))
    if checkpoint_fruit_cost is not None:
        todo += checkpoint_fruit_cost_patches(checkpoint_fruit_cost)
    if force_checkpoint_mode is not None:
        todo += checkpoint_mode_patches(force_checkpoint_mode)
    for patch in todo:
        if apply_patch(blob, patch, log=log):
            applied += 1
    dest = out_path or so_path
    with open(dest, "wb") as f:
        f.write(blob)
    return applied


def _unknown(name: str):
    raise KeyError(f"unknown .so patch {name!r}; available: {sorted(PATCHES)}")
