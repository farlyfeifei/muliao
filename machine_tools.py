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
import machine_control
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
    # ---- 电脑控制能力（scope=computer_control，默认关、不随全选打开、独立显式授权）----
    # 这些是「执行动作」而非只读采集。每个都受 computer_control 权限闸门 + 每次执行前的
    # Jev 门控（server.action_gate）双重约束；高风险动作会先请用户确认才真正执行。
    "list_windows": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "list_windows",
                "description": "列出当前可控制的桌面窗口（标题、进程、pid）。在执行任何窗口操作前，先用它确认目标窗口存在与准确标题。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "最多返回几个窗口，默认 20，上限 200", "default": 20},
                    },
                    "required": [],
                },
            },
        },
    },
    "focus_window": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "focus_window",
                "description": "把指定窗口切换到前台/激活，以便后续点击或输入。按 title（可模糊匹配）或 pid 定位，二者至少给一个。当用户说「切到那个窗口」「把它调到前面」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "窗口标题（支持部分匹配）"},
                        "pid": {"type": "integer", "description": "窗口所属进程 pid"},
                    },
                    "required": [],
                },
            },
        },
    },
    "close_window": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "close_window",
                "description": "关闭指定窗口。按 title 或 pid 定位，二者至少给一个。这会终止该窗口对应的程序，属较高风险动作。当用户说「关掉那个窗口/程序」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "窗口标题（支持部分匹配）"},
                        "pid": {"type": "integer", "description": "窗口所属进程 pid"},
                    },
                    "required": [],
                },
            },
        },
    },
    "open_application": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "open_application",
                "description": "启动一个应用程序（按名字如 notepad / 计算器，或可执行文件路径），可附启动参数。当用户说「打开某程序」「帮我启动 X」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name_or_path": {"type": "string", "description": "应用名或可执行文件路径"},
                        "args": {"type": "string", "description": "可选启动参数（字符串，或字符串数组）"},
                    },
                    "required": ["name_or_path"],
                },
            },
        },
    },
    "click_element": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "click_element",
                "description": "在某个窗口内点击一个界面控件（按钮、菜单项、输入框等），按控件名 element_name 或 automation_id 定位。window_title 为空则作用于当前前台窗口。当用户说「点那个按钮」「勾选某项」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window_title": {"type": "string", "description": "目标窗口标题；留空表示当前前台窗口"},
                        "element_name": {"type": "string", "description": "控件显示名称"},
                        "automation_id": {"type": "string", "description": "控件的 automation id（比名称更稳定）"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "鼠标按键，默认 left", "default": "left"},
                    },
                    "required": [],
                },
            },
        },
    },
    "type_text": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "type_text",
                "description": "向（可选定位的）输入框输入文字，可选在末尾回车。先确保目标窗口/控件已聚焦（必要时先 focus_window 或 click_element）。当用户说「在某处输入…」「帮我填…」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要输入的文本"},
                        "window_title": {"type": "string", "description": "目标窗口标题；留空表示当前前台窗口"},
                        "element_name": {"type": "string", "description": "目标输入控件名称；留空表示当前焦点控件"},
                        "enter": {"type": "boolean", "description": "输入后是否追加回车，默认 false", "default": False},
                    },
                    "required": ["text"],
                },
            },
        },
    },
    "press_keys": {
        "scope": "computer_control",
        "spec": {
            "type": "function",
            "function": {
                "name": "press_keys",
                "description": "发送组合键或单个按键，如 ctrl+s、alt+f4、enter、esc。window_title 为空则作用于当前前台窗口。当用户说「按某快捷键」「保存一下（Ctrl+S）」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "组合键，用 + 连接，如 ctrl+s、alt+f4；单键如 enter、esc、a"},
                        "window_title": {"type": "string", "description": "目标窗口标题；留空表示当前前台窗口"},
                    },
                    "required": ["keys"],
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


def tool_scope(name: str) -> str | None:
    """返回某工具绑定的权限 scope；未知工具返回 None。

    server 的动作门控据此判断一个工具是否「控制类」（scope == computer_control），
    从而决定 Jev 不可用时 fail-closed（控制类拒绝）还是降级放行（只读类）。
    """
    t = TOOL_DEFS.get(name)
    return t["scope"] if t else None


def is_control_tool(name: str) -> bool:
    """该工具是否为电脑控制类（执行动作，而非只读采集）。"""
    return tool_scope(name) == "computer_control"



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

        # ---- 电脑控制能力（执行动作）----
        # 到这里 computer_control 权限已过（函数头 scope 闸门）；动作该不该做由
        # server.action_gate 的 Jev 门控在执行前裁决。machine_control 返回 dict，
        # 这里统一转成给模型的截断文本；控制结果不复用 finish() 的权限复查语义
        # （finish 是为只读采集设计的），但同样在返回前再查一次 computer_control，
        # 防止执行途中撤权后仍把结果回灌给模型。
        def finish_control(res: dict) -> str:
            if not permissions.is_granted(t["scope"]):
                return _trim({"error": "unavailable_tool",
                              "hint": "电脑控制权限已撤销，本次动作结果已丢弃。不要重试。"})
            return _trim(res)

        if name == "list_windows":
            return finish_control(machine_control.list_windows(limit=limit or 20))

        if name == "focus_window":
            return finish_control(machine_control.focus_window(
                title=args.get("title"), pid=args.get("pid")))

        if name == "close_window":
            return finish_control(machine_control.close_window(
                title=args.get("title"), pid=args.get("pid")))

        if name == "open_application":
            return finish_control(machine_control.open_application(
                name_or_path=_as_text(args.get("name_or_path")), args=args.get("args")))

        if name == "click_element":
            return finish_control(machine_control.click_element(
                window_title=args.get("window_title"),
                element_name=args.get("element_name"),
                automation_id=args.get("automation_id"),
                button=_as_text(args.get("button")) or "left"))

        if name == "type_text":
            # 直接透传原始 text（不经 _as_text 的 strip），保留用户想输入的首尾空白；
            # machine_control.type_text 自己校验 isinstance(str) 与非空。
            return finish_control(machine_control.type_text(
                text=args.get("text"),
                window_title=args.get("window_title"),
                element_name=args.get("element_name"),
                enter=bool(args.get("enter"))))

        if name == "press_keys":
            return finish_control(machine_control.press_keys(
                keys=_as_text(args.get("keys")),
                window_title=args.get("window_title")))

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
