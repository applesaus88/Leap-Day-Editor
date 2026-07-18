"""
firebar.py — one parametric "firebar" (the game's `Mace` trap) instead of the
17 fixed mace presets the game ships.

A Mace is a cog pivot with an arm of spiked balls that spins around it. The game
bakes ~17 prefabs (trap22/23/25/27-34/15/15b/log1-3...) each with a fixed config
in a `Mace` MonoBehaviour. The serialized fields are exactly the knobs a level
author wants:

    chainLengthInTiles : balls per arm           -> "amount of red parts"
    doubleMace         : 0/1 arm on both sides   -> "is it double"
    angularSpeed       : sign = spin direction   -> +abs = CCW, -abs = CW
    progress           : start angle (radians)   -> "start offset"
    circularMovement   : 1 spins / 0 swings linearly

So a universal firebar = pick a carrier mace token, write these fields onto its
prefab at build time (see core/typetree.override_mono_fields). Each DISTINCT
config claims one carrier token from CARRIERS; identical configs share one.
Overriding a token's prefab changes every placement of that token in the build —
fine for a mod, but see the caveat in studio/app.place_firebar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# progress (radians) for each start-pointing direction, read off the real
# presets: right=0, up=+pi/2, down=-pi/2, left=pi.
START_RAD = {"right": 0.0, "up": math.pi / 2, "down": -math.pi / 2, "left": math.pi}

# Carrier mace tokens. ALL must share the same art (the `tile_fire-2` firebar
# sprite) so every placed firebar looks like a firebar regardless of config —
# overriding the Mace fields changes geometry/spin, NOT the ball art. The other
# mace tokens render as a grey spike ball (mace_ball: trap22/15/15b) or a log
# (log_trap*: trap_log1/2/3/2b), so they're deliberately NOT used here.
# (Verified each token's resolved sprite from the bundle.)
CARRIERS = [
    "trap27", "trap28", "trap29", "trap30",
    "trap31", "trap32", "trap33", "trap34",
    "trap23", "trap25",
]

# The same parametric Mace, but carriers whose art is a grey SPIKE BALL
# (mace_ball) or a rolling LOG (log_trap) instead of the fire bar. Overriding
# their Mace fields changes geometry/spin but keeps their own art — so the user
# gets a configurable spike-ball or log trap. Each pool is its own set of slots.
SPIKEBALL_CARRIERS = ["trap22", "trap15", "trap15b"]
LOG_CARRIERS = ["trap_log1", "trap_log2", "trap_log2b", "trap_log3"]

# kind -> carrier pool. "firebar" is the default (backward-compatible).
POOLS = {
    "firebar": CARRIERS,
    "spikeball": SPIKEBALL_CARRIERS,
    "log": LOG_CARRIERS,
}
ALL_CARRIERS = CARRIERS + SPIKEBALL_CARRIERS + LOG_CARRIERS


def kind_of(token: str) -> str | None:
    """Which trap kind a carrier token belongs to (or None if not a carrier)."""
    for kind, pool in POOLS.items():
        if token in pool:
            return kind
    return None

LENGTH_MIN, LENGTH_MAX = 1, 8       # observed presets go 3..6; clamp sane
SPEED_DEFAULT = 2.0


@dataclass
class Firebar:
    length: int = 3                 # chainLengthInTiles
    double: bool = False            # doubleMace
    clockwise: bool = True          # angularSpeed sign (CW = negative)
    start: str = "right"            # progress: right/up/down/left
    circular: bool = True           # circularMovement (False = linear swing)
    speed: float = SPEED_DEFAULT    # |angularSpeed|

    def normalized(self) -> "Firebar":
        return Firebar(
            length=max(LENGTH_MIN, min(LENGTH_MAX, int(self.length))),
            double=bool(self.double),
            clockwise=bool(self.clockwise),
            start=self.start if self.start in START_RAD else "right",
            circular=bool(self.circular),
            speed=abs(float(self.speed)) or SPEED_DEFAULT,
        )

    def fields(self) -> dict:
        """The Mace MonoBehaviour fields to write for this config."""
        fb = self.normalized()
        return {
            "chainLengthInTiles": fb.length,
            "doubleMace": 1 if fb.double else 0,
            "circularMovement": 1 if fb.circular else 0,
            "angularSpeed": (-1.0 if fb.clockwise else 1.0) * fb.speed,
            "progress": START_RAD[fb.start],
        }

    def key(self) -> tuple:
        """Identity for de-duping configs onto carriers."""
        f = self.fields()
        return tuple(sorted((k, round(v, 4) if isinstance(v, float) else v)
                            for k, v in f.items()))

    def summary(self) -> str:
        fb = self.normalized()
        return (f"{'double' if fb.double else 'single'} · {fb.length} parts · "
                f"{'CW' if fb.clockwise else 'CCW'} · start {fb.start}"
                + ("" if fb.circular else " · swing"))


def from_settings(s: dict) -> Firebar:
    return Firebar(
        length=s.get("length", 3),
        double=bool(s.get("double", False)),
        clockwise=bool(s.get("clockwise", True)),
        start=s.get("start", "right"),
        circular=bool(s.get("circular", True)),
        speed=float(s.get("speed", SPEED_DEFAULT)),
    ).normalized()
