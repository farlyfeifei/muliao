r"""本机控制能力层 · 只读采集之外的「执行动作」

这是幕僚 Muliáo 的**纯执行能力层**：把 Windows 桌面控制原语（列窗口、激活/关闭窗口、
启动应用、点击控件、输入文本、发送组合键）封装成结构化、可离线测试的函数。

与 collectors.py 的分工：
  · collectors.py = 只读采集（看），受 permissions 七类数据 scope 约束。
  · machine_control.py = 执行动作（做），**本模块不含任何权限判断**。

⚠️ 调用前必须经「权限闸门 + Jev 门控」：
  权限闸门（machine_tools 侧）决定是否允许执行这类能力；
  Jev 门控（server.jev_ask）决定这一步动作在当前语境下是否该做。
  本模块只负责「能不能做、怎么做」，不负责「该不该做」——后者永远在调用方。

设计要点：
  1) **防御式导入**：pywinauto / uiautomation 未安装、或非 Windows 平台时，
     每个函数都返回结构化失败（error="control_unavailable"），**绝不抛异常到调用方**。
  2) **模块级 seam `_backend()`**：返回一个封装了底层 GUI 自动化的对象，或 None。
     测试可 monkeypatch 它注入假 backend，从而在没有真实桌面的 CI 上离线跑通。
  3) **统一返回 dict**：成功 {"ok": True, ...结果字段}；
     失败 {"ok": False, "error": <短码>, "hint": <给模型的一句话>, ...}。
     错误短码：control_unavailable（库缺失/非Win）、target_not_found（窗口/元素找不到）、
               invalid_args（参数缺失/类型错）、action_failed（执行期失败）。
  4) **不 import machine_tools**（会循环依赖）。错误文本自己做最小脱敏：
     只回「类型名 + 短描述」，抹掉本机用户目录 / 用户名，不把完整路径泄给上游模型。

backend seam 协议（假 backend 只要实现这些方法即可注入）：
  · enum_windows(limit:int) -> list[dict]      每项含 title/pid/process/hwnd
  · resolve_window(title:str|None, pid:int|None) -> token|None   找不到返回 None
  · activate(token) / close(token)             失败抛异常
  · launch(name_or_path:str, args) -> dict     返回 {"pid":..,"name":..}，失败抛异常
  · click(token, element_name, automation_id, button)   元素找不到抛 _TargetNotFound
  · type_text(token, text, element_name, enter)         同上
  · send_keys(token, keys)                              同上
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

# 桌面控制能力默认绑定到一个独立的能力 scope（与七类只读数据 scope 分开）。
# 这里只声明常量供调用方参考；本模块**不做**任何权限判断。
CONTROL_SCOPE = "machine_control"

# 合法鼠标按键
_VALID_BUTTONS = ("left", "right", "middle")

# list_windows 上限防呆：模型给 10**9 也不至于把整台机器灌进上下文
_MAX_LIMIT = 200


# ============ 内部异常：区分「找不到」与「执行失败」 ============
class _TargetNotFound(Exception):
    """backend 在窗口内找不到指定元素时抛出，供公开函数翻译成 target_not_found。"""


# ============ 最小脱敏（不依赖 machine_tools，复制一份精简逻辑） ============
def _scrub(msg: str) -> str:
    """抹掉本机用户目录 / 用户名，避免把完整路径泄给上游模型。"""
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if home:
        msg = msg.replace(home, "~")
    # Windows 用户目录 / *nix home 的通用形状
    msg = re.sub(r"[A-Za-z]:\\+Users\\+[^\\\s\"']+", "~", msg)
    msg = re.sub(r"(?<![\w])/home/[^/\s\"']+", "~", msg)
    if user and len(user) >= 3:
        msg = re.sub(r"\b" + re.escape(user) + r"\b", "<user>", msg)
    return " ".join(msg.split())


def _err(exc: BaseException, limit: int = 160) -> str:
    """把异常压成一行安全文本：类型名 + 脱敏后的短描述。"""
    msg = _scrub(str(exc).strip())
    if len(msg) > limit:
        msg = msg[:limit] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


# ============ 统一返回构造 ============
def _fail(code: str, hint: str, **extra: Any) -> dict:
    d: dict[str, Any] = {"ok": False, "error": code, "hint": hint}
    d.update(extra)
    return d


def _unavailable() -> dict:
    return _fail(
        "control_unavailable",
        "桌面控制能力当前不可用（非 Windows，或未安装 pywinauto）。"
        "不要重试；请告知用户此机器无法执行 GUI 自动化。",
    )


def _ok(**fields: Any) -> dict:
    d: dict[str, Any] = {"ok": True}
    d.update(fields)
    return d


# ============ 参数校验小工具 ============
def _clean_str(v: Any) -> str | None:
    """把可能是 None/数字/list 的值安全收成「去空白后的非空字符串」，否则 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _clean_limit(v: Any, default: int) -> int:
    """校验 limit：必须是正整数（拒绝 bool），封顶 _MAX_LIMIT。非法时抛 ValueError。"""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ValueError("limit 不能是布尔值")
    if not isinstance(v, int):
        # 容忍 "20" / 20.0 这类可无损转换的脏值
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("limit 必须是整数")
        if not f.is_integer():
            raise ValueError("limit 必须是整数")
        v = int(f)
    if v <= 0:
        raise ValueError("limit 必须为正")
    return min(v, _MAX_LIMIT)


