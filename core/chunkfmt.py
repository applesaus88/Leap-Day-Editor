"""
chunkfmt.py — parse / emit Leap Day level "chunk" XML.

A chunk looks like:

    <level w="14" h="19">
      <active>generic_06,-:12,generic_06:2,...</active>   # main tile layer
      <bg>...</bg>                                         # optional bg layer
      <fg>...</fg>                                         # optional fg layer
      <enemy sx="7" sy="9" properties="valentinesBlob"/>  # 0+ enemies
      <path .../> <traps>...</traps> <conn>...</conn>      # advanced (preserved verbatim)
      <autotile_type>...</autotile_type>
      <bgColor>0</bgColor>
      <difficulty>1</difficulty>
    </level>

Grid encoding (verified against all 3,647 shipped chunks):
  * `<active>` is a comma list of tokens, each `name` or `name:count`.
  * `-` is an empty cell. Cells fill ROW-MAJOR, row 0 = TOP, w columns wide.
  * total cells == w * h exactly.
  * enemy sx = column (0=left), sy = row (0=TOP).

We fully model active/bg/fg grids + enemies + difficulty + bgColor, and keep
any other tags (path/traps/conn/autotile_type/autotiles) as raw blocks so that
editing an existing chunk never drops data. Round-trip is stable (see tests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMPTY = "-"
_NUM = r"-?\d+(?:\.\d+)?"
_LEVEL_RE = re.compile(r'<level\s+w="(\d+)"\s+h="(\d+)"\s*>(.*)</level>', re.S)
_LAYER_RE = lambda tag: re.compile(rf"<{tag}>(.*?)</{tag}>", re.S)
_ENEMY_RE = re.compile(
    rf'<enemy\s+sx="({_NUM})"\s+sy="({_NUM})"\s+properties="([^"]*)"\s*/?>'
)
_SCALAR_RE = lambda tag: re.compile(rf"<{tag}>(.*?)</{tag}>", re.S)
_CONN_RE = re.compile(r'<conn\s+sx="(\d+)"\s+sy="(\d+)"\s+mx="(\d+)"\s+my="(\d+)"\s*/?>')
_PATH_RE = re.compile(r'<path\s+x="(\d+)"\s+y="(\d+)"\s+pts="([^"]*)"\s*/?>')
_AUTOTILE_RE = re.compile(r'<autotile_type\s+x="(\d+)"\s+y="(\d+)"\s*>([^<]*)</autotile_type>')


def _num(text: str) -> int | float:
    """Parse a numeric token, keeping int when it's integral (e.g. difficulty
    is usually an int but can be fractional like 0.5)."""
    text = text.strip()
    f = float(text)
    i = int(f)
    return i if (f == i and "." not in text) else f


def _fmt(n: int | float) -> str:
    return str(int(n)) if isinstance(n, int) else repr(n)


def _parse_pts(text: str) -> list[list[int]]:
    """'[9,20]>[4,20]>...' -> [[9,20],[4,20],...]"""
    out = []
    for seg in text.split(">"):
        seg = seg.strip().strip("[]")
        if not seg:
            continue
        x, y = seg.split(",")
        out.append([int(float(x)), int(float(y))])
    return out

# tags we model explicitly; everything else is preserved as a raw block
_KNOWN_TAGS = {"active", "bg", "fg", "grid2", "enemy", "bgColor", "difficulty",
               "conn", "path", "autotile_type"}


def decode_grid(text: str, w: int, h: int) -> list[list[str]]:
    """RLE text -> 2D grid [row][col], row 0 = top."""
    cells: list[str] = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            name, cnt = tok.rsplit(":", 1)
            cells.extend([name] * int(cnt))
        else:
            cells.append(tok)
    if len(cells) != w * h:
        raise ValueError(f"grid has {len(cells)} cells, expected {w*h} ({w}x{h})")
    return [cells[r * w:(r + 1) * w] for r in range(h)]


def encode_grid(grid: list[list[str]]) -> str:
    """2D grid -> RLE text, matching the game's run-length style."""
    flat: list[str] = [c for row in grid for c in row]
    out: list[str] = []
    i = 0
    n = len(flat)
    while i < n:
        j = i
        while j < n and flat[j] == flat[i]:
            j += 1
        run = j - i
        out.append(flat[i] if run == 1 else f"{flat[i]}:{run}")
        i = j
    return ",".join(out)


@dataclass
class Enemy:
    sx: int | float
    sy: int | float
    properties: str


