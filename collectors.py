r"""本机采集层 · 权限确认后才真正生效

设计原则（与 notifier.py 一致）：
  1) 只读，不改用户任何文件、不注入进程、不改系统设置。
  2) **未授权时一律返回空**，并在 capability 里说明为什么没有数据。
  3) 每个源自报状态：ready / granted / denied / unavailable，**不伪造数据**。
  4) 采集到的原始内容只落本机（%APPDATA%\Muliao\），可一键焚毁。

覆盖范围（用户要求「能访问他电脑的每个角落」）：
  · 进程列表（含命令行）        · 窗口标题（前台 + 全部顶层窗口）
  · 系统通知                    · 浏览器历史（Chrome / Edge）
  · AI 工具对话日志（Claude Code / Codex）
  · 用户目录文件清单（桌面 / 文档 / 下载）
  · 系统信息（主机 / 用户 / 磁盘 / 开机时长）
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any

import notifier          # 复用已验证的通知抓取
import permissions       # 权限闸门


# ============ 系统信息 ============
def system_info() -> dict:
    """主机、用户、系统版本、磁盘、开机时长。
    含主机名/用户名/管理员状态，属个人可识别信息，**同样受权限闸门约束**。"""
    if not permissions.is_granted("system"):
        return {"granted": False, "hostname": None, "user": None,
                "note": "未授权系统信息访问"}
    out: dict[str, Any] = {"granted": True, "platform": sys.platform,
                           "python": sys.version.split()[0]}
    try:
        import socket
        out["hostname"] = socket.gethostname()
        out["user"] = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            out["os"] = os.environ.get("OS", "Windows")
            import ctypes
            out["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        else:
            import platform
            out["os"] = " ".join(platform.platform().split()[:2])
            out["is_admin"] = os.geteuid() == 0
    except Exception:
        pass
    try:
        import psutil
        out["boot_time"] = int(psutil.boot_time())
        out["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        out["cpu_count"] = psutil.cpu_count()
        out["mem_gb"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        out["mem_avail_gb"] = round(psutil.virtual_memory().available / 1024 ** 3, 1)
        # 注意：disk_partitions() 返回的 sdiskpart 只有 device/mountpoint/fstype/opts，
        # **没有 total/free**——必须再对每个挂载点调 disk_usage() 才能拿到容量。
        # （早先版本直接读 d.total 会抛 AttributeError 并被外层 except 吞掉，导致 disks 恒为空）
        disks = []
        for d in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(d.mountpoint)
                disks.append({"mount": d.device, "fstype": d.fstype,
                              "total_gb": round(u.total / 1024 ** 3, 1),
                              "free_gb": round(u.free / 1024 ** 3, 1),
                              "used_pct": round(u.percent, 1)})
            except Exception:
                continue
        out["disks"] = disks
    except Exception as e:
        out["psutil_err"] = str(e)
    return out


# ============ 进程列表 ============
def processes(limit: int = 200, with_cmdline: bool = True) -> dict:
    """所有运行中进程：pid / 名称 / 内存 / 命令行，按内存降序取前 limit。

    注意（曾经的 bug，勿回退）：必须**先采全部、再排序、最后截断**。
    早先版本在遍历中 `len(items) >= limit` 就 break，导致只看到 psutil 先返回的
    前 N 个进程（恰好都是低内存的系统进程），真正的内存大户永远采不到，
    排序结果完全失真（最高才 43MB，实际机器上有 611MB 的 MsMpEng）。
    另：cpu_percent 首次调用无采样基准，返回值无意义（曾出现 1152.2 这种），
    故不再返回 cpu，避免给模型和用户假数据。
    """
    if not permissions.is_granted("processes"):
        return {"granted": False, "items": [], "count": 0,
                "note": "未授权进程访问"}
    try:
        import psutil
    except ImportError:
        return {"granted": True, "items": [], "count": 0, "note": "psutil 不可用"}
    items = []
    total = 0
    for p in psutil.process_iter(["pid", "name", "username", "memory_info", "create_time"]):
        total += 1
        try:
            info = p.info
            mi = info.get("memory_info")
            rec = {
                "pid": info.get("pid"),
                "name": info.get("name") or "",
                "user": info.get("username") or "",
                "mem_mb": round((mi.rss if mi else 0) / 1024 ** 2, 1),
                "started": int(info.get("create_time") or 0),
            }
            if with_cmdline:
                try:
                    rec["cmdline"] = " ".join(p.cmdline() or [])[:400]
                except Exception:
                    rec["cmdline"] = ""
            items.append(rec)
        except Exception:
            continue
    # 先全采 → 再排序 → 最后截断
    items.sort(key=lambda r: r["mem_mb"], reverse=True)
    items = items[:limit]
    return {"granted": True, "items": items, "count": len(items),
            "total_procs": total,
            "note": "按内存(RSS)降序；cpu 占用需两次采样才准确，故不返回"}


# ============ 窗口标题 ============
def windows(limit: int = 120) -> dict:
    """所有顶层可见窗口标题 + 前台窗口。Windows 用 pywin32；其他平台降级。"""
    if not permissions.is_granted("windows"):
        return {"granted": False, "items": [], "count": 0, "note": "未授权窗口访问"}
    if sys.platform != "win32":
        return {"granted": True, "items": [], "count": 0,
                "note": "当前平台未实现窗口枚举（Windows 专属，需 pywin32）"}
    try:
        import win32gui
        import win32process
    except ImportError:
        return {"granted": True, "items": [], "count": 0,
                "note": "pywin32 不可用（pip install pywin32）"}
    items = []
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        fg = 0

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return
            cls = win32gui.GetClassName(hwnd)
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = ""
            try:
                import psutil
                proc = psutil.Process(pid).name()
            except Exception:
                pass
            items.append({"hwnd": hwnd, "title": title[:200], "class": cls[:60],
                          "pid": pid, "process": proc, "foreground": hwnd == fg})
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception as e:
        return {"granted": True, "items": items[:limit], "count": len(items[:limit]),
                "note": f"枚举中断：{e}"}
    items.sort(key=lambda w: (not w["foreground"], w["process"]))
    return {"granted": True, "items": items[:limit], "count": len(items[:limit]),
            "foreground": next((w["title"] for w in items if w["foreground"]), None)}


# ============ 浏览器历史（复制避锁，思路源自 echo/src/collectors.ts） ============
_BROWSER_DB = {
    "chrome": ("Google/Chrome/User Data/{profile}/History",),
    "edge": ("Microsoft/Edge/User Data/{profile}/History",),
}


def browser_history(limit: int = 300, profiles: tuple = ("Default", "Profile 1")) -> dict:
    """Chrome / Edge 访问历史：标题 + URL + 访问时间。"""
    if not permissions.is_granted("browser"):
        return {"granted": False, "items": [], "count": 0, "note": "未授权浏览器历史访问"}
    la = os.environ.get("LOCALAPPDATA") or ""
    if not la:
        return {"granted": True, "items": [], "count": 0, "note": "无 LOCALAPPDATA（非 Windows？）"}
    items = []
    scanned = 0
    for browser, (rel,) in _BROWSER_DB.items():
        for prof in profiles:
            src = os.path.join(la, rel.format(profile=prof))
            if not os.path.isfile(src):
                continue
            scanned += 1
            tmp = os.path.join(tempfile.gettempdir(), f"muliao_hist_{browser}_{abs(hash(prof))}.sqlite")
            try:
                # 关键：先复制再只读打开，避开浏览器对 History 的独占锁
                shutil.copyfile(src, tmp)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=3.0)
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT url, title, last_visit_time FROM urls "
                    "WHERE last_visit_time > 0 ORDER BY last_visit_time DESC LIMIT ?",
                    (limit // max(scanned, 1) + 40,),
                ).fetchall()
                con.close()
                for r in rows:
                    # Chrome 时间戳：自 1601-01-01 起的微秒
                    ts = int(r["last_visit_time"] or 0)
                    ts = round(ts / 1_000_000 - 11644473600, 1) if ts > 0 else 0.0
                    items.append({
                        "source": browser, "profile": prof, "url": (r["url"] or "")[:300],
                        "title": (r["title"] or "(无标题)")[:160], "ts": ts,
                    })
            except Exception:
                continue
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"granted": True, "items": items[:limit], "count": len(items[:limit]),
            "dbs_scanned": scanned,
            "note": None if scanned else "未找到 Chrome/Edge 历史库"}


# ============ AI 工具对话日志 ============
_AI_LOG_DIRS = {
    "claude": (".claude/projects",),
    "codex": (".codex/sessions",),
}


def _walk_jsonl(root: str, limit_files: int = 200) -> list:
    if not os.path.isdir(root):
        return []
    out = []
    stack = [root]
    while stack and len(out) < limit_files:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append(e.path)
                elif e.name.endswith(".jsonl"):
                    out.append((e.path, e.stat().st_mtime))
            except OSError:
                continue
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit_files]]


def ai_logs(limit: int = 200, max_files: int = 60) -> dict:
    """Claude Code / Codex 的对话日志（jsonl）：谁在什么时候问了什么。"""
    if not permissions.is_granted("ai_logs"):
        return {"granted": False, "items": [], "count": 0, "note": "未授权 AI 日志访问"}
    home = os.path.expanduser("~")
    items = []
    files_seen = 0
    for tool, (rel,) in _AI_LOG_DIRS.items():
        root = os.path.join(home, rel)
        files = _walk_jsonl(root, max_files)
        files_seen += len(files)
        for fp in files[:max_files]:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json_loads(line)
                        except Exception:
                            continue
                        text = _extract_msg(o)
                        if not text:
                            continue
                        role, body = text
                        items.append({
                            "tool": tool, "role": role, "text": body[:500],
                            "session": os.path.basename(fp)[:40],
                            "ts": _parse_ts(o.get("timestamp")),
                        })
                        if len(items) >= limit * 4:
                            raise StopIteration
            except StopIteration:
                break
            except Exception:
                continue
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return {"granted": True, "items": items[:limit], "count": len(items[:limit]),
            "files_seen": files_seen,
            "tools_found": [t for t, (r,) in _AI_LOG_DIRS.items()
                            if os.path.isdir(os.path.join(home, r))]}


def json_loads(s: str):
    import json as _json
    return _json.loads(s)


def _parse_ts(v) -> float:
    if not v:
        return 0.0
    try:
        if isinstance(v, (int, float)):
            return float(v)
        from datetime import datetime
        s = str(v).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _extract_msg(o: dict):
    """从 Claude/Codex 的 jsonl 记录里抽出 (role, text)。"""
    typ = o.get("type")
    msg = o.get("message") or {}
    if typ == "user" and isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str) and c.strip():
            return "user", c.strip()
        if isinstance(c, list):
            t = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            if t.strip():
                return "user", t.strip()
    if typ == "assistant" and isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):
            t = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            if t.strip():
                return "assistant", t.strip()
        elif isinstance(c, str) and c.strip():
            return "assistant", c.strip()
    # Codex event_msg 形态
    p = o.get("payload") or {}
    if o.get("type") == "event_msg" and p.get("type") == "user_message" and isinstance(p.get("message"), str):
        return "user", p["message"].strip()
    if o.get("type") == "response_item" and p.get("type") == "message" and isinstance(p.get("content"), list):
        t = " ".join(x.get("text", "") for x in p["content"] if isinstance(x, dict))
        if t.strip():
            return p.get("role") or "assistant", t.strip()
    return None


# ============ 用户目录文件清单 ============
_SCAN_DIRS = {
    "桌面": "Desktop",
    "文档": "Documents",
    "下载": "Downloads",
}


def user_files(limit: int = 400, depth: int = 3) -> dict:
    """桌面 / 文档 / 下载 的文件清单（名称、大小、修改时间）——不读内容。"""
    if not permissions.is_granted("files"):
        return {"granted": False, "items": [], "count": 0, "note": "未授权文件访问"}
    home = os.path.expanduser("~")
    items = []
    for label, sub in _SCAN_DIRS.items():
        root = os.path.join(home, sub)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                items.append({
                    "area": label, "name": fn[:160],
                    "path": fp.replace(home, "~")[:300],
                    "size_kb": round(st.st_size / 1024, 1),
                    "mtime": round(st.st_mtime, 1),
                    "ext": os.path.splitext(fn)[1].lower()[:12],
                })
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"granted": True, "items": items[:limit], "count": len(items[:limit]),
            "areas_found": [lab for lab, sub in _SCAN_DIRS.items()
                            if os.path.isdir(os.path.join(home, sub))],
            "note": "只列文件名与元数据，不读取文件内容"}


# ============ 通知（复用 notifier） ============
def notifications(limit: int = 60, since_id: str = "") -> dict:
    """系统通知 + 微信归档。授权后才抓。"""
    if not permissions.is_granted("notifications"):
        return {"granted": False, "items": [], "count": 0, "platforms_used": [],
                "note": "未授权通知访问"}
    data = notifier.fetch_all(since_id=since_id, limit=limit, all_platforms=True)
    return {"granted": True, **data}


# ============ 总览：一次拿全部源的可用性 ============
def sources() -> list:
    """所有采集源的可用性 + 授权状态（供权限页展示）。"""
    caps = {c["platform"]: c for c in notifier.capabilities()}
    win = caps.get(notifier.PLATFORM) or caps.get("windows") or {}
    return [
        {
            "id": "notifications", "name": "系统通知",
            "desc": "全系统 toast / 通知中心（含微信归档）",
            "granted": permissions.is_granted("notifications"),
            "available": bool(win.get("ready")),
            "detail": win.get("method") or "只读通知库",
            "note": win.get("note"),
            "sensitive": True,
        },
        {
            "id": "processes", "name": "进程列表",
            "desc": "所有运行中进程：名称、内存、CPU、完整命令行",
            "granted": permissions.is_granted("processes"),
            "available": _has("psutil"),
            "detail": "psutil.process_iter",
            "note": None if _has("psutil") else "需 pip install psutil",
            "sensitive": True,
        },
        {
            "id": "windows", "name": "窗口标题",
            "desc": "所有顶层可见窗口的标题、类名、所属进程、前台窗口",
            "granted": permissions.is_granted("windows"),
            "available": sys.platform == "win32" and _has("win32gui"),
            "detail": "win32gui.EnumWindows",
            "note": None if (sys.platform == "win32" and _has("win32gui"))
                    else "需 Windows + pywin32",
            "sensitive": True,
        },
        {
            "id": "browser", "name": "浏览器历史",
            "desc": "Chrome / Edge 访问过的网址与标题（复制避锁后只读）",
            "granted": permissions.is_granted("browser"),
            "available": _browser_exists(),
            "detail": "History sqlite（先复制再只读打开）",
            "note": None if _browser_exists() else "未找到 Chrome/Edge 历史库",
            "sensitive": True,
        },
        {
            "id": "ai_logs", "name": "AI 工具对话日志",
            "desc": "Claude Code / Codex 的历史对话（谁问了什么）",
            "granted": permissions.is_granted("ai_logs"),
            "available": _ai_dirs_exist(),
            "detail": "~/.claude/projects、~/.codex/sessions 下的 jsonl",
            "note": None if _ai_dirs_exist() else "未找到 AI 工具日志目录",
            "sensitive": True,
        },
        {
            "id": "files", "name": "文件清单",
            "desc": "桌面 / 文档 / 下载 的文件名、大小、修改时间（不读内容）",
            "granted": permissions.is_granted("files"),
            "available": True,
            "detail": "os.walk（深度 3 层，只取元数据）",
            "note": "只列文件名与元数据，不读取文件内容",
            "sensitive": True,
        },
        {
            "id": "system", "name": "系统信息",
            "desc": "主机名、用户、系统版本、磁盘、开机时长、是否管理员",
            "granted": permissions.is_granted("system"),
            "available": True,
            "detail": "os / socket / psutil",
            "note": None, "sensitive": False,
        },
    ]


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _browser_exists() -> bool:
    la = os.environ.get("LOCALAPPDATA") or ""
    if not la:
        return False
    for browser, (rel,) in _BROWSER_DB.items():
        for prof in ("Default", "Profile 1"):
            if os.path.isfile(os.path.join(la, rel.format(profile=prof))):
                return True
    return False


def _ai_dirs_exist() -> bool:
    home = os.path.expanduser("~")
    return any(os.path.isdir(os.path.join(home, rel)) for (rel,) in _AI_LOG_DIRS.values())


# 采集器注册表：id → 采集函数
COLLECTORS = {
    "system": lambda **kw: system_info(),
    "processes": lambda **kw: processes(**kw),
    "windows": lambda **kw: windows(**kw),
    "browser": lambda **kw: browser_history(**kw),
    "ai_logs": lambda **kw: ai_logs(**kw),
    "files": lambda **kw: user_files(**kw),
    "notifications": lambda **kw: notifications(**kw),
}
