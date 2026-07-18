"""
axml.py — minimal editor for Android binary XML (AndroidManifest.xml).

Just enough to do two things to a compiled manifest WITHOUT apktool/aapt:

  * rename the package + the provider authorities / custom permissions that embed
    it (so a modded build installs ALONGSIDE the original instead of colliding —
    Android rejects a second app that reuses a provider authority or a custom
    permission name), and set a distinct application label.
  * strip ad / billing / Play-store components (providers, services, activities,
    receivers, permissions, meta-data) so the SDKs don't auto-initialise.

We only ever EDIT existing string-pool entries (keeping count + order, so every
string-index reference in the tree stays valid), APPEND new strings at the end
(indices past the resource map — safe), set an existing attribute's value, or
DROP whole element subtrees. We never rewrite component class names, so the
unchanged dex still resolves every class.

Binary XML layout (little-endian), per chunk: u16 type, u16 headerSize, u32 size.
  0x0001 string pool · 0x0180 resource map · 0x0100/0x0101 namespace ·
  0x0102 start element · 0x0103 end element · 0x0104 cdata
Reference: AOSP ResourceTypes.h.
"""

from __future__ import annotations

import struct

RES_XML_TYPE = 0x0003
RES_STRING_POOL_TYPE = 0x0001
RES_XML_RESOURCE_MAP_TYPE = 0x0180
RES_XML_START_NAMESPACE = 0x0100
RES_XML_END_NAMESPACE = 0x0101
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
RES_XML_CDATA = 0x0104

UTF8_FLAG = 1 << 8
SORTED_FLAG = 1 << 0

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03

NO_ENTRY = 0xFFFFFFFF


# ---- string pool (de)serialisation ---------------------------------------

def _dec_len16(buf, off):
    """Decode a UTF-16 string length (chars). High bit on the first u16 means a
    second u16 follows (32-bit length). Returns (length, next_off)."""
    v = struct.unpack_from("<H", buf, off)[0]
    off += 2
    if v & 0x8000:
        v2 = struct.unpack_from("<H", buf, off)[0]
        off += 2
        v = ((v & 0x7FFF) << 16) | v2
    return v, off


def _dec_len8(buf, off):
    """Decode a UTF-8 length byte (or two). High bit -> two bytes."""
    v = buf[off]
    off += 1
    if v & 0x80:
        v = ((v & 0x7F) << 8) | buf[off]
        off += 1
    return v, off


def _enc_len16(n):
    if n > 0x7FFF:
        return struct.pack("<HH", (n >> 16) | 0x8000, n & 0xFFFF)
    return struct.pack("<H", n)


def _enc_len8(n):
    if n > 0x7F:
        return bytes([(n >> 8) | 0x80, n & 0xFF])
    return bytes([n])


class StringPool:
    def __init__(self, strings, flags):
        self.strings = strings        # list[str]
        self.flags = flags

    @property
    def utf8(self):
        return bool(self.flags & UTF8_FLAG)

    @classmethod
    def parse(cls, buf, start):
        typ, hsize, size = struct.unpack_from("<HHI", buf, start)
        assert typ == RES_STRING_POOL_TYPE, f"not a string pool @ {start}"
        n_str, n_sty, flags, str_start, sty_start = struct.unpack_from(
            "<IIIII", buf, start + 8)
        offs = struct.unpack_from(f"<{n_str}I", buf, start + 28)
        data0 = start + str_start
        utf8 = bool(flags & UTF8_FLAG)
        out = []
        for o in offs:
            p = data0 + o
            if utf8:
                _, p = _dec_len8(buf, p)        # char count (unused)
                blen, p = _dec_len8(buf, p)     # byte count
                out.append(buf[p:p + blen].decode("utf-8"))
            else:
                clen, p = _dec_len16(buf, p)
                out.append(buf[p:p + 2 * clen].decode("utf-16-le"))
        return cls(out, flags), start + size

    def serialize(self):
        # drop SORTED (we may change contents/order of meaning); linear lookup is fine
        flags = self.flags & ~SORTED_FLAG
        utf8 = bool(flags & UTF8_FLAG)
        blob, offs = bytearray(), []
        for s in self.strings:
            offs.append(len(blob))
            if utf8:
                enc = s.encode("utf-8")
                blob += _enc_len8(len(s)) + _enc_len8(len(enc)) + enc + b"\x00"
            else:
                enc = s.encode("utf-16-le")
                blob += _enc_len16(len(s)) + enc + b"\x00\x00"
        while len(blob) % 4:
            blob += b"\x00"
        n = len(self.strings)
        str_start = 28 + 4 * n              # no styles
        body = struct.pack("<IIIII", n, 0, flags, str_start, 0)
        body += struct.pack(f"<{n}I", *offs)
        body += bytes(blob)
        size = 8 + len(body)
        return struct.pack("<HHI", RES_STRING_POOL_TYPE, 28, size) + body


