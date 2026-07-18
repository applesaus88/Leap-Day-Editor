"""
bundle.py — read / edit / repack Leap Day's data.unity3d (UnityFS bundle).

Leap Day stores its levels as plain-text "chunk" TextAssets inside data.unity3d,
plus a master index TextAsset named `levels`. This module gives the rest of the
studio a small, dependency-light API over those TextAssets:

    b = Bundle(path_to_data_unity3d)
    b.list_text_assets()                 -> [names]
    b.get_text("s9_jk_blob_01")          -> str
    b.set_text("s9_jk_blob_01", new_str) -> marks dirty
    b.add_text("my_level_01", xml)       -> clones an existing TextAsset slot
    b.save(out_path)                     -> repacks (LZ4) to disk if dirty

Only TextAssets are touched here; serialized MonoBehaviour fields are handled
later (Phase 3) via generated type trees.
"""

from __future__ import annotations

import UnityPy


class Bundle:
    def __init__(self, path: str):
        self.path = path
        self.env = UnityPy.load(path)
        self._dirty = False
        # name -> reader object (the parsed TextAsset)
        self._text_objs: dict[str, object] = {}
        self._index()

    def _index(self) -> None:
        # Some chunk TextAssets exist as MULTIPLE copies in the bundle (separate
        # serialized sub-files). The game may load any copy, so we must track
        # and edit ALL of them — keep a list per name, not a single object.
        self._text_objs.clear()
        for obj in self.env.objects:
            if obj.type.name != "TextAsset":
                continue
            data = obj.read()
            name = getattr(data, "m_Name", None)
            if name:
                self._text_objs.setdefault(name, []).append(data)

    # ---- read -------------------------------------------------------------
    def list_text_assets(self) -> list[str]:
        return sorted(self._text_objs.keys())

    def has_text(self, name: str) -> bool:
        return name in self._text_objs

    def get_text(self, name: str) -> str:
        return self._text_objs[name][0].m_Script

    def copies(self, name: str) -> int:
        return len(self._text_objs.get(name, []))

    # ---- write ------------------------------------------------------------
    def set_text(self, name: str, content: str) -> None:
        for data in self._text_objs[name]:      # edit EVERY copy
            data.m_Script = content
            data.save()
        self._dirty = True

    # NOTE: UnityPy 1.25 exposes no add/clone API on the serialized file, so we
    # cannot mint brand-new TextAsset objects. Custom levels are injected by
    # OVERWRITING existing chunk TextAssets (see core/modbuild.py) — their pool
    # placement is driven by the <difficulty> value the game parses at runtime,
    # so overwriting is a complete injection path with no type trees required.
    # Truly adding new named chunks (growing chunkFiles[]) is a later, type-tree
    # phase.

    # ---- persist ----------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        """Flag the bundle for repack after an out-of-band edit to self.env
        (e.g. a typetree override applied via core/typetree.py)."""
        self._dirty = True

    def save(self, out_path: str, packer: str = "lz4") -> None:
        data = self.env.file.save(packer=packer)
        with open(out_path, "wb") as fh:
            fh.write(data)