# ============ pid / args 校验小工具（用哨兵类型区分「非法」与「None」） ============
class _PidError:
    """哨兵：pid 非法。"""


class _ArgsError:
    """哨兵：args 非法。"""


def _clean_pid(v: Any) -> int | None:
    """校验 pid：None→None；正整数→int；其他→_PidError 哨兵。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return _PidError()
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str):
        try:
            n = int(v.strip())
            return n if n > 0 else _PidError()
        except ValueError:
            return _PidError()
    return _PidError()


def _clean_args(v: Any) -> Any:
    """校验启动参数：None→None；str→str；list/tuple[str]→原样；其他→_ArgsError 哨兵。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        if all(isinstance(x, str) for x in v):
            return list(v)
        return _ArgsError()
    return _ArgsError()


# ============ 组合键翻译（纯函数，可离线测试） ============
_NAMED_KEYS = {
    "enter": "{ENTER}", "return": "{ENTER}", "esc": "{ESC}", "escape": "{ESC}",
    "tab": "{TAB}", "space": "{SPACE}", "backspace": "{BACKSPACE}", "bksp": "{BACKSPACE}",
    "delete": "{DELETE}", "del": "{DELETE}", "insert": "{INSERT}", "ins": "{INSERT}",
    "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "home": "{HOME}", "end": "{END}", "pgup": "{PGUP}", "pgdn": "{PGDN}",
    "pageup": "{PGUP}", "pagedown": "{PGDN}", "printscreen": "{PRTSC}", "prtsc": "{PRTSC}",
}
_MODS = {"ctrl": "^", "control": "^", "alt": "%", "option": "%", "shift": "+"}


def _combo_to_keys(keys: str) -> str:
    """把友好组合键（"ctrl+s" / "alt+f4" / "enter"）翻译成 pywinauto send_keys 语法。

    这是**纯字符串变换**，不触碰 GUI，可独立离线测试。
    """
    parts = [p.strip() for p in keys.split("+") if p.strip()]
    if not parts:
        return keys.strip()
    prefix = ""
    for p in parts[:-1]:
        pl = p.lower()
        if pl in _MODS:
            prefix += _MODS[pl]
        elif pl in ("win", "windows", "cmd", "super"):
            # pywinauto 无标准 Win 键映射，近似用 ^（Ctrl）兜底，避免静默吞掉
            prefix += "^"
        else:
            prefix += "{" + p.upper() + "}"
    main = parts[-1]
    ml = main.lower()
    if ml in _NAMED_KEYS:
        body = _NAMED_KEYS[ml]
    elif re.fullmatch(r"f\d{1,2}", ml):
        body = "{" + ml.upper() + "}"
    elif len(main) == 1:
        body = main
    else:
        body = "{" + main.upper() + "}"
    return prefix + body


def _escape_literal(text: str) -> str:
    """把要「逐字输入」的文本里的 pywinauto 特殊字符转义，避免被当成组合键。"""
    out: list[str] = []
    for ch in text:
        if ch == "{":
            out.append("{{}")
        elif ch == "}":
            out.append("{}}")
        elif ch in "^%~()+[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


