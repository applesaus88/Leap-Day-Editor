"""
override.py — compile a day's authored chunk list into `Level.overrideChunksNew`,
the in-game per-position chunk override the generator applies (checkAndOverride-
Chunks). This is the ROBUST "load exactly our chunks" path: the seed still picks
a level, then the override stamps our chunk paths over chosen positions by index,
so the element-based selection can never swap them.

Verified in-game (see project-chunk-override-system memory):
  * `chunkNr` is a DIRECT index into the generated list (Level @ 0x3e8); the
    gen list = the deterministic per-date level EXCLUDING chunk0 + inserted
    specials (tom_tv/reward_powerup/enable_notifications/missing_king_poster);
    checkpoints ARE in it at their own indices.
  * the override only matches when CHUNK_OVERRIDE_DEF.date == the game's
    "YYMMDD" date string (e.g. 2026-01-01 -> "260101").
  * overriding first-section indices (0..2) collapses the start -> we keep them.
  * unconnectable content hangs the generator -> caller must supply climbable,
    linking chunks (a content concern, not this module's).

The output structure (matches the Level type tree exactly):
  [ { "date": "YYMMDD",
      "chunks": [ { "chunkNr": int, "chunkPath": str,
                    "isCheckpoint": 0|1, "isEndChunk": 0|1,
                    "checkpointNr": int, "index": int }, ... ] } ]
plugged into Project.overrides["overrideChunksNew"] -> modbuild applies it.
"""

from __future__ import annotations

import re

# Every gen-list entry after chunk0 is overridable — verified in-game that
# overriding even the first gameplay slots with VALID (climbable, connecting)
# chunks loads fine; the earlier "start collapse" was unconnectable content, not
# the position. (chunk0, the true start, isn't in the gen list at all.) Kept as a
# knob in case a future title/intro slot needs protecting; 0 = edit everything.
FIRST_SECTION = 0


def to_game_date(date_str: str) -> str:
    """'2026-01-01' -> '260101' (the YYMMDD string the override matches on)."""
    y, m, d = date_str.split("-")
    return f"{y[2:]}{int(m):02d}{int(d):02d}"


def _is_structural(g: dict) -> bool:
    """A gen entry that ISN'T a plain gameplay slot — a checkpoint, the finish,
    the endzone ramp, a special, or the start. These ride between gameplay
    chunks and must move up WITH them when a chunk is inserted below."""
    return bool(g.get("is_checkpoint") or g.get("is_end") or g.get("is_endzone")
                or g.get("is_special") or g.get("is_start"))


def gameplay_slots(gen_list: list[dict]) -> list[dict]:
    """The override-able gameplay entries of a captured gen list, in order:
    not a checkpoint, not the end chunk, and past the first-section. Each item is
    the gen entry dict (with its 'chunkNr')."""
    out = []
    for g in gen_list:
        # checkpoints / the finish / the endzone ramp / specials / start are
        # functional or position-sensitive — never gameplay slots.
        if _is_structural(g):
            continue
        if g["chunkNr"] < FIRST_SECTION:
            continue
        out.append(g)
    return out


def structural_items(gen_list: list[dict]) -> list[dict]:
    """The NON-gameplay gen entries (checkpoints / specials / endzone / finish),
    in gen order — excluding the start, which is chunk0 and never overridden.
    Each rides above some gameplay chunk; see `baseline_struct_anchors`."""
    return [g for g in gen_list
            if _is_structural(g) and not g.get("is_start")]


def baseline_struct_anchors(gen_list: list[dict]) -> list[int]:
    """For each structural item (in `structural_items` order) the number of
    gameplay chunks BELOW it in the captured level — its "anchor". A checkpoint
    with anchor 3 sits just above the 3rd gameplay chunk. Inserting a gameplay
    chunk below it bumps the anchor (the studio does this), so the structural
    item rides up with everything above the insertion point."""
    anchors, gp = [], 0
    for g in gen_list:
        if g.get("is_start"):
            continue
        if _is_structural(g):
            anchors.append(gp)
        else:
            gp += 1
    return anchors


def _chunk_kind(path: str) -> str | None:
    """A swapped chunk's structural role, from its name — so the override can TAG
    the slot (isEndChunk / isCheckpoint) and the game treats it correctly instead
    of rendering a terminus/checkpoint as a plain (broken) gameplay chunk. Mirrors
    the editor's structuralKind()."""
    p = (path or "").lower()
    b = p.rsplit("/", 1)[-1]
    # end chunks live in .../endchunks/ and are named end_/end2_/end3_… or finish*.
    if "endchunks/" in p or b in ("finish", "finish2") \
            or (b.startswith("finish") and "endzone" not in b) \
            or re.match(r"^end\d*_", b):
        return "end"
    if "checkpoint" in b:
        return "checkpoint"
    return None


