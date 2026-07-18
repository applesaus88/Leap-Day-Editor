"""
dayorder.py — shape a day's level by reordering / swapping / deleting chunks.

A captured day plays a FIXED ordered list of chunk "slots" (the gameplay chunks,
between the start / checkpoints / end). The generator decides how many slots and
in what order, but each slot loads a specific, unique TextAsset — so we can make
a slot show any content by overwriting that TextAsset. That lets us:

  * REORDER  — put slot j's content into slot i (swap what plays where)
  * REPLACE  — point a slot at any other chunk's (or edited) content
  * DELETE   — fill a slot with an empty pass-through corridor
  * ADD      — limited: there are exactly len(slots) slots; a desired list longer
               than that is truncated (you can't lengthen the generator's level).

`day_order_levels` returns {original_slot_name -> XML} ready to drop into a mod's
`levels` (overwrite map). Read ALL desired contents before assigning so a
reorder that references a slot we're about to overwrite stays correct.
"""

from __future__ import annotations

from .chunkfmt import Chunk, EMPTY

# a minimal climbable pass-through: 14-wide side-wall corridor (a "deleted" slot)
EMPTY_CORRIDOR = Chunk.empty(14, 12).to_xml()


def day_order_levels(slots: list[str], desired: list[str],
                     resolve) -> dict[str, str]:
    """Map a desired ordered list onto a day's fixed gameplay slots.

    slots   : the day's gameplay chunk names, in play order (from the capture).
    desired : the user's wanted order — names to place at each position; a falsy
              entry ("" / None) means "deleted" -> empty corridor. Length may be
              shorter (trailing slots emptied) but not longer (extra truncated).
    resolve : name -> chunk XML (edited version if any, else the original).

    Returns {slot_name -> XML} to overwrite. The slot NAMES stay the game's
    (preserving its order/checkpoints); only their CONTENT changes.
    """
    # resolve all desired contents up front (avoid read-after-overwrite issues)
    contents = []
    for name in desired[:len(slots)]:
        contents.append(resolve(name) if name else EMPTY_CORRIDOR)
    levels: dict[str, str] = {}
    for i, slot in enumerate(slots):
        levels[slot] = contents[i] if i < len(contents) else EMPTY_CORRIDOR
    return levels


def compile_day_orders(project, index, resolve) -> dict[str, str]:
    """Build the combined overwrite map for every date the project reorders.

    project.day_orders: {date -> desired [names]}. index: the january_index dict
    ({date -> {slots:[...]}}). resolve: name -> XML. Returns merged {slot -> XML}.
    """
    out: dict[str, str] = {}
    for date, desired in project.day_orders.items():
        day = index.get(date)
        if not day:
            continue
        out.update(day_order_levels(day["slots"], desired, resolve))
    return out
