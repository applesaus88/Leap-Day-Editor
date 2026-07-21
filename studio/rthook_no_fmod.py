# PyInstaller runtime hook — runs before any app code.
#
# UnityPy's export/__init__.py eagerly imports AudioClipConverter, whose only
# module-level dependency is `import fmod_toolkit` (the proprietary FMOD engine).
# The editor never decodes audio — fmod_toolkit is used solely inside functions we
# never call. So we register an empty stub module for it up front: the import
# succeeds, texture decoding works, and no FMOD binary is bundled or shipped.
import sys
import types

if "fmod_toolkit" not in sys.modules:
    sys.modules["fmod_toolkit"] = types.ModuleType("fmod_toolkit")
