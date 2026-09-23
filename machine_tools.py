"""本机工具层 · 让「授权」真正生效

设计要点：
  1) **只暴露已授权的工具**。未授权的采集源不会出现在 tools 列表里，
     模型连「可以调它」都不知道——这是比运行时拦截更强的隐私保证。
  2) 工具执行时**再过一次权限闸门**（双保险，防止授权被中途撤销后仍被调用）。
  3) 工具定义是**稳定文本**，放在 system 之后作为前缀的一部分。
     ⚠️ 缓存命中率影响：只要已授权工具集不变，前缀就逐字节稳定，命中率不受影响；
        用户中途改授权会使工具集变化 → 该轮缓存 miss（低频事件，可接受）。
  4) 返回值统一截断，避免把整个进程列表灌进上下文。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

import collectors
import permissions

# ---- 工具定义（OpenAI function-calling 格式）----
# 每个工具绑定一个权限 scope；scope 未授权则该工具不出现在列表里。
TOOL_DEFS: dict[str, dict] = {
    "get_machine_snapshot": {
        "scope": "system",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_machine_snapshot",
                "description": "获取这台电脑的概况：主机名、系统、是否管理员、CPU/内存、磁盘余量、开机时长。当需要了解运行环境时调用。",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    },
    "get_foreground_window": {
        "scope": "windows",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_foreground_window",
                "description": "获取用户当前正在看哪个窗口（前台窗口标题与所属进程），以及最近在用的其他窗口。当用户说「我这个」「当前页面」「刚才那个」等指代时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "最多返回几个窗口，默认 8", "default": 8},
                    },
                    "required": [],
                },
            },
        },
    },
    "get_running_processes": {
        "scope": "processes",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_running_processes",
                "description": "列出正在运行的进程（名称、内存占用、完整命令行），按内存降序。当用户问「什么在占内存」「有没有跑某程序」「内存不够了」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回条数，默认 15，按内存降序", "default": 15},
                        "filter": {"type": "string", "description": "只保留名称或命令行包含该关键词的进程"},
                    },
                    "required": [],
                },
            },
        },
    },
    "get_recent_notifications": {
        "scope": "notifications",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_recent_notifications",
                "description": "读取最近的系统通知（来源应用、标题、正文）。当用户问「有什么消息」「刚才那个通知」「有没有报错提醒」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回条数，默认 12", "default": 12},
                    },
                    "required": [],
                },
            },
        },
    },
    "get_browser_history": {
        "scope": "browser",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_browser_history",
                "description": "查询 Chrome/Edge 最近访问过的网页（标题与网址）。当用户问「我刚才看的那个网页」「之前查过什么」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回条数，默认 15", "default": 15},
                    },
                    "required": [],
                },
            },
        },
    },
    "get_recent_files": {
        "scope": "files",
        "spec": {
            "type": "function",
            "function": {
                "name": "get_recent_files",
                "description": "列出桌面/文档/下载里最近改动的文件（名称、大小、时间、所在区域）。只读元数据，不读文件内容。当用户问「我刚下载的那个文件」「桌面上有什么」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回条数，默认 15", "default": 15},
                        "area": {"type": "string", "description": "限定区域：桌面 / 文档 / 下载"},
                    },
                    "required": [],
                },
            },
        },
    },
    "search_ai_logs": {
        "scope": "ai_logs",
        "spec": {
            "type": "function",
            "function": {
                "name": "search_ai_logs",
                "description": "在 Claude Code / Codex 的历史对话里检索。当用户问「我之前让 AI 做过什么」「上次那个方案」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "检索关键词"},
                        "limit": {"type": "integer", "description": "返回条数，默认 8", "default": 8},
                    },
                    "required": [],
                },
            },
        },
    },
}


def available_tool_specs() -> list[dict]:
    """只返回已授权工具的 spec。未授权的工具对模型完全不可见。"""
    out = []
    for name, t in TOOL_DEFS.items():
        if permissions.is_granted(t["scope"]):
            out.append(t["spec"])
    return out


def available_tool_names() -> list:
    return [n for n, t in TOOL_DEFS.items() if permissions.is_granted(t["scope"])]


# ---- 工具执行 ----
def _trim(obj: Any, limit: int = 1500) -> str:
    """序列化工具结果并截断。

    limit 默认 1500 字符是**为缓存命中率**定的：工具结果是全新内容，
    必然 miss，它的体积直接决定这一轮的命中率下限（rate ≈ 前缀/(前缀+新增)）。
    实测一个未裁剪的进程列表可达 2600+ 字符，把整轮拉到 60%。
    """
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"…(已截断，原长 {len(s)})"


# ---- 异常信息脱敏（回灌给上游模型前必须过一遍）----
# 采集层抛出的异常常带完整本机路径，里面有用户名、桌面文件名、浏览器历史库位置、
# AI 日志目录等——这些等于把「没被授权看到的东西」或「授权范围内的隐私细节」
# 泄漏到上游模型的上下文里。所以这里统一抹掉可识别片段，只留下排障需要的类型与短描述。
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern, str], ...] | None = None


def _sensitive_patterns() -> tuple[tuple[re.Pattern, str], ...]:
    global _SENSITIVE_PATTERNS
    if _SENSITIVE_PATTERNS is None:
        pats: list[tuple[str, str]] = []
        home = os.path.expanduser("~")
        cands = {
            home: "~",
            os.environ.get("APPDATA") or "": "%APPDATA%",
            os.environ.get("LOCALAPPDATA") or "": "%LOCALAPPDATA%",
            os.environ.get("USERPROFILE") or "": "~",
            os.environ.get("TEMP") or "": "%TEMP%",
            os.environ.get("TMP") or "": "%TMP%",
        }
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        # 长路径先替换，避免被短片段抢先截断
        for raw, tag in sorted(((k, v) for k, v in cands.items() if k),
                               key=lambda kv: -len(kv[0])):
            pats.append((re.compile(re.escape(raw), re.IGNORECASE), tag))
        # 兜底：Windows 用户目录 / *nix home 的通用形状
        pats.append((re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"']+"), "~"))
        pats.append((re.compile(r"[A-Za-z]:\\+home\\+[^\\\s\"']+"), "~"))
        pats.append((re.compile(r"(?<![\w])/home/[^/\s\"']+"), "~"))
        # 用户名单独出现时（如日志里的 "Administrator"）也抹掉
        if user and len(user) >= 3:
            pats.append((re.compile(r"\b" + re.escape(user) + r"\b", re.IGNORECASE), "<user>"))
        # 浏览器历史库 / AI 日志这类目录名不留线索
        pats.append((re.compile(r"muliao_hist_[A-Za-z0-9_]+\.sqlite"), "<tmp>"))
        _SENSITIVE_PATTERNS = tuple((re_c, tag) for re_c, tag in pats)
    return _SENSITIVE_PATTERNS


def sanitize_error(exc: BaseException, limit: int = 180) -> str:
    """把异常压成一行安全文本：类型名 + 脱敏后的短描述。

    返回的字符串会原样进入上游模型上下文，所以：
      · 不含完整本机路径 / 用户名 / 临时文件位置
      · 长度封顶，避免把一整段 traceback 灌进 prompt
    """
    msg = str(exc).strip()
    for pat, tag in _sensitive_patterns():
        msg = pat.sub(tag, msg)
    # 异常消息里可能夹带换行（sqlite3 的多行错误），压成空格
    msg = " ".join(msg.split())
    if len(msg) > limit:
        msg = msg[:limit] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _as_dict(args: Any) -> dict:
    """模型偶尔把 arguments 给成 list / 字符串 / null，统一收成 dict，后续 .get 才不崩。"""
    if isinstance(args, dict):
        return args
    return {}


def _as_text(v: Any) -> str:
    """把 filter / area / keyword 这类字符串参数安全转 str（模型可能给 None / 数字 / list）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    return ""