@dataclass
class Conn:
    sx: int
    sy: int
    mx: int
    my: int


@dataclass
class Path:
    x: int
    y: int
    pts: list[list[int]]   # [[x,y], ...]


@dataclass
class Autotile:
    x: int
    y: int
    v: int                 # autotile variant/skin index


@dataclass
class Chunk:
    w: int
    h: int
    active: list[list[str]]
    difficulty: int | float = 1
    bg_color: int | float | None = 0
    bg: list[list[str]] | None = None
    fg: list[list[str]] | None = None
    grid2: list[list[str]] | None = None   # editor "second grid" — a half-cell-offset
                                           # block layer; merged into fg at build time
    enemies: list[Enemy] = field(default_factory=list)
    conns: list[Conn] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    autotiles: list[Autotile] = field(default_factory=list)
    extra_blocks: list[str] = field(default_factory=list)  # raw traps/autotiles/etc.

    # ---- construction -----------------------------------------------------
    @classmethod
    def empty(cls, w: int = 14, h: int = 19, border: str = "generic_06") -> "Chunk":
        """A blank level with left/right wall columns (the common base)."""
        grid = [[EMPTY] * w for _ in range(h)]
        if border:
            for r in range(h):
                grid[r][0] = border
                grid[r][w - 1] = border
        return cls(w=w, h=h, active=grid)

    @classmethod
    def parse(cls, xml: str) -> "Chunk":
        m = _LEVEL_RE.search(xml)
        if not m:
            raise ValueError("not a <level> chunk")
        w, h = int(m.group(1)), int(m.group(2))
        body = m.group(3)

        active = decode_grid(_LAYER_RE("active").search(body).group(1), w, h)
        bg = fg = None
        if (mb := _LAYER_RE("bg").search(body)):
            bg = decode_grid(mb.group(1), w, h)
        if (mf := _LAYER_RE("fg").search(body)):
            fg = decode_grid(mf.group(1), w, h)
        grid2 = None
        if (mg2 := _LAYER_RE("grid2").search(body)):
            grid2 = decode_grid(mg2.group(1), w, h)

        enemies = [Enemy(_num(sx), _num(sy), props)
                   for sx, sy, props in _ENEMY_RE.findall(body)]
        conns = [Conn(int(a), int(b), int(c), int(d))
                 for a, b, c, d in _CONN_RE.findall(body)]
        paths = [Path(int(x), int(y), _parse_pts(pts))
                 for x, y, pts in _PATH_RE.findall(body)]
        # some dev chunks wrap autotile_type entries in an <autotiles> block that
        # we preserve verbatim as an extra; don't also parse those individually.
        body_no_wrap = re.sub(r"<autotiles>.*?</autotiles>", "", body, flags=re.S)
        autotiles = [Autotile(int(x), int(y), int(float(v)))
                     for x, y, v in _AUTOTILE_RE.findall(body_no_wrap)]

        diff_m = _SCALAR_RE("difficulty").search(body)
        difficulty = _num(diff_m.group(1)) if diff_m else 1
        bgc_m = _SCALAR_RE("bgColor").search(body)
        bg_color = _num(bgc_m.group(1)) if bgc_m else None

        # preserve any other top-level tag block verbatim (traps/autotiles/...)
        extras: list[str] = []
        for tag_m in re.finditer(r"<([a-zA-Z0-9_]+)(\s[^>]*)?(/>|>.*?</\1>)", body, re.S):
            tag = tag_m.group(1)
            if tag in _KNOWN_TAGS:
                continue
            extras.append(tag_m.group(0).strip())

        return cls(w=w, h=h, active=active, difficulty=difficulty, bg_color=bg_color,
                   bg=bg, fg=fg, grid2=grid2, enemies=enemies, conns=conns, paths=paths,
                   autotiles=autotiles, extra_blocks=extras)

    # ---- emission ---------------------------------------------------------
    def to_xml(self, for_game: bool = False) -> str:
        """Serialize to <level> XML. for_game=True produces a chunk the GAME can
        read: the editor-only <grid2> (half-cell-offset second grid) is merged into
        <fg> at integer coords and its own tag dropped. for_game=False (editor save)
        keeps <grid2> separate so the offset layer round-trips."""
        # Every enemy must sit on an `ennemy0` spawn marker in the active grid —
        # the game pairs each <enemy> with that marker cell and won't spawn an
        # enemy that has none. Guarantee it here so enemies placed in the editor
        # actually appear in-game (work on a copy; don't mutate self).
        active = self.active
        if self.enemies:
            active = [row[:] for row in self.active]
            for e in self.enemies:
                sx, sy = int(round(e.sx)), int(round(e.sy))
                if 0 <= sy < self.h and 0 <= sx < self.w:
                    active[sy][sx] = "ennemy0"
        # the second grid: keep it separate for the editor; for the game merge its
        # (integer-coord) tiles down into fg so they render as normal foreground.
        fg = self.fg
        grid2 = self.grid2
        if for_game and grid2 is not None:
            fg = [row[:] for row in self.fg] if self.fg is not None \
                else [[EMPTY] * self.w for _ in range(self.h)]
            for r in range(min(self.h, len(grid2))):
                for c in range(min(self.w, len(grid2[r]))):
                    if grid2[r][c] != EMPTY:
                        fg[r][c] = grid2[r][c]
            grid2 = None
        lines = [f'<level w="{self.w}" h="{self.h}">']
        lines.append(f"  <active>{encode_grid(active)}</active>")
        if self.bg is not None:
            lines.append(f"  <bg>{encode_grid(self.bg)}</bg>")
        if fg is not None:
            lines.append(f"  <fg>{encode_grid(fg)}</fg>")
        if grid2 is not None:
            lines.append(f"  <grid2>{encode_grid(grid2)}</grid2>")
        for e in self.enemies:
            lines.append(
                f'  <enemy sx="{_fmt(e.sx)}" sy="{_fmt(e.sy)}" properties="{e.properties}"/>'
            )
        for cn in self.conns:
            lines.append(f'  <conn sx="{cn.sx}" sy="{cn.sy}" mx="{cn.mx}" my="{cn.my}"/>')
        for p in self.paths:
            pts = ">".join(f"[{x},{y}]" for x, y in p.pts)
            lines.append(f'  <path x="{p.x}" y="{p.y}" pts="{pts}"/>')
        for at in self.autotiles:
            lines.append(f'  <autotile_type x="{at.x}" y="{at.y}">{at.v}</autotile_type>')
        for block in self.extra_blocks:
            lines.append(f"  {block}")
        if self.bg_color is not None:
            lines.append(f"  <bgColor>{_fmt(self.bg_color)}</bgColor>")
        lines.append(f"  <difficulty>{_fmt(self.difficulty)}</difficulty>")
        lines.append("</level>")
        return "\n".join(lines)

    def set_width(self, w: int) -> "Chunk":
        """Resize to `w` columns (the game grid is 14 wide; a 15-wide chunk won't
        render in a standard slot). Rows are cropped/padded on the RIGHT — for the
        222 stock w=15 chunks the dropped 15th column is empty padding outside the
        right wall, so this is lossless. Elements past the new width are dropped."""
        self.active = _norm_rows(self.active, w, self.h)
        if self.bg is not None:
            self.bg = _norm_rows(self.bg, w, self.h)
        if self.fg is not None:
            self.fg = _norm_rows(self.fg, w, self.h)
        self.enemies = [e for e in self.enemies if int(round(e.sx)) < w]
        self.conns = [c for c in self.conns if c.sx < w and c.mx < w]
        self.paths = [p for p in self.paths if all(x < w for x, _ in p.pts)]
        self.autotiles = [a for a in self.autotiles if a.x < w]
        self.w = w
        return self

    # ---- helpers for the editor ------------------------------------------
    def set_cell(self, row: int, col: int, name: str) -> None:
        self.active[row][col] = name

    def tile_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for row in self.active:
            for c in row:
                hist[c] = hist.get(c, 0) + 1
        return hist

    def fill_dead_sides(self, tile: str = "generic_06") -> int:
        """Fill the empty active cells of the DEAD SIDE columns with `tile`
        (brick), so a wide chunk's side screens read as solid wall instead of
        empty background.

        A "dead side" is a contiguous run of columns on the LEFT or RIGHT edge
        that carries no gameplay whatsoever — no active tile, enemy, connection
        or path point. We fill only the columns OUTSIDE the gameplay span
        [min live col, max live col]; the play area and any interior gaps are
        left untouched (so the main path keeps its open background, and no cell
        the player uses is ever bricked over). Fills on the ACTIVE layer, matching
        the game's own bricked rooms (checkpoint_premium_fruit is solid
        generic_06). No-op on single-screen (≤14 wide) chunks. Returns the number
        of cells filled.
        """
        if self.w <= 14:
            return 0
        live: set[int] = set()
        for x in range(self.w):
            if any(self.active[y][x] != EMPTY for y in range(self.h)):
                live.add(x)
        for e in self.enemies:
            live.add(int(round(e.sx)))
        for c in self.conns:
            live.add(c.sx)
        for p in self.paths:
            live.add(p.x)
            live.update(px for px, _ in p.pts)
        live = {x for x in live if 0 <= x < self.w}
        if not live:
            return 0   # nothing to anchor to — don't brick a fully-empty chunk
        lo, hi = min(live), max(live)
        filled = 0
        for x in list(range(0, lo)) + list(range(hi + 1, self.w)):
            for y in range(self.h):
                if self.active[y][x] == EMPTY:
                    self.active[y][x] = tile
                    filled += 1
        return filled