# ============ 真实 backend（pywinauto / win32gui） ============
class _PywinautoBackend:
    """封装 pywinauto（UIA backend）+ win32gui 的底层执行原语。

    构造时若任何依赖缺失会抛异常，由 _backend() 捕获后降级为 None。
    """

    def __init__(self) -> None:
        # 触发式导入：缺库直接抛，_backend() 负责降级
        from pywinauto import Application, Desktop  # noqa: F401
        import win32gui  # noqa: F401
        import win32process  # noqa: F401
        self._Desktop = Desktop
        self._Application = Application

    # ---- 枚举（复用 win32gui，与 collectors.windows 同源，但不含权限判断） ----
    def enum_windows(self, limit: int) -> list[dict]:
        import win32gui
        import win32process
        out: list[dict] = []

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                if not title.strip():
                    return
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = ""
                try:
                    import psutil
                    proc = psutil.Process(pid).name()
                except Exception:
                    pass
                out.append({"hwnd": hwnd, "title": title[:200], "pid": pid, "process": proc})
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        return out[:limit]

    # ---- 定位窗口：返回 pywinauto 窗口包装对象，找不到返回 None ----
    def resolve_window(self, title: str | None, pid: int | None):
        import win32gui
        import win32process
        hwnd = 0
        if title:
            hwnd = win32gui.FindWindow(None, title)
            if not hwnd:
                matches: list[int] = []
                low = title.lower()

                def cb(h, _):
                    try:
                        if win32gui.IsWindowVisible(h) and low in win32gui.GetWindowText(h).lower():
                            matches.append(h)
                    except Exception:
                        pass

                win32gui.EnumWindows(cb, None)
                hwnd = matches[0] if matches else 0
        elif pid:
            def cb2(h, _):
                nonlocal hwnd
                if hwnd:
                    return
                try:
                    if not win32gui.IsWindowVisible(h):
                        return
                    _t, p = win32process.GetWindowThreadProcessId(h)
                    if p == pid:
                        hwnd = h
                except Exception:
                    pass

            win32gui.EnumWindows(cb2, None)
        else:
            hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        try:
            return self._Desktop(backend="uia").window(handle=hwnd)
        except Exception:
            return None

    def activate(self, win) -> None:
        win.set_focus()

    def close(self, win) -> None:
        win.close()

    def launch(self, name_or_path: str, args: Any) -> dict:
        cmd = name_or_path
        if args:
            if isinstance(args, (list, tuple)):
                cmd = cmd + " " + " ".join(str(a) for a in args)
            else:
                cmd = cmd + " " + str(args)
        app = self._Application(backend="uia").start(cmd)
        return {"pid": getattr(app, "process", None),
                "name": os.path.basename(name_or_path) or name_or_path}

    def _child(self, win, element_name: str | None, automation_id: str | None):
        spec: dict[str, Any] = {}
        if automation_id:
            spec["auto_id"] = automation_id
        elif element_name:
            spec["title"] = element_name
        if not spec:
            return None
        child = win.child_window(**spec)
        try:
            if not child.exists(timeout=2):
                raise _TargetNotFound("element not found")
        except _TargetNotFound:
            raise
        except Exception:
            # exists() 自身异常视为找不到，避免误报成功
            raise _TargetNotFound("element probe failed")
        return child

    def click(self, win, element_name: str | None, automation_id: str | None, button: str) -> None:
        child = self._child(win, element_name, automation_id)
        if child is None:
            raise _TargetNotFound("element not specified")
        child.click_input(button=button)

    def type_text(self, win, text: str, element_name: str | None, enter: bool) -> None:
        target = win
        if element_name:
            child = self._child(win, element_name, None)
            if child is None:
                raise _TargetNotFound("element not found")
            target = child
        try:
            target.set_focus()
        except Exception:
            pass
        body = _escape_literal(text)
        if enter:
            body += "{ENTER}"
        target.type_keys(body, with_spaces=True, pause=0.02)

    def send_keys(self, win, keys: str) -> None:
        combo = _combo_to_keys(keys)
        try:
            win.set_focus()
        except Exception:
            pass
        win.type_keys(combo, pause=0.02)


# ============ 模块级 seam：测试 monkeypatch 此函数注入假 backend ============
_BACKEND: Any = None
_BACKEND_TRIED = False