# ---- element / attribute views over raw chunk bytes -----------------------

class Chunk:
    def __init__(self, typ, raw):
        self.typ = typ
        self.raw = bytearray(raw)

    # -- start-element helpers (typ == RES_XML_START_ELEMENT) --
    @property
    def name_ref(self):
        return struct.unpack_from("<I", self.raw, 20)[0]

    @property
    def attr_count(self):
        return struct.unpack_from("<H", self.raw, 28)[0]

    def _attr_off(self, i):
        attr_start = struct.unpack_from("<H", self.raw, 24)[0]
        return 16 + attr_start + i * 20

    def attrs(self):
        """Yield (index, ns_ref, name_ref, rawValue_ref, dataType, data)."""
        for i in range(self.attr_count):
            o = self._attr_off(i)
            ns, name, raw = struct.unpack_from("<III", self.raw, o)
            dtype = self.raw[o + 15]
            data = struct.unpack_from("<I", self.raw, o + 16)[0]
            yield i, ns, name, raw, dtype, data

    def set_attr_string(self, i, str_index):
        o = self._attr_off(i)
        struct.pack_into("<I", self.raw, o + 8, str_index)    # rawValue
        self.raw[o + 15] = TYPE_STRING                        # dataType
        struct.pack_into("<I", self.raw, o + 16, str_index)   # data


class Manifest:
    def __init__(self, data: bytes):
        self.orig = bytes(data)
        typ, hsize, total = struct.unpack_from("<HHI", data, 0)
        assert typ == RES_XML_TYPE, "not a binary XML file"
        self.pool, p = StringPool.parse(data, 8)
        self.chunks: list[Chunk] = []          # everything AFTER the string pool
        while p < len(data):
            ctyp, chsize, csize = struct.unpack_from("<HHI", data, p)
            self.chunks.append(Chunk(ctyp, data[p:p + csize]))
            p += csize

    # -- string table ops --
    def intern(self, s: str) -> int:
        if s in self.pool.strings:
            return self.pool.strings.index(s)
        self.pool.strings.append(s)
        return len(self.pool.strings) - 1

    def rename_string(self, index: int, value: str):
        self.pool.strings[index] = value

    def s(self, ref: int) -> str | None:
        if ref == NO_ENTRY or ref >= len(self.pool.strings):
            return None
        return self.pool.strings[ref]

    # -- tree walk --
    def elements(self):
        """Yield (depth, Chunk) for start elements, with the enclosing depth."""
        depth = 0
        for ch in self.chunks:
            if ch.typ == RES_XML_START_ELEMENT:
                yield depth, ch
                depth += 1
            elif ch.typ == RES_XML_END_ELEMENT:
                depth -= 1

    def tag(self, ch: Chunk) -> str | None:
        return self.s(ch.name_ref) if ch.typ == RES_XML_START_ELEMENT else None

    def attr_value_ref(self, ch: Chunk, attr_name: str):
        """Return (attr_index, value_string) for the named string attribute, or
        (None, None)."""
        for i, ns, name, raw, dtype, data in ch.attrs():
            if self.s(name) == attr_name and dtype == TYPE_STRING:
                return i, self.s(data)
        return None, None

    def serialize(self) -> bytes:
        body = self.pool.serialize()
        for ch in self.chunks:
            body += bytes(ch.raw)
        return struct.pack("<HHI", RES_XML_TYPE, 8, 8 + len(body)) + bytes(body)

    # -- high-level edits ---------------------------------------------------
    def set_package(self, new_pkg: str) -> bool:
        """Repoint the <manifest package="…"> value (and any split's) to new_pkg.
        Repoints (vs in-place edit) so a string index shared with something else
        can never be disturbed."""
        for _d, ch in self.elements():
            if self.tag(ch) == "manifest":
                i, _v = self.attr_value_ref(ch, "package")
                if i is not None:
                    ch.set_attr_string(i, self.intern(new_pkg))
                    return True
        return False

    def rename_embedded(self, old_pkg: str, new_pkg: str, tag: str, *, log=print):
        """Make provider authorities + custom permission names unique to the clone
        so the second install doesn't collide. Authorities/permissions that embed
        the old package get the prefix swapped; foreign authorities (e.g. a shared
        plugin's) get the clone tag appended. Component class names are untouched."""
        ren = {}                                # string index -> new value
        for _d, ch in self.elements():
            t = self.tag(ch)
            for i, ns, name, raw, dtype, data in ch.attrs():
                if dtype != TYPE_STRING:
                    continue
                an, val = self.s(name), self.s(data)
                if val is None:
                    continue
                if an == "authorities":
                    new = (new_pkg + val[len(old_pkg):]) if val.startswith(old_pkg) \
                        else f"{val}.{tag}"
                    if new != val:
                        ren[data] = new
                elif an == "name" and t in ("permission", "uses-permission"):
                    if val.startswith(old_pkg):
                        ren[data] = new_pkg + val[len(old_pkg):]
                elif an == "permission":            # android:permission on a component
                    if val.startswith(old_pkg):
                        ren[data] = new_pkg + val[len(old_pkg):]
        for idx, new in ren.items():
            self.rename_string(idx, new)
        if ren:
            log(f"[axml] renamed {len(ren)} authority/permission string(s)")
        return len(ren)

    def set_app_label(self, text: str) -> bool:
        for _d, ch in self.elements():
            if self.tag(ch) == "application":
                for i, ns, name, raw, dtype, data in ch.attrs():
                    if self.s(name) == "label":
                        ch.set_attr_string(i, self.intern(text))
                        return True
        return False

    def strip_components(self, name_patterns, perm_names, *, log=print) -> int:
        """Drop whole element subtrees whose android:name matches a pattern
        (substring), plus uses-permission whose name is in perm_names. Balanced
        start/end removal keeps the tree well-formed; orphaned strings are left in
        the pool (their indices stay valid)."""
        def should_remove(ch):
            t = self.tag(ch)
            if t in ("provider", "service", "activity", "activity-alias",
                     "receiver", "meta-data"):
                _i, val = self.attr_value_ref(ch, "name")
                if val and any(p in val for p in name_patterns):
                    return True
            if t == "uses-permission":
                _i, val = self.attr_value_ref(ch, "name")
                if val and val in perm_names:
                    return True
            return False

        out, removing_depth, depth, removed = [], None, 0, 0
        for ch in self.chunks:
            if ch.typ == RES_XML_START_ELEMENT:
                if removing_depth is None and should_remove(ch):
                    removing_depth = depth
                    removed += 1
                if removing_depth is None:
                    out.append(ch)
                depth += 1
            elif ch.typ == RES_XML_END_ELEMENT:
                depth -= 1
                if removing_depth is not None:
                    if depth == removing_depth:
                        removing_depth = None       # drop this closing tag too
                else:
                    out.append(ch)
            else:
                if removing_depth is None:
                    out.append(ch)
        self.chunks = out
        log(f"[axml] stripped {removed} component(s)/permission(s)")
        return removed