def _as_limit(args: dict) -> int | None:
    """从 args 里取 limit，容忍脏值。

    负数必须挡住：limit=-5 传进 collectors 会变成 items[:-5]，
    静默砍掉**内存占用最高**的那几条（正是用户最想看的数据），不报错但结果全错。
    """
    raw = args.get("limit")
    if raw is None or raw == "":
        return None
    try:
        n = float(raw)
        if not math.isfinite(n):
            return None
        v = int(n)
    except (TypeError, ValueError, OverflowError):
        return None
    if v <= 0:
        return None
    return min(v, 500)          # 上限防呆：模型给 10**9 也不至于把整台机器灌进上下文


def execute_tool(name: str, args: dict | None) -> str:
    """执行工具，返回给模型的字符串结果。
    双重闸门：先查该工具是否已授权（防止授权被撤销后仍被调用）。"""
    args = _as_dict(args)
    t = TOOL_DEFS.get(name)
    if not t or not permissions.is_granted(t["scope"]):
        return _trim({"error": "unavailable_tool",
                      "hint": "该工具当前不可用。不要重试；请基于已有信息回答，或告知用户在权限页开启相应能力。"})

    def finish(obj) -> str:
        # 采集可能耗时；若用户在采集途中撤权，结果不得返回给模型。
        if not permissions.is_granted(t["scope"]):
            return _trim({"error": "unavailable_tool",
                          "hint": "权限已撤销，本次采集结果已丢弃。不要重试。"})
        return _trim(obj)

    try:
        limit = _as_limit(args)
        if name == "get_machine_snapshot":
            return finish(collectors.system_info())

        if name == "get_foreground_window":
            d = collectors.windows(limit=min(limit or 6, 10))
            items = d.get("items", [])
            return finish({
                "foreground": d.get("foreground"),
                "recent": [f"{w['title'][:60]} [{w['process'] or '?'}]" for w in items],
            })

        if name == "get_running_processes":
            n = min(limit or 10, 25)
            d = collectors.processes(limit=60)
            items = d.get("items", [])
            kw = _as_text(args.get("filter")).lower()
            if kw:
                items = [p for p in items
                         if kw in (p.get("name") or "").lower() or kw in (p.get("cmdline") or "").lower()]
            return finish({
                "total": d.get("total_procs"), "top": n, "filter": kw or None,
                "procs": [f"{p['name']} {p['mem_mb']}MB" +
                          (f" | {p['cmdline'][:70]}" if kw and p.get("cmdline") else "")
                          for p in items[:n]],
                "note": "按内存降序",
            })

        if name == "get_recent_notifications":
            d = collectors.notifications(limit=min(limit or 10, 20))
            return finish({
                "count": d.get("count"),
                "items": [f"[{n['app_name']}] {n['title'][:50]}" +
                          (f" — {n['body'][:70]}" if n.get("body") else "")
                          for n in d.get("items", [])],
            })

        if name == "get_browser_history":
            d = collectors.browser_history(limit=min(limit or 10, 20))
            return finish({
                "count": d.get("count"),
                "items": [f"{h['title'][:50]} | {h['url'][:90]}" for h in d.get("items", [])],
            })

        if name == "get_recent_files":
            d = collectors.user_files(limit=min(limit or 12, 25))
            area = _as_text(args.get("area"))
            items = d.get("items", [])
            if area:
                items = [f for f in items if area in (f.get("area") or "")]
            return finish({
                "areas": d.get("areas_found"),
                "files": [f"[{f['area']}] {f['name'][:52]} {f['size_kb']}KB" for f in items],
            })

        if name == "search_ai_logs":
            d = collectors.ai_logs(limit=min(limit or 6, 12))
            kw = _as_text(args.get("keyword")).lower()
            items = d.get("items", [])
            if kw:
                items = [x for x in items if kw in (x.get("text") or "").lower()]
            return finish({
                "count": len(items), "keyword": kw or None, "tools": d.get("tools_found"),
                "matches": [f"[{x['tool']}/{x['role']}] {x['text'][:150]}" for x in items],
            })

    except Exception as e:  # noqa: BLE001
        # 脱敏后再回灌：不把本机路径 / 用户名 / 临时文件位置泄给上游模型
        return _trim({"error": sanitize_error(e), "tool": name,
                      "hint": "该工具本次执行失败，不要重试，请基于其余信息作答。"})
    return _trim({"error": "未实现的工具", "tool": name,
                  "hint": "不要重试。"})


def parse_args(raw) -> dict | None:
    """严格解析模型工具参数；残缺或非对象 JSON 一律拒绝执行。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