def _backend():
    """返回底层执行 backend；不可用（非 Windows / 缺库 / 构造失败）时返回 None。

    这是测试 seam：测试用 `machine_control._backend = lambda: FakeBackend()` 替换它，
    即可在没有真实桌面、没装 pywinauto 的环境里离线验证全部公开函数。
    """
    global _BACKEND, _BACKEND_TRIED
    if _BACKEND_TRIED:
        return _BACKEND
    _BACKEND_TRIED = True
    if sys.platform != "win32":
        _BACKEND = None
        return None
    try:
        import pywinauto  # noqa: F401
    except Exception:
        _BACKEND = None
        return None
    try:
        _BACKEND = _PywinautoBackend()
    except Exception:
        _BACKEND = None
    return _BACKEND


def _backend_or_unavailable():
    """取 backend；None 时返回 (None, 失败dict)，否则 (backend, None)。"""
    b = _backend()
    if b is None:
        return None, _unavailable()
    return b, None


# ============ 公开能力函数 ============
def list_windows(limit: int = 20) -> dict:
    """列出可控制的顶层窗口（title/pid/process）。

    返回：{"ok": True, "count": int, "windows": [{"title","pid","process","hwnd"}, ...]}
    失败：control_unavailable / invalid_args / action_failed
    """
    try:
        n = _clean_limit(limit, 20)
    except ValueError as e:
        return _fail("invalid_args", f"limit 非法：{e}", arg="limit")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        wins = b.enum_windows(n)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "枚举窗口失败。", detail=_err(e))
    if not isinstance(wins, list):
        wins = []
    return _ok(count=len(wins), windows=wins)


def focus_window(title: str | None = None, pid: int | None = None) -> dict:
    """把指定窗口置前 / 激活。title 或 pid 至少给一个。

    返回：{"ok": True, "title": str|None, "pid": int|None}
    失败：invalid_args（缺 title/pid）、control_unavailable、target_not_found、action_failed
    """
    t = _clean_str(title)
    if title is not None and not isinstance(title, str) and t is None:
        return _fail("invalid_args", "title 必须是字符串。", arg="title")
    p = _clean_pid(pid)
    if isinstance(p, _PidError):
        return _fail("invalid_args", "pid 必须是正整数。", arg="pid")
    if t is None and p is None:
        return _fail("invalid_args", "需要 title 或 pid 至少其一。", arg="title|pid")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        win = b.resolve_window(t, p)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "定位窗口失败。", detail=_err(e))
    if win is None:
        return _fail("target_not_found", "找不到匹配的窗口，请确认标题或 pid。",
                     title=t, pid=p)
    try:
        b.activate(win)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "激活窗口失败。", detail=_err(e))
    return _ok(title=t, pid=p)


def close_window(title: str | None = None, pid: int | None = None) -> dict:
    """关闭指定窗口。title 或 pid 至少给一个。

    返回：{"ok": True, "title": str|None, "pid": int|None}
    失败：invalid_args、control_unavailable、target_not_found、action_failed
    """
    t = _clean_str(title)
    if title is not None and not isinstance(title, str) and t is None:
        return _fail("invalid_args", "title 必须是字符串。", arg="title")
    p = _clean_pid(pid)
    if isinstance(p, _PidError):
        return _fail("invalid_args", "pid 必须是正整数。", arg="pid")
    if t is None and p is None:
        return _fail("invalid_args", "需要 title 或 pid 至少其一。", arg="title|pid")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        win = b.resolve_window(t, p)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "定位窗口失败。", detail=_err(e))
    if win is None:
        return _fail("target_not_found", "找不到匹配的窗口，无法关闭。", title=t, pid=p)
    try:
        b.close(win)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "关闭窗口失败。", detail=_err(e))
    return _ok(title=t, pid=p)


def open_application(name_or_path: str, args: Any = None) -> dict:
    """启动一个应用（按名字或路径），可附启动参数。

    返回：{"ok": True, "pid": int|None, "name": str}
    失败：invalid_args（缺 name_or_path / args 类型错）、control_unavailable、action_failed
    """
    target = _clean_str(name_or_path)
    if target is None:
        return _fail("invalid_args", "name_or_path 不能为空。", arg="name_or_path")
    norm_args = _clean_args(args)
    if isinstance(norm_args, _ArgsError):
        return _fail("invalid_args", "args 必须是字符串或字符串列表。", arg="args")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        info = b.launch(target, norm_args)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "启动应用失败。", detail=_err(e))
    if not isinstance(info, dict):
        info = {}
    return _ok(pid=info.get("pid"), name=info.get("name") or target)