VALID_WIDTHS = (14, 28, 42)   # the game grid is 14 wide; wide rooms are 2x/3x


def snap_width(w: int) -> int:
    """Nearest legal chunk width (14, 28 or 42). Ties go to the narrower."""
    return min(VALID_WIDTHS, key=lambda v: (abs(v - w), v))


def _norm_rows(grid: list[list[str]] | None, w: int, h: int) -> list[list[str]]:
    """Force a grid (or None) to exactly h rows x w cols, padding with EMPTY."""
    if grid is None:
        return [[EMPTY] * w for _ in range(h)]
    out = []
    for r in range(h):
        row = list(grid[r]) if r < len(grid) else []
        row = (row + [EMPTY] * w)[:w]
        out.append(row)
    return out


def stack_chunks(chunks: list["Chunk"], *, width: int = 14,
                 first_at_bottom: bool = True) -> "Chunk":
    """Vertically concatenate chunks into ONE tall chunk, in order.

    This is how an authored *sequence* of chunks ("these chunks, next to each
    other") becomes a single renderable level section: their grids are stacked,
    so when the day's chunk pool is flooded with the result the player climbs
    through exactly the chunks in the order given. Every chunk is normalised to
    `width` (default 14, the dominant Leap Day width); narrower rows are padded
    with empty cells, wider ones truncated.

    first_at_bottom=True (default) puts chunks[0] at the BOTTOM — i.e. it is the
    first one the player reaches when climbing up — so the list reads in play
    order. Set False to put chunks[0] at the top.

    Enemies, conns, paths and autotiles are shifted by each chunk's vertical
    offset so they stay aligned with their tiles. Difficulty becomes the max of
    the inputs; bgColor is taken from the first chunk.
    """
    if not chunks:
        raise ValueError("stack_chunks needs at least one chunk")
    order = list(reversed(chunks)) if first_at_bottom else list(chunks)
    # normalise each to (h_i x width); remember per-chunk height
    norm_active = [_norm_rows(c.active, width, c.h) for c in order]
    heights = [c.h for c in order]
    total_h = sum(heights)
    has_bg = any(c.bg is not None for c in order)
    has_fg = any(c.fg is not None for c in order)

    active: list[list[str]] = []
    bg: list[list[str]] | None = [] if has_bg else None
    fg: list[list[str]] | None = [] if has_fg else None
    enemies: list[Enemy] = []
    conns: list[Conn] = []
    paths: list[Path] = []
    autotiles: list[Autotile] = []

    offset = 0  # rows already placed above (row 0 = top)
    for c, agrid, h in zip(order, norm_active, heights):
        active.extend(agrid)
        if bg is not None:
            bg.extend(_norm_rows(c.bg, width, h))
        if fg is not None:
            fg.extend(_norm_rows(c.fg, width, h))
        for e in c.enemies:
            sx = min(int(e.sx), width - 1)
            enemies.append(Enemy(sx, e.sy + offset, e.properties))
        for cn in c.conns:
            conns.append(Conn(cn.sx, cn.sy + offset, cn.mx, cn.my + offset))
        for p in c.paths:
            paths.append(Path(p.x, p.y + offset,
                              [[x, y + offset] for x, y in p.pts]))
        for at in c.autotiles:
            autotiles.append(Autotile(at.x, at.y + offset, at.v))
        offset += h

    return Chunk(
        w=width, h=total_h, active=active,
        difficulty=max((c.difficulty for c in order), default=1),
        bg_color=order[0].bg_color,
        bg=bg, fg=fg, enemies=enemies, conns=conns, paths=paths,
        autotiles=autotiles,
    )
