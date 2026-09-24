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
    "list_agent_sessions": {
        "scope": "ai_logs",
        "spec": {
            "type": "function",
            "function": {
                "name": "list_agent_sessions",
                "description": (
                    "列出本机 AI Agent（Claude Code / Codex）的会话，每条给出**真实项目路径 cwd**、"
                    "git 分支、最后活动时间、首条用户消息。当用户提到「某个 Agent 的通知/报错」"
                    "「哪个项目出问题了」时先调这个定位到具体项目和会话，**不要靠猜文件位置**。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回条数，默认 12", "default": 12},
                        "filter": {"type": "string", "description": "按项目名/分支/消息内容过滤"},
                    },
                    "required": [],
                },
            },
        },
    },
    # ---- 读取文件内容（scope=file_content，独立能力，默认关、不随全选打开）----
    # 内容会进入上游模型上下文，敏感度远高于元数据，故单列一个能力。
    "list_folder": {
        "scope": "file_content",
        "spec": {
            "type": "function",
            "function": {
                "name": "list_folder",
                "description": (
                    "列出指定文件夹的内容（文件名、类型、大小、修改时间）。路径用 list_agent_sessions "
                    "拿到的 cwd，或用户明确给出的路径。用于在动手改之前看清项目结构。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件夹绝对路径"},
                        "recursive": {"type": "boolean", "description": "是否递归子目录，默认 false"},
                        "limit": {"type": "integer", "description": "最多返回条数，默认 120", "default": 120},
                    },
                    "required": ["path"],
                },
            },
        },
    },
    "read_file": {
        "scope": "file_content",
        "spec": {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "读取一个文本文件的内容（可按行范围）。用于真正看懂 Agent 报的问题、方案文档、"
                    "配置或源码，再决定怎么改。**只能读文本文件**；二进制会被拒绝。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "start_line": {"type": "integer", "description": "起始行（1 起），默认 1"},
                        "max_lines": {"type": "integer", "description": "最多读取行数，默认 200", "default": 200},
                    },
                    "required": ["path"],
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
def _shorten_strings(obj: Any, per_str: int, depth: int = 0) -> Any:
    """递归把对象里的长字符串裁短，**在序列化之前**做。

    为什么不在序列化后截断：那会把 JSON 从中间切断成非法文本（实测 read_file
    正文超限时模型收到的是断尾的 content 串，既读不出内容也会误导解析）。
    裁完再 dumps，结构永远合法。
    """
    if depth > 6:
        return obj
    if isinstance(obj, str):
        if len(obj) <= per_str:
            return obj
        return obj[:per_str] + f"…(已截断，原长 {len(obj)} 字符)"
    if isinstance(obj, dict):
        return {k: _shorten_strings(v, per_str, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_shorten_strings(v, per_str, depth + 1) for v in obj]
    return obj


def _trim(obj: Any, limit: int = 1500) -> str:
    """序列化工具结果并截断，**保证输出永远是合法 JSON**。

    limit 默认 1500 字符是**为缓存命中率**定的：工具结果是全新内容，
    必然 miss，它的体积直接决定这一轮的命中率下限（rate ≈ 前缀/(前缀+新增)）。
    实测一个未裁剪的进程列表可达 2600+ 字符，把整轮拉到 60%。

    做法：按 limit 裁内部长字符串再序列化；若总量仍超预算（多个字符串累加），
    就逐档收紧单串预算重试，直到落进「limit + 结构开销」为止。
    绝不在序列化后做 `s[:limit]` 硬切——那会把 JSON 从中间切断成非法文本
    （实测 read_file 正文超限时模型收到断尾 JSON，既读不出内容也会误导解析）。
    """
    # 预算只给结构开销留少量余量（键名/引号/括号），不放水到数倍，否则缓存命中率崩。
    budget = max(int(limit * 1.25), limit + 200)
    for per_str in (limit, limit // 2, limit // 4, limit // 8, 60):
        s = json.dumps(_shorten_strings(obj, per_str), ensure_ascii=False, default=str)
        if len(s) <= budget:
            return s
    # 极端情况（元素数量本身巨大）：单串裁到最短仍超预算，返回该结果并如实标注。
    s = json.dumps(_shorten_strings(obj, 40), ensure_ascii=False, default=str)
    return json.dumps({"truncated_result": s[:budget],
                       "note": "结果元素过多，已大幅截断；请缩小 limit 或加过滤条件。"},
                      ensure_ascii=False)


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


# ---- 文件内容读取的辅助与安全防线 ----
# 噪声/依赖目录：递归列目录时跳过，避免把整棵 node_modules 灌进上下文。
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode",
})
# 单文件读取上限（防超大文件撑爆上下文）。
_MAX_READ_BYTES = 512 * 1024
# 二进制扩展名：不能当文本读，直接拒。
_BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".mp4", ".mov",
    ".mp3", ".wav", ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".dll",
    ".so", ".dylib", ".pyd", ".pdf", ".onnx", ".bin", ".dat", ".class",
    ".jar", ".woff", ".woff2", ".ttf", ".otf", ".db", ".sqlite", ".sqlite3",
})
# 绝不允许读取的敏感路径片段（存密钥/凭据的文件）。命中即拒，防止 file_content
# 能力被用来把 API key / 私钥 / 凭据读进上游模型上下文。
_SECRET_PATH_HINTS = (
    "muliao\\config.json", "muliao/config.json",       # 本项目的密钥配置
    "config.json", ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".pem", ".pfx", ".p12", ".key", "credentials", "secrets",
    ".npmrc", ".pypirc", ".netrc", ".aws\\credentials", ".aws/credentials",
    ".kube\\config", ".kube/config", "token.json", ".git-credentials",
)
# 敏感**目录**：整棵树禁读。
# 只按文件名黑名单是不够的——实测 ~/.ssh/creator_city_deploy 这种自定义名字的
# 私钥（无扩展名）能绕过名单被完整读出。密钥存放目录里放什么都算敏感，一律拒。
_SECRET_DIRS = (
    ".ssh", ".gnupg", ".aws", ".kube", ".azure", ".docker",
    ".config\\gh", ".config/gh", "credential", "credentials",
    "appdata\\roaming\\muliao", "appdata/roaming/muliao",
    "appdata\\local\\muliao", "appdata/local/muliao",
)
# 私钥内容特征：即使路径没命中任何规则，读到这些也立即丢弃内容并拒绝。
# 最后一道防线——目录和文件名都可能被绕过，但私钥的正文格式骗不了人。
_PRIVATE_KEY_MARKERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "ssh-rsa AAAA", "ssh-ed25519 AAAA", "ecdsa-sha2-nistp",
)


