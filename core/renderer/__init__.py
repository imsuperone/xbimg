# -*- coding: utf-8 -*-
"""渲染引擎包（由原 core/renderer.py 拆分）。

分层：shared(基础) -> fonts(字体) -> emoji(表情) -> theme(配色) / layout(排版)
      -> painter(绘制) -> engine(编排)。
为兼容拆分前的扁平模块用法（`from core.renderer import X`、`core.renderer.X`、
monkeypatch），本包经 __getattr__ 把未显式定义的名字转发到子模块；转发发生在
访问时刻，可变全局（字体生效集、_FONT_EPOCH 等）读到的一定是当前值。
跨模块打桩请落到属主模块：core.renderer.shared / .fonts / .emoji / ...。
"""
from . import shared, fonts, emoji, theme, layout, painter, engine

_SUBMODULES = (shared, fonts, emoji, theme, layout, painter, engine)


def __getattr__(name: str):
    for _mod in _SUBMODULES:
        try:
            return getattr(_mod, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    names = set(globals())
    for _mod in _SUBMODULES:
        names.update(vars(_mod))
    return sorted(names)
