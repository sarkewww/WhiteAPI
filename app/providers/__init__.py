"""Provider 注册表"""

from __future__ import annotations

import importlib
from typing import Optional

from app.providers.base import MusicProvider

_REGISTRY: dict[str, type[MusicProvider]] = {}
_INSTANCES: dict[str, MusicProvider] = {}


def register_provider(cls: type[MusicProvider]) -> type[MusicProvider]:
    """注册 provider 类"""
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str, cookies: str = "") -> Optional[MusicProvider]:
    """获取 provider 实例（带缓存）"""
    if name not in _REGISTRY:
        return None
    key = name
    if key not in _INSTANCES:
        _INSTANCES[key] = _REGISTRY[name](cookies=cookies)
    return _INSTANCES[key]


def list_providers() -> list[dict]:
    """列出所有已注册 provider"""
    return [
        {"name": cls.name, "display_name": cls.display_name}
        for cls in _REGISTRY.values()
    ]


def load_builtin_providers() -> None:
    """加载内置 provider"""
    for mod in ("netease", "qq", "spotify", "kugou", "kuwo"):
        try:
            importlib.import_module(f"app.providers.{mod}")
        except ImportError:
            pass