def _binary_ext(path: str) -> bool:
    return os.path.splitext(str(path))[1].lower() in _BINARY_EXTS


def _fmt_time(ts: Any) -> str:
    """把 mtime 秒时间戳转成可读串；非法值返回空。"""
    try:
        v = float(ts)
        if v <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (TypeError, ValueError):
        return ""


def _blocked_secret_path(path: str) -> str:
    """若路径指向存密钥/凭据的文件或敏感目录，返回拒绝原因；否则返回空串（放行）。

    这是 file_content 能力的核心防线：即使权限已开，也绝不把 config.json、
    SSH 私钥、.env、云凭据等读进上游模型上下文。

    两层匹配，都用大小写不敏感的归一化子串（反斜杠统一成斜杠）：
      ① 敏感**目录**整棵树禁读 —— 只按文件名挡不住自定义名字的私钥
         （实测 ~/.ssh/creator_city_deploy 无扩展名，能绕过纯文件名黑名单）。
      ② 敏感**文件名**片段 —— config.json / .env / *.key / *.pem 等。
    """
    raw = str(path or "")
    low = raw.replace("\\", "/").lower()

    # ① 敏感目录：路径中任何一段命中即拒（整棵树）
    for d in _SECRET_DIRS:
        seg = d.replace("\\", "/").lower().strip("/")
        # 目录名作为独立路径段出现：/a/.ssh/b 或结尾 /a/.ssh
        if f"/{seg}/" in f"/{low}/" or low.endswith(f"/{seg}"):
            return (f"该路径位于敏感目录（{os.path.basename(d)}）内，"
                    "整棵树禁止读取；不要重试该路径")

    # ② 敏感文件名片段（结尾或后接分隔符，避免误伤 myconfig.json.bak 之类）
    for hint in _SECRET_PATH_HINTS:
        h = hint.replace("\\", "/").lower()
        if low.endswith(h) or f"/{h}" in low or low.endswith(h.lstrip("/")):
            return f"该路径疑似存放密钥/凭据（{os.path.basename(raw)}），出于安全不允许读取"
    return ""


