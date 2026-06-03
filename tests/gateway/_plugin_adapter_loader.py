"""Shared helper for loading platform-plugin ``adapter.py`` modules in tests.

Every platform plugin under ``plugins/platforms/<name>/`` ships its own
``adapter.py``. If two tests independently do::

    sys.path.insert(0, "plugins/platforms/irc")
    from adapter import IRCAdapter

    sys.path.insert(0, "plugins/platforms/teams")
    from adapter import TeamsAdapter

…then whichever collects first in an xdist worker wins
``sys.modules["adapter"]``, and the other raises ``ImportError`` at
collection time. The fallout cascades across unrelated tests sharing that
worker because ``sys.path`` is still polluted.

Use :func:`load_plugin_adapter` instead of ad-hoc ``sys.path`` tricks.
It loads the adapter from an explicit file path under a unique module
name (``plugin_adapter_<plugin_name>``), so it cannot collide with any
other plugin's adapter module.

The ``tests/gateway/conftest.py`` guard rejects the anti-pattern at
collection time so this can't regress when new plugin adapter tests are
added.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_DIR = _REPO_ROOT / "plugins" / "platforms"


def load_plugin_adapter(plugin_name: str) -> ModuleType:
    """Import ``plugins/platforms/<plugin_name>/adapter.py`` in isolation.

    When the plugin directory has an ``__init__.py``, the package is
    loaded first (so relative imports inside ``adapter.py`` work) and
    the adapter module is registered under the unique name
    ``plugin_adapter_<plugin_name>`` in ``sys.modules``.

    When there is no ``__init__.py``, the adapter is loaded as a
    standalone module (legacy behaviour).

    No ``sys.path`` mutation.  Safe to call multiple times — repeat
    calls return the already-loaded module.
    """
    module_name = f"plugin_adapter_{plugin_name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    plugin_dir = _PLUGINS_DIR / plugin_name
    adapter_path = plugin_dir / "adapter.py"
    if not adapter_path.is_file():
        raise FileNotFoundError(
            f"Plugin adapter not found: {adapter_path}. "
            f"Known plugins: {sorted(p.name for p in _PLUGINS_DIR.iterdir() if p.is_dir())}"
        )

    has_pkg = (plugin_dir / "__init__.py").is_file()

    if has_pkg:
        # Load the package so relative imports resolve, then return
        # the adapter submodule from it.
        import importlib

        pkg_name = f"plugins.platforms.{plugin_name}"
        pkg_cached = sys.modules.get(pkg_name)
        if pkg_cached is None:
            pkg_spec = importlib.util.spec_from_file_location(
                pkg_name,
                str(plugin_dir / "__init__.py"),
                submodule_search_locations=[str(plugin_dir)],
            )
            if pkg_spec is None or pkg_spec.loader is None:
                raise ImportError(f"Could not build package spec for {pkg_name}")
            pkg_mod = importlib.util.module_from_spec(pkg_spec)
            sys.modules[pkg_name] = pkg_mod
            pkg_spec.loader.exec_module(pkg_mod)

        # Now load adapter.py as a submodule of the package.
        adapter_submod = f"{pkg_name}.adapter"
        adapter_cached = sys.modules.get(adapter_submod)
        if adapter_cached is not None:
            sys.modules[module_name] = adapter_cached
            return adapter_cached

        spec = importlib.util.spec_from_file_location(
            adapter_submod, adapter_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not build import spec for {adapter_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[adapter_submod] = module
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(adapter_submod, None)
            sys.modules.pop(module_name, None)
            raise
        return module
    else:
        # Legacy: load adapter.py as a standalone module.
        spec = importlib.util.spec_from_file_location(module_name, adapter_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not build import spec for {adapter_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module
