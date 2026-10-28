"""PLC Digital Twin - Twin Core.

PLC実機と10ms周期でDMブロックを交換し、仮想I/O・挙動シミュレーション・
ブラウザ可視化(WebSocket)を1プロセス(+ループ専用プロセス)で提供する。
"""
__version__ = "0.3.0"

# PyYAML が入っていない環境 (pip が使えない / ビルドできない) では同梱の純 Python 版を使う
try:
    import yaml as _yaml  # noqa: F401
except ImportError:  # pragma: no cover
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "_vendor"))
    import yaml as _yaml  # noqa: F401