def _looks_like_private_key(content: str) -> bool:
    """内容兜底：正文含私钥特征就判定为凭据文件（路径规则的最后一道防线）。

    目录名和文件名都可能被绕过（自定义命名、非常规位置），但私钥正文格式骗不了人。
    """
    head = str(content or "")[:4000]
    return any(marker in head for marker in _PRIVATE_KEY_MARKERS)


def _safe_abs_path(path: str) -> str | None:
    """把模型给的路径规范成本机绝对路径；不存在或非绝对则返回 None。

    要求绝对路径，避免相对路径落到进程 CWD（打包态 CWD 不可预期）读到意外文件。
    """
    p = str(path or "").strip()
    if not p:
        return None
    if not os.path.isabs(p):
        return None
    norm = os.path.normpath(p)
    if not os.path.exists(norm):
        return None
    return norm


def _file_brief(path: str, base: str) -> dict:
    """构造一条文件/目录摘要（名、类型、大小）。相对 base 给出简短相对路径。"""
    is_dir = os.path.isdir(path)
    try:
        size_kb = 0 if is_dir else max(0, os.path.getsize(path)) // 1024
    except OSError:
        size_kb = 0
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        rel = os.path.basename(path)
    return {
        "name": rel.replace("\\", "/"),
        "kind": "dir" if is_dir else "file",
        "size_kb": size_kb,
    }