def click_element(window_title: str | None = None, element_name: str | None = None,
                  automation_id: str | None = None, button: str = "left") -> dict:
    """在窗口内点击一个 UI 控件（按 name 或 automation_id 定位）。

    window_title 为空时作用于当前前台窗口。
    返回：{"ok": True, "window": str|None, "element": str|None, "auto_id": str|None, "button": str}
    失败：invalid_args（缺 element_name/automation_id 或 button 非法）、
          control_unavailable、target_not_found、action_failed
    """
    wt = _clean_str(window_title)
    if window_title is not None and not isinstance(window_title, str) and wt is None:
        return _fail("invalid_args", "window_title 必须是字符串。", arg="window_title")
    en = _clean_str(element_name)
    aid = _clean_str(automation_id)
    if en is None and aid is None:
        return _fail("invalid_args", "需要 element_name 或 automation_id 至少其一。",
                     arg="element_name|automation_id")
    btn = button if isinstance(button, str) else ""
    btn = btn.strip().lower()
    if btn not in _VALID_BUTTONS:
        return _fail("invalid_args",
                     f"button 必须是 {'/'.join(_VALID_BUTTONS)} 之一。", arg="button")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        win = b.resolve_window(wt, None)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "定位窗口失败。", detail=_err(e))
    if win is None:
        return _fail("target_not_found", "找不到目标窗口。", window=wt)
    try:
        b.click(win, en, aid, btn)
    except _TargetNotFound as e:
        return _fail("target_not_found", "在窗口内找不到该控件。",
                     window=wt, element=en, auto_id=aid, detail=_err(e))
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "点击控件失败。", detail=_err(e))
    return _ok(window=wt, element=en, auto_id=aid, button=btn)


def type_text(text: str, window_title: str | None = None,
              element_name: str | None = None, enter: bool = False) -> dict:
    """向（可选定位的）焦点控件输入文本；enter=True 时末尾追加回车。

    返回：{"ok": True, "chars": int, "enter": bool, "window": str|None, "element": str|None}
    失败：invalid_args（text 非字符串或为空）、control_unavailable、target_not_found、action_failed
    """
    if not isinstance(text, str):
        return _fail("invalid_args", "text 必须是字符串。", arg="text")
    if not text.strip():
        return _fail("invalid_args", "text 不能为空。", arg="text")
    wt = _clean_str(window_title)
    if window_title is not None and not isinstance(window_title, str) and wt is None:
        return _fail("invalid_args", "window_title 必须是字符串。", arg="window_title")
    en = _clean_str(element_name)
    do_enter = bool(enter)

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        win = b.resolve_window(wt, None)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "定位窗口失败。", detail=_err(e))
    if win is None:
        return _fail("target_not_found", "找不到目标窗口。", window=wt)
    try:
        b.type_text(win, text, en, do_enter)
    except _TargetNotFound as e:
        return _fail("target_not_found", "在窗口内找不到该输入控件。",
                     window=wt, element=en, detail=_err(e))
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "输入文本失败。", detail=_err(e))
    return _ok(chars=len(text), enter=do_enter, window=wt, element=en)


def press_keys(keys: str, window_title: str | None = None) -> dict:
    """发送组合键，如 "ctrl+s"、"alt+f4"、"enter"。window_title 为空时作用于前台窗口。

    返回：{"ok": True, "keys": str, "window": str|None}
    失败：invalid_args（keys 非字符串或为空）、control_unavailable、target_not_found、action_failed
    """
    if not isinstance(keys, str):
        return _fail("invalid_args", "keys 必须是字符串。", arg="keys")
    if not keys.strip():
        return _fail("invalid_args", "keys 不能为空。", arg="keys")
    wt = _clean_str(window_title)
    if window_title is not None and not isinstance(window_title, str) and wt is None:
        return _fail("invalid_args", "window_title 必须是字符串。", arg="window_title")

    b, bad = _backend_or_unavailable()
    if bad:
        return bad
    try:
        win = b.resolve_window(wt, None)
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "定位窗口失败。", detail=_err(e))
    if win is None:
        return _fail("target_not_found", "找不到目标窗口。", window=wt)
    try:
        b.send_keys(win, keys.strip())
    except _TargetNotFound as e:
        return _fail("target_not_found", "目标窗口不可用。", window=wt, detail=_err(e))
    except Exception as e:  # noqa: BLE001
        return _fail("action_failed", "发送组合键失败。", detail=_err(e))
    return _ok(keys=keys.strip(), window=wt)