def compile_day_override(date_str: str, gen_list: list[dict],
                         desired: list[str | None]) -> dict | None:
    """Map an authored gameplay sequence onto a day's gen list.

    `gen_list`  : the captured generated list for `date_str` — a list of
                  {chunkNr:int, name:str, is_checkpoint:bool, is_end:bool}.
    `desired`   : chunk paths to place at successive gameplay slots (in order).
                  An entry that is None/'' leaves that slot's original chunk.
                  Longer-than-available is truncated (can't add slots).

    Returns one CHUNK_OVERRIDE_DEF dict, or None if nothing to override.
    """
    slots = gameplay_slots(gen_list)
    chunks = []
    for i, path in enumerate(desired):
        if i >= len(slots):
            break                                   # can't lengthen the level
        if not path:
            continue                                # keep original at this slot
        nr = slots[i]["chunkNr"]
        kind = _chunk_kind(path)          # tag the slot by what was swapped in
        chunks.append({"chunkNr": nr, "chunkPath": _full(path),
                       "isCheckpoint": 1 if kind == "checkpoint" else 0,
                       "isEndChunk": 1 if kind == "end" else 0,
                       "checkpointNr": 0, "index": nr})
    if not chunks:
        return None
    return {"date": to_game_date(date_str), "chunks": chunks}


def _full(path: str) -> str:
    """Chunk paths in the gen list are full ('Levels/v110/foo'); accept either a
    bare name or a full path from the caller and normalise to what the loader
    expects (the game resolves both, but keep whatever the gen list used)."""
    return path


def compile_day_build(date_str: str, gen_list: list[dict],
                      desired: list[str | None], resolve) -> tuple[dict | None, dict]:
    """Compile an authored day into (override_entry, levels) for modbuild.

    `desired` is one entry per gameplay slot (in order); '' / None keeps the
    slot's original chunk. `resolve(name)` returns:
        ("chunk", full_path)  -> a different EXISTING game chunk for this slot
        ("custom", xml)       -> authored content (no game chunk name)
        None                  -> unknown; skip (keep original)

    For a custom chunk we overwrite the slot's NATURAL chunk TextAsset with the
    authored XML and force that name to stay at the position via the override
    (so the element-based selection can't move it). For a game chunk we just
    point the override at its path. Returns:
        override_entry : one CHUNK_OVERRIDE_DEF (or None) for overrideChunksNew
        levels         : {bare_chunk_name: xml} of TextAsset overwrites
    """
    slots = gameplay_slots(gen_list)
    last_nr = max((s["chunkNr"] for s in slots), default=-1)
    chunks, levels = [], {}
    extra = 0
    for i, want in enumerate(desired):
        if not want:
            continue
        if i < len(slots):
            nat = slots[i]                          # natural gen entry at this slot
            nr, nat_path = nat["chunkNr"], nat["name"]
        else:
            # EXPERIMENTAL: author past the captured level length — append the
            # chunk at the next sequential chunkNr. Whether the runtime generator
            # actually has a position there (i.e. renders it) is unverified; if it
            # doesn't, these simply don't appear (the level stays its native size).
            nr, nat_path = last_nr + 1 + extra, None
            extra += 1
        kind = resolve(want)
        if kind is None:
            continue
        tag, payload = kind
        if tag == "custom":
            if nat_path is None:
                continue                            # no carrier for an appended slot
            levels[_bare(nat_path)] = payload       # overwrite the natural TextAsset
            path = nat_path                         # force the natural name to stay
        else:                                       # "chunk": a different game chunk
            path = payload
        role = _chunk_kind(want)                    # tag the slot by what was swapped in
        chunks.append({"chunkNr": nr, "chunkPath": path,
                       "isCheckpoint": 1 if role == "checkpoint" else 0,
                       "isEndChunk": 1 if role == "end" else 0,
                       "checkpointNr": 0, "index": nr})
    entry = {"date": to_game_date(date_str), "chunks": chunks} if chunks else None
    return entry, levels


def interleave_sequence(gen_list: list[dict], desired: list,
                        anchors: list[int]) -> list[dict]:
    """Rebuild the day's FULL ordered level from an authored gameplay list and
    the structural anchors, so structural items (checkpoints/specials/finish)
    sit above the gameplay chunk they're anchored to — wherever that chunk now
    is. Inserting a gameplay chunk below an anchor (which bumps it) therefore
    pushes that checkpoint up too.

    Returns a flat list, bottom -> top, of entries:
        {"role": "gameplay", "gp": i, "gen": <gen entry|None>}
        {"role": "struct",   "gen": <gen entry>}
    Each entry's index in the list IS its new chunkNr.
    """
    structs = structural_items(gen_list)
    gp_gen = gameplay_slots(gen_list)
    n = len(desired)
    # clamp any anchor that now points past the (possibly shorter) gameplay list
    anc = [min(max(0, a), n) for a in anchors]
    seq, si = [], 0
    for c in range(n + 1):
        while si < len(structs) and anc[si] == c:      # structs that sit at this height
            seq.append({"role": "struct", "gen": structs[si]})
            si += 1
        if c < n:
            seq.append({"role": "gameplay", "gp": c,
                        "gen": gp_gen[c] if c < len(gp_gen) else None})
    while si < len(structs):                            # anything anchored at/above the top
        seq.append({"role": "struct", "gen": structs[si]})
        si += 1
    return seq