def execute_tool(name: str, args: dict | None) -> str:
    """执行工具，返回给模型的字符串结果。
    双重闸门：先查该工具是否已授权（防止授权被撤销后仍被调用）。"""
    args = _as_dict(args)
    t = TOOL_DEFS.get(name)
    if not t or not permissions.is_granted(t["scope"]):
        return _trim({"error": "unavailable_tool",
                      "hint": "该工具当前不可用。不要重试；请基于已有信息回答，或告知用户在权限页开启相应能力。"})

    def finish(obj, chars: int = 1500) -> str:
        # 采集可能耗时；若用户在采集途中撤权，结果不得返回给模型。
        if not permissions.is_granted(t["scope"]):
            return _trim({"error": "unavailable_tool",
                          "hint": "权限已撤销，本次采集结果已丢弃。不要重试。"})
        return _trim(obj, limit=chars)

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

        if name == "list_agent_sessions":
            d = collectors.ai_sessions(limit=min(limit or 12, 40))
            kw = _as_text(args.get("filter")).lower()
            items = d.get("items", [])
            if kw:
                items = [s for s in items if kw in " ".join([
                    str(s.get("project") or ""), str(s.get("cwd") or ""),
                    str(s.get("git_branch") or ""), str(s.get("first_user_message") or ""),
                ]).lower()]
            return finish({
                "count": len(items), "tools": d.get("tools_found"), "filter": kw or None,
                "sessions": [
                    {
                        "tool": s.get("tool"),
                        "project": s.get("project"),
                        "cwd": s.get("cwd"),
                        "cwd_exists": s.get("cwd_exists"),
                        "git_branch": s.get("git_branch"),
                        "last_activity": _fmt_time(s.get("last_activity")),
                        "messages": s.get("message_count"),
                        "first_user_message": s.get("first_user_message"),
                    } for s in items
                ],
                "note": "cwd 是该项目文件夹的绝对路径；要改这个项目的文件，直接用它，不要猜。",
            })

        # ---- 读取文件内容（scope=file_content）----
        if name in ("list_folder", "read_file"):
            path = _as_text(args.get("path"))
            blocked = _blocked_secret_path(path)
            if blocked:
                return finish({"error": "path_not_allowed",
                               "hint": f"{blocked}。不要重试该路径。"})
            resolved = _safe_abs_path(path)
            if resolved is None:
                return finish({"error": "invalid_args",
                               "hint": "path 必须是本机存在的绝对路径。"})

            if name == "list_folder":
                if not os.path.isdir(resolved):
                    return finish({"error": "not_a_directory", "path": resolved})
                recursive = bool(args.get("recursive"))
                n = min(limit or 120, 400)
                out = []
                try:
                    if recursive:
                        for root, dirs, files in os.walk(resolved):
                            # 跳过噪声/依赖目录，避免把整棵 node_modules 灌进上下文
                            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                            for fn in files:
                                fp = os.path.join(root, fn)
                                out.append(_file_brief(fp, resolved))
                                if len(out) >= n:
                                    raise StopIteration
                            if len(out) >= n:
                                break
                    else:
                        for e in os.scandir(resolved):
                            if e.is_dir() and e.name in _SKIP_DIRS:
                                continue
                            out.append(_file_brief(e.path, resolved))
                            if len(out) >= n:
                                break
                except StopIteration:
                    pass
                except OSError as exc:
                    return finish({"error": "read_failed", "path": resolved,
                                   "detail": type(exc).__name__})
                out.sort(key=lambda x: (x["kind"], x["name"].lower()))
                return finish({
                    "path": resolved, "count": len(out), "truncated": len(out) >= n,
                    "entries": [f"{'[D] ' if e['kind']=='dir' else '    '}{e['name']}"
                                + (f"  {e['size_kb']}KB" if e["kind"] == "file" else "")
                                for e in out],
                }, chars=4000)

            # read_file
            if not os.path.isfile(resolved):
                return finish({"error": "not_a_file", "path": resolved})
            if _binary_ext(resolved):
                return finish({"error": "binary_file",
                               "hint": "该扩展名是二进制文件，无法当文本读取。"})
            try:
                if os.path.getsize(resolved) > _MAX_READ_BYTES:
                    return finish({"error": "file_too_large",
                                   "hint": f"文件超过 {_MAX_READ_BYTES // 1024}KB，"
                                           "请缩小范围或只读关键部分。"})
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError as exc:
                return finish({"error": "read_failed", "path": resolved,
                               "detail": type(exc).__name__})
            start = max(1, int(args.get("start_line") or 1))
            max_lines = min(int(args.get("max_lines") or 200), 600)
            chunk = lines[start - 1: start - 1 + max_lines]
            content = "".join(chunk)
            # 内容兜底：路径规则可能被自定义命名/非常规位置绕过，但私钥正文格式骗不了人。
            # 命中即丢弃内容、返回拒绝——绝不把私钥正文回灌给上游模型。
            if _looks_like_private_key(content):
                return finish({"error": "path_not_allowed",
                               "hint": "该文件正文含私钥/凭据特征，出于安全已拒绝读取。"
                                       "不要重试该路径。"})
            return finish({
                "path": resolved, "total_lines": len(lines),
                "start_line": start, "returned_lines": len(chunk),
                "truncated": start - 1 + max_lines < len(lines),
                "content": content,
            }, chars=8000)

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