# ---- store / ad components to remove on a "full strip" --------------------
# Substring match on a component's android:name. Tuned to kill ad, billing,
# Play-store, Play-games and analytics-upload pieces while leaving androidx /
# Unity-core / Firebase-config / gms-common scaffolding (whose removal most
# often breaks launch). Easy to widen/narrow if a device test misbehaves.
STORE_NAME_PATTERNS = (
    "com.google.android.gms.ads", "com.google.android.gms.games",
    "com.google.android.gms.auth", "com.google.android.gms.measurement",
    "com.google.android.gms.tagmanager",
    "com.android.billingclient", "com.android.vending.billing",
    "com.ironsource", "com.unity3d.services.ads", "com.unity3d.ironsourceads",
    "com.unity3d.services.store", "com.facebook.ads", "AudienceNetwork",
    "MobileAdsInitProvider", "mobileadsinitprovider",
    "playgamesinitprovider", "PlayGames",
    "IronsourceLifecycle", "LevelPlay",
    "com.google.android.datatransport",
    "AdsSdkInitializer", "AdActivity", "AdService",
)
STORE_PERMISSIONS = frozenset({
    "com.android.vending.BILLING",
    "com.android.vending.CHECK_LICENSE",
    "com.google.android.gms.permission.AD_ID",
    "android.permission.ACCESS_ADSERVICES_AD_ID",
    "android.permission.ACCESS_ADSERVICES_ATTRIBUTION",
    "android.permission.ACCESS_ADSERVICES_TOPICS",
})


def clone_and_strip(data: bytes, *, new_pkg: str, old_pkg: str = "com.nitrome.leapday",
                    label: str | None = None, clone_tag: str = "mod",
                    strip: bool = True, log=print) -> bytes:
    """Rewrite a binary AndroidManifest.xml: rename the package (so it installs
    next to the original), uniquify provider authorities / custom permissions,
    optionally set a distinct app label, and on `strip` remove ad/billing/Play
    components. Pass split manifests through too (they only carry `package`)."""
    m = Manifest(data)
    if not m.set_package(new_pkg):
        log("[axml] WARNING: no <manifest package> attribute found")
    m.rename_embedded(old_pkg, new_pkg, clone_tag, log=log)
    if label:
        if not m.set_app_label(label):
            log("[axml] note: no <application android:label> to relabel")
    if strip:
        m.strip_components(STORE_NAME_PATTERNS, STORE_PERMISSIONS, log=log)
    return m.serialize()