def compile_day_build_full(date_str: str, gen_list: list[dict],
                           desired: list, anchors: list[int],
                           resolve, removed_structs=None,
                           custom_checkpoints=None) -> tuple[dict | None, dict]:
    """Like `compile_day_build`, but re-stamps the WHOLE level (gameplay AND
    structural items) at sequential chunkNrs so a mid-level insert shifts
    everything above it up — checkpoints included. Used when a day's authored
    list grew past its native length; pure reorders/edits keep the lighter
    gameplay-only `compile_day_build` path.

    `removed_structs` : set of structural-item ordinals (in structural_items /
        emitted-struct order) to DROP — e.g. remove a checkpoint from the day.
    `custom_checkpoints` : set of gameplay-slot indices to FLAG as a checkpoint
        (isCheckpoint:1) — a custom checkpoint at an author-chosen position.

    Emitted chunkNr / index / checkpointNr are renumbered contiguously AFTER the
    drops so the override stays a dense, correctly-numbered list.

    NOTE: this is the only path that re-emits checkpoint/special positions via
    the override. Needs an in-game playtest to confirm the runtime honours a
    moved / removed / added checkpoint.
    """
    removed_structs = set(removed_structs or [])
    custom_checkpoints = set(custom_checkpoints or [])
    seq = interleave_sequence(gen_list, desired, anchors)
    out, levels = [], {}
    struct_i = 0
    for e in seq:
        if e["role"] == "struct":
            si = struct_i
            struct_i += 1
            if si in removed_structs:
                continue                                # dropped checkpoint/special
            g = e["gen"]
            is_cp = bool(g.get("is_checkpoint"))
            out.append({"chunkPath": g["name"],
                        "isCheckpoint": 1 if is_cp else 0,
                        "isEndChunk": 1 if g.get("is_end") else 0,
                        "_cp": is_cp})
            continue
        want = desired[e["gp"]]
        if not want:
            continue                                    # emptied slot -> keep native
        nat_path = e["gen"]["name"] if e["gen"] else None
        kind = resolve(want)
        if kind is None:
            continue
        tag, payload = kind
        if tag == "custom":
            if nat_path is None:
                continue                                # no carrier for an appended slot
            levels[_bare(nat_path)] = payload
            path = nat_path
        else:
            path = payload
        role = _chunk_kind(want)                        # tag the slot by what was swapped in
        is_cp = (e["gp"] in custom_checkpoints) or role == "checkpoint"
        out.append({"chunkPath": path, "isCheckpoint": 1 if is_cp else 0,
                    "isEndChunk": 1 if role == "end" else 0, "_cp": is_cp})
    # The game won't load a level with more than 31 chunks — cap it (keeping the
    # end chunk) so an over-long authored day can't produce a broken build. The
    # editor also warns before you exceed it.
    MAX_CHUNKS = 56
    if len(out) > MAX_CHUNKS:
        end = [c for c in out if c.get("isEndChunk")]
        out = [c for c in out if not c.get("isEndChunk")][:MAX_CHUNKS - len(end)] + end
    # renumber contiguously (chunkNr/index) and re-derive checkpointNr in order.
    # checkpointNr is 1-INDEXED: 0 means "start / no checkpoint" to the game, so a
    # real checkpoint numbered 0 respawns at the start. First checkpoint = 1.
    chunks, cp_nr = [], 1
    for nr, c in enumerate(out):
        chunks.append({"chunkNr": nr, "chunkPath": c["chunkPath"],
                       "isCheckpoint": c["isCheckpoint"], "isEndChunk": c["isEndChunk"],
                       "checkpointNr": cp_nr if c["_cp"] else 0, "index": nr})
        if c["_cp"]:
            cp_nr += 1
    entry = {"date": to_game_date(date_str), "chunks": chunks} if chunks else None
    return entry, levels


def _bare(path: str) -> str:
    return path.split("/")[-1]


def compile_overrides(day_designs: dict[str, tuple[list[dict], list[str | None]]]
                      ) -> list[dict]:
    """Merge several days into one overrideChunksNew list.
    day_designs: {date_str: (gen_list, desired)}. Skips days that yield nothing.
    """
    out = []
    for date_str, (gen_list, desired) in day_designs.items():
        d = compile_day_override(date_str, gen_list, desired)
        if d:
            out.append(d)
    return out
