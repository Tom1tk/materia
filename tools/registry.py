import importlib
import logging
import pkgutil
from typing import Optional

logger = logging.getLogger(__name__)

_registry: dict = {}

_SKIP_MODULES = frozenset({"spec", "registry", "builtin", "user_tools", "result", "__init__"})


def register(spec) -> None:
    """Register a ToolSpec. Replaces any existing entry with the same name."""
    if spec.name in _registry:
        logger.info(f"[registry] Replacing tool: {spec.name}")
    _registry[spec.name] = spec


def get(name: str) -> Optional[object]:
    return _registry.get(name)


def all_tools() -> list:
    return list(_registry.values())


def names() -> list[str]:
    return list(_registry.keys())


def discover() -> None:
    """Import every non-core module under tools/ so they run their register() calls."""
    import tools as pkg
    for finder, name, is_pkg in pkgutil.iter_modules(pkg.__path__):
        if name in _SKIP_MODULES:
            continue
        full_name = f"tools.{name}"
        try:
            importlib.import_module(full_name)
            logger.info(f"[registry] Loaded {full_name}")
        except Exception as e:
            logger.error(f"[registry] Failed to load {full_name}: {e}")
