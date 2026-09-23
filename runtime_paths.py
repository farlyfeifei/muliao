"""幕僚运行时路径。

开发实例可通过 ``MULIAO_DATA_DIR`` 隔离授权、日志与未来数据库；正式版未设置时
继续使用 ``%APPDATA%\\Muliao``，保持现有安装兼容。
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    configured = os.environ.get("MULIAO_DATA_DIR")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return (Path(appdata) / "Muliao").resolve()


def config_path() -> Path:
    configured = os.environ.get("MULIAO_CONFIG")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    return data_dir() / "config.json"
