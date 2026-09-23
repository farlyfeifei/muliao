"""权限同意闸门 · 用户授权后采集才真正生效

设计原则：
  1) **默认全关**：未授权时 collectors 一律返回空，绝不偷采。
  2) 授权记录落在 %APPDATA%\\Muliao\\consent.json，含时间戳与逐项开关。
  3) 用户可逐项授权，也可一键全开 / 一键撤销（焚毁）。
  4) 授权是「用户显式同意」，不是「进程权限」——两者分开：
     · 用户同意 = 本模块记录的 grant（隐私合规层面）
     · 系统能力 = 各采集源实际的可用性与 OS 权限（collectors 自报 available）
     两者都满足，采集才真正产出数据。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

from runtime_paths import data_dir

# 与 server.py / collectors.py 一致的存储目录
_STORE_DIR = str(data_dir())
_CONSENT = os.path.join(_STORE_DIR, "consent.json")
_lock = threading.RLock()

# 七类只读数据源；保留稳定标识，供 collectors、machine_tools 与蜂群权限快照使用。
DATA_SCOPES = ["notifications", "processes", "windows", "browser", "ai_logs", "files", "system"]
# 执行/设备能力独立于数据采集。能力必须逐项显式开启，永不随“全部数据”授权打开。
CAPABILITY_SCOPES = ["voice_control"]
ALL_SCOPES = DATA_SCOPES + CAPABILITY_SCOPES

# 默认状态：全部未授权
_DEFAULT: dict[str, Any] = {
    "agreed": False,          # 用户是否看过并同意了总隐私条款
    "agreed_at": None,
    "version": None,          # 同意时的条款版本号
    "scopes": {s: False for s in ALL_SCOPES},
    "granted_at": {s: None for s in ALL_SCOPES},
}

CONSENT_VERSION = "1"


def _load() -> dict:
    with _lock:
        try:
            if os.path.isfile(_CONSENT):
                with open(_CONSENT, "r", encoding="utf-8") as f:
                    d = json.load(f)
                # 补齐新增 scope，避免旧 consent 文件缺键
                base = json.loads(json.dumps(_DEFAULT))
                base.update({k: v for k, v in d.items() if k in ("agreed", "agreed_at", "version")})
                base["scopes"].update(d.get("scopes", {}))
                base["granted_at"].update(d.get("granted_at", {}))
                return base
        except Exception:
            pass
        return json.loads(json.dumps(_DEFAULT))


def _save(d: dict) -> None:
    """原子保存授权状态；失败必须抛出，API 不能虚报成功。"""
    with _lock:
        os.makedirs(_STORE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="consent-", suffix=".tmp", dir=_STORE_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _CONSENT)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def status() -> dict:
    """当前授权状态（供权限页展示）。"""
    d = _load()
    return {
        "agreed": d["agreed"],
        "agreed_at": d["agreed_at"],
        "version": d["version"],
        "scopes": dict(d["scopes"]),
        "granted_at": dict(d["granted_at"]),
        "consent_version": CONSENT_VERSION,
    }


def is_granted(scope: str) -> bool:
    """采集前的闸门：未同意总条款 或 该 scope 未授权 → False。"""
    d = _load()
    return bool(d["agreed"]) and bool(d["scopes"].get(scope, False))


def agree(scopes: list | None = None, agree_all: bool = False) -> dict:
    """用户同意隐私条款，并把当前选择保存为完整授权集合。

    scopes=None 且 agree_all=False 时表示只同意条款、不授权采集。
    后续再次提交会同步撤销未勾选项，避免权限管理页出现“看似关闭、实际仍授权”。
    """
    with _lock:
        d = _load()
        now = time.time()
        d["agreed"] = True
        d["agreed_at"] = now
        d["version"] = CONSENT_VERSION
        targets = set(DATA_SCOPES if agree_all else (scopes or []))
        # “全选”只代表七类只读数据；已有能力授权保持不变，不可偷开或误关麦克风。
        for s in DATA_SCOPES:
            on = s in targets
            d["scopes"][s] = on
            d["granted_at"][s] = now if on else None
        _save(d)
        return status()


def set_scope(scope: str, on: bool) -> dict:
    """单项开关。"""
    with _lock:
        d = _load()
        if scope in d["scopes"]:
            d["scopes"][scope] = bool(on)
            d["granted_at"][scope] = time.time() if on else None
            # 任何单项授权都隐含同意总条款
            if on and not d["agreed"]:
                d["agreed"] = True
                d["agreed_at"] = time.time()
                d["version"] = CONSENT_VERSION
        _save(d)
        return status()


def revoke_all() -> dict:
    """撤销全部授权（一键焚毁同意记录）。"""
    with _lock:
        d = json.loads(json.dumps(_DEFAULT))
        _save(d)
        return status()


def purge_all() -> dict:
    """撤销授权并删除 consent 文件（彻底焚毁）；删除失败必须上抛。"""
    with _lock:
        if os.path.exists(_CONSENT):
            os.remove(_CONSENT)
    return json.loads(json.dumps(_DEFAULT))


def granted_scopes() -> list:
    d = _load()
    if not d["agreed"]:
        return []
    return [s for s in ALL_SCOPES if d["scopes"].get(s)]
