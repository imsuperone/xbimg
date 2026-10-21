# -*- coding: utf-8 -*-
"""版本号唯一真相源读取器（驻 core/，根 metadata.yaml 仍是唯一手写处）。"""
import os

_CACHE = ""


def get_version(plugin_base: str = "") -> str:
    """动态读取根目录 metadata.yaml 的 version 字段，绝不硬编码。带缓存。"""
    global _CACHE
    if _CACHE:
        return _CACHE
    base = plugin_base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    meta = os.path.join(base, "metadata.yaml")
    if os.path.isfile(meta):
        try:
            with open(meta, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("version:"):
                        v = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if v:
                            _CACHE = v
                            return v
        except Exception:
            pass
    return "1.3.37"
