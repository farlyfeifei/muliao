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

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Mapping

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
            cwd = ""
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
                        # cwd 记录该会话真实的工作目录（项目文件夹绝对路径）。
                        # 早先版本把它丢了，模型只能靠猜文件位置——这是「找不到要操作的
                        # 文件夹」的根因。这里补上，并顺带记 git 分支。
                        if not cwd:
                            c = o.get("cwd")
                            if isinstance(c, str) and c.strip():
                                cwd = c.strip()
                        text = _extract_msg(o)
                        if not text:
                            continue
                        role, body = text
                        items.append({
                            "tool": tool, "role": role, "text": body[:500],
                            "session": os.path.basename(fp)[:40],
                            "cwd": cwd,
                            "git_branch": str(o.get("gitBranch") or ""),
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


# Claude Code 的项目目录名是把绝对路径里的分隔符换成 '-' 编码来的
# （如 D:\2026暑假一切\进化酒馆\muliao -> D--2026暑假一切-进化酒馆-muliao）。
# 它只能作 cwd 缺失时的兜底线索，无法无损还原（'-' 有歧义），故只做展示。
def _project_dir_hint(dir_name: str) -> str:
    return str(dir_name or "").replace("--", ":\\").replace("-", "\\")


def ai_sessions(limit: int = 30, max_files: int = 200) -> dict:
    """列出本机 AI Agent（Claude Code / Codex）的**会话**及其真实项目路径。

    这是「通知溯源」的关键：一条 Agent 通知只带应用名和标题，但它的会话日志里
    记着 cwd（项目文件夹绝对路径）、git 分支、最后活动时间。有了这些，幕僚就能
    从「哪个 Agent 发的通知」直接定位到「哪个项目的哪个会话」，再去读那个文件夹
    或操作那个 Agent 窗口——不必猜文件位置。

    只读元数据（路径/时间/分支/首条用户消息前 80 字），不读文件内容。
    """
    if not permissions.is_granted("ai_logs"):
        return {"granted": False, "items": [], "count": 0, "note": "未授权 AI 日志访问"}
    home = os.path.expanduser("~")
    sessions: list[dict] = []
    files_seen = 0
    for tool, (rel,) in _AI_LOG_DIRS.items():
        root = os.path.join(home, rel)
        for fp in _walk_jsonl(root, max_files):
            files_seen += 1
            cwd = ""
            branch = ""
            sid = ""
            first_user = ""
            n_msgs = 0
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
                        if not cwd and isinstance(o.get("cwd"), str):
                            cwd = o["cwd"].strip()
                        if not branch and isinstance(o.get("gitBranch"), str):
                            branch = o["gitBranch"].strip()
                        if not sid and isinstance(o.get("sessionId"), str):
                            sid = o["sessionId"].strip()
                        t = _extract_msg(o)
                        if t:
                            n_msgs += 1
                            if not first_user and t[0] == "user":
                                first_user = t[1][:80]
            except Exception:
                continue
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = 0
            sid = sid or os.path.basename(fp)[:36]
            sessions.append({
                "tool": tool,
                "session_id": sid,
                "cwd": cwd,
                # 项目名：cwd 的最后一段；cwd 缺失时用目录名编码兜底。
                "project": (os.path.basename(cwd.rstrip("\\/")) if cwd
                            else _project_dir_hint(os.path.basename(os.path.dirname(fp)))[:40]),
                "git_branch": branch,
                "last_activity": mtime,
                "message_count": n_msgs,
                "first_user_message": first_user,
                "log_file": fp,
                "cwd_exists": bool(cwd) and os.path.isdir(cwd),
            })
    sessions.sort(key=lambda s: s.get("last_activity") or 0, reverse=True)
    return {"granted": True, "items": sessions[:limit], "count": len(sessions[:limit]),
            "files_seen": files_seen,
            "tools_found": [t for t, (r,) in _AI_LOG_DIRS.items()
                            if os.path.isdir(os.path.join(home, r))]}


# ============ 通知 → Agent 会话 溯源 ============
# 为什么需要推断：Windows toast 通知只带 app_name/title/body/ts，**不带 session_id
# 或项目路径**（实测 Claude/Codex/ZCode 的通知 launch 字段均为空）。所以「这条通知
# 是哪个 Agent、哪个对话发出的」只能靠三个信号加权推断：
#   ① 应用归属（app_name → 工具）
#   ② 时间邻近（通知发出前最近活跃的那个会话）
#   ③ 正文重叠（通知文本与会话消息/项目名的词面重合度）
# 推断结果带 confidence 与 reason，让上层能判断该不该信。

# 通知应用名（小写子串）→ _AI_LOG_DIRS 里的工具名。
# 一个工具可能对应多个应用名（桌面版/CLI/第三方壳）。
_AGENT_APP_HINTS: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("openai.codex", "codex"),
    ("zcode", "zcode"),
    ("cursor", "cursor"),
    ("windsurf", "windsurf"),
    ("gemini", "gemini"),
    ("copilot", "copilot"),
    ("aider", "aider"),
)

# 参与重叠度计算的停用词（中英文），避免「的/了/请/帮我」这类高频词虚增分数。
_PROVENANCE_STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "请", "帮", "我", "你", "这", "那", "有", "就",
    "都", "也", "还", "要", "会", "可以", "一个", "什么", "怎么", "现在", "已经",
    "the", "a", "an", "is", "are", "was", "to", "of", "in", "and", "or", "for",
    "on", "with", "this", "that", "it", "as", "at", "by", "be",
})


def _agent_tool_for_app(app_name: str, app_id: str) -> str | None:
    """把通知的应用标识映射到日志工具名；不认识则返回 None。"""
    blob = f"{app_name} {app_id}".lower()
    for hint, tool in _AGENT_APP_HINTS:
        if hint in blob:
            return tool
    return None


def _tokenize(text: str, limit: int = 80) -> set[str]:
    """粗分词：中文按 2-gram，英文按单词，去停用词。够做重叠度打分即可。"""
    raw = re.sub(r"[^\w一-鿿]+", " ", str(text or "").lower())
    tokens: set[str] = set()
    for word in raw.split():
        if word in _PROVENANCE_STOPWORDS or len(word) < 2:
            continue
        if re.match(r"^[a-z0-9_]+$", word):
            tokens.add(word)
        else:  # 含中文：取 2-gram，比单字更有区分度
            for i in range(len(word) - 1):
                gram = word[i:i + 2]
                if gram not in _PROVENANCE_STOPWORDS:
                    tokens.add(gram)
        if len(tokens) >= limit:
            break
    return tokens


def _overlap(a: set[str], b: set[str]) -> float:
    """两个词集的重叠度：交并比，落在 0..1。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def _session_corpus(session: Mapping[str, Any]) -> str:
    """把一个会话里可用于匹配的文本拼起来（项目名 + 分支 + 首条用户消息）。

    注意：这里只用 ai_sessions 已经加载的摘要字段，不重新读日志文件，
    所以溯源的开销与 ai_sessions 同量级。
    """
    return " ".join(str(session.get(k) or "") for k in
                    ("project", "cwd", "git_branch", "first_user_message"))


def agent_provenance(limit: int = 8, window_seconds: float = 7200.0,
                     max_sessions: int = 60) -> dict:
    """把最近的 Agent 通知关联到最可能的 Agent 会话（含真实项目路径）。

    返回每条通知的候选会话与置信度。这是「幕僚读到一条 Agent 通知后，定位到是哪个
    Agent、哪个对话、哪个文件夹」的关键一步——定位到 cwd 之后才能去读那个项目的
    文件、或把纠正指令发回那个 Agent，而不必猜路径。

    只读，不碰任何写操作；需要 notifications + ai_logs 两个 scope。
    """
    if not permissions.is_granted("notifications"):
        return {"granted": False, "items": [], "count": 0,
                "note": "未授权通知访问"}
    if not permissions.is_granted("ai_logs"):
        return {"granted": False, "items": [], "count": 0,
                "note": "未授权 AI 日志访问（溯源需要它来定位会话）"}

    notes = notifications(limit=max(limit * 3, 24)).get("items") or []
    sessions_doc = ai_sessions(limit=max_sessions, max_files=240)
    sessions = sessions_doc.get("items") or []

    out: list[dict] = []
    for n in notes:
        app_name = str(n.get("app_name") or "")
        app_id = str(n.get("app") or "")
        tool = _agent_tool_for_app(app_name, app_id)
        title = str(n.get("title") or "")
        body = str(n.get("body") or "")
        ts = float(n.get("ts") or 0.0)
        entry: dict[str, Any] = {
            "notification_id": n.get("id"),
            "app_name": app_name or app_id,
            "title": title[:120],
            "body": body[:300],
            "ts": ts,
            "agent_tool": tool,
            "is_agent": bool(tool),
            "launch": str(n.get("launch") or "")[:120],
            "match": None,
        }
        if not tool:
            # 非 Agent 应用（微信/邮件/系统安全等）：不参与溯源，但要如实说明。
            entry["reason"] = "非 AI Agent 应用，无需溯源"
            out.append(entry)
            continue
        if not sessions:
            entry["reason"] = "本机没有可用的 Agent 会话日志"
            out.append(entry)
            continue

        n_tokens = _tokenize(f"{title} {body}")
        candidates = [s for s in sessions if str(s.get("tool") or "") == tool] or sessions
        scored: list[tuple[float, dict, list[str]]] = []
        for s in candidates:
            last = float(s.get("last_activity") or 0.0)
            # 时间邻近度：通知通常在会话活跃期间或刚结束时发出。
            # 取「通知前 window 内」的会话；晚于通知的会话只轻微惩罚（时钟漂移）。
            delta = ts - last
            if delta >= 0:
                recency = max(0.0, 1.0 - min(delta, window_seconds) / window_seconds)
            else:
                recency = max(0.0, 0.35 + 0.65 * (delta / 300.0))
            overlap = _overlap(n_tokens, _tokenize(_session_corpus(s)))
            score = round(0.55 * recency + 0.45 * overlap, 4)
            reasons: list[str] = []
            if recency > 0:
                reasons.append(f"时间邻近(约{abs(int(delta))}秒前活跃)")
            if overlap > 0:
                reasons.append(f"正文与该项目文本重叠{overlap:.2f}")
            scored.append((score, s, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best, best_reasons = scored[0]
        # 阈值：低于此分只给候选、不下结论，避免误导模型去改错的项目。
        confident = best_score >= 0.28
        entry["match"] = {
            "session_id": best.get("session_id"),
            "tool": best.get("tool"),
            "project": best.get("project"),
            "cwd": best.get("cwd"),
            "cwd_exists": best.get("cwd_exists"),
            "git_branch": best.get("git_branch"),
            "log_file": best.get("log_file"),
            "score": best_score,
            "confident": confident,
            "reasons": best_reasons or ["仅按工具归属与最近活跃推断"],
            "alternatives": [
                {"project": s.get("project"), "cwd": s.get("cwd"),
                 "session_id": s.get("session_id"), "score": sc}
                for sc, s, _ in scored[1:4]
            ],
        }
        if not confident:
            entry["reason"] = ("置信度不足：通知正文与会话内容重合度低，"
                               "请让用户确认是哪个项目，不要据此改动文件")
        out.append(entry)

    # 只保留 Agent 通知在前，便于模型优先处理
    out.sort(key=lambda e: (not e.get("is_agent"), -(e.get("ts") or 0)))
    return {"granted": True, "items": out[:limit], "count": len(out[:limit]),
            "sessions_scanned": len(sessions),
            "window_seconds": window_seconds,
            "note": ("match.cwd 是该项目文件夹的绝对路径；confidence 为 False 时必须先"
                     "向用户确认，不要据此修改文件。")}


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


# ============ 会话级「完整上下文」还原 ============
# _extract_msg 只取 text 块，会把 Agent 真正做过的事全部丢掉。实测三个真实会话里
# 有 6614 条 tool_use、6613 条 tool_result、2380 条 thinking，而 text 只有 4474 条——
# 也就是说「它调了什么工具、工具返回了什么证据、它怎么想的」一条都看不到。
# 判断一个外部 Agent 有没有跑偏，恰恰要看这些证据，而不是只看它的结论性发言。
# 下面把一条记录展开成有序的「事件」，保留角色、思考、工具调用与工具结果。

# 单个文本片段保留长度：够判断意图与证据，又不至于把整条日志灌进上下文。
_CTX_TEXT_LIMIT = 1200
_CTX_TOOL_RESULT_LIMIT = 1500


def _clip(text: Any, limit: int) -> str:
    s = str(text if text is not None else "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"…(截断，原长 {len(s)})"


def _flatten_tool_content(value: Any) -> str:
    """tool_result 的 content 可能是 str，也可能是 [{type:text,...}] 之类的块列表。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if isinstance(value.get("text"), str):
            return value["text"]
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "image":
                    parts.append("[image omitted]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str)[:400])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(value)


def _extract_events(o: dict) -> list[dict]:
    """把一条 jsonl 记录展开成 0..N 个结构化事件（不丢工具调用与工具结果）。

    事件形状统一为 {kind, role?, text?, tool?, input?, result?, ...}，
    kind ∈ user / assistant / thinking / tool_use / tool_result / meta。
    """
    events: list[dict] = []
    typ = o.get("type")
    msg = o.get("message") if isinstance(o.get("message"), Mapping) else {}

    if typ in ("user", "assistant") and msg:
        role = str(msg.get("role") or typ)
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                events.append({"kind": role, "text": _clip(content, _CTX_TEXT_LIMIT)})
        elif isinstance(content, (list, tuple)):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                btype = str(block.get("type") or "")
                if btype == "text":
                    if str(block.get("text") or "").strip():
                        events.append({"kind": role,
                                       "text": _clip(block.get("text"), _CTX_TEXT_LIMIT)})
                elif btype == "thinking":
                    if str(block.get("thinking") or block.get("text") or "").strip():
                        events.append({"kind": "thinking", "role": role,
                                       "text": _clip(block.get("thinking") or block.get("text"),
                                                     _CTX_TEXT_LIMIT)})
                elif btype in ("tool_use", "server_tool_use"):
                    events.append({
                        "kind": "tool_use", "role": role,
                        "tool": str(block.get("name") or ""),
                        "tool_use_id": str(block.get("id") or ""),
                        "input": _clip(json.dumps(block.get("input"), ensure_ascii=False,
                                                  default=str) if block.get("input") is not None
                                       else "", _CTX_TEXT_LIMIT),
                    })
                elif btype in ("tool_result", "web_search_tool_result"):
                    events.append({
                        "kind": "tool_result",
                        "tool_use_id": str(block.get("tool_use_id") or ""),
                        "is_error": bool(block.get("is_error")),
                        "result": _clip(_flatten_tool_content(block.get("content")),
                                        _CTX_TOOL_RESULT_LIMIT),
                    })
                elif btype == "image":
                    events.append({"kind": "meta", "text": "[image]"})
        return events

    # Codex 形态
    p = o.get("payload") if isinstance(o.get("payload"), Mapping) else {}
    if typ == "event_msg" and p.get("type") == "user_message" and isinstance(p.get("message"), str):
        events.append({"kind": "user", "text": _clip(p["message"], _CTX_TEXT_LIMIT)})
    elif typ == "response_item" and p.get("type") == "message" and isinstance(p.get("content"), list):
        texts = [str(x.get("text") or "") for x in p["content"] if isinstance(x, Mapping)]
        joined = "\n".join(t for t in texts if t.strip())
        if joined.strip():
            events.append({"kind": str(p.get("role") or "assistant"),
                           "text": _clip(joined, _CTX_TEXT_LIMIT)})
    elif typ == "response_item" and p.get("type") in ("function_call", "local_shell_call"):
        events.append({"kind": "tool_use", "tool": str(p.get("name") or p.get("action") or ""),
                       "input": _clip(str(p.get("arguments") or ""), _CTX_TEXT_LIMIT)})
    elif typ == "response_item" and p.get("type") == "function_call_output":
        events.append({"kind": "tool_result",
                       "result": _clip(_flatten_tool_content(p.get("output")),
                                       _CTX_TOOL_RESULT_LIMIT)})
    elif typ == "response_item" and p.get("type") == "reasoning":
        summary = p.get("summary") or p.get("content")
        if summary:
            events.append({"kind": "thinking",
                           "text": _clip(_flatten_tool_content(summary), _CTX_TEXT_LIMIT)})
    return events


def _find_session_log(session_id: str, max_files: int = 400) -> tuple[str, str] | None:
    """按 sessionId 字段（而非文件名）定位日志文件，返回 (tool, path)。

    不能只靠文件名：Claude 的 jsonl 文件名通常就是 sessionId，但 codex 是
    rollout-<时间>-<uuid> 形态，且子 agent 日志在 subagents/** 下。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    home = os.path.expanduser("~")
    # 先试文件名直配（最快路径），再退回逐文件扫 sessionId 字段。
    for tool, (rel,) in _AI_LOG_DIRS.items():
        root = os.path.join(home, rel)
        direct = os.path.join(root, sid + ".jsonl")
        if os.path.isfile(direct):
            return tool, direct
    for tool, (rel,) in _AI_LOG_DIRS.items():
        root = os.path.join(home, rel)
        for fp in _walk_jsonl(root, max_files):
            if sid in os.path.basename(fp):
                return tool, fp
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i > 40:      # sessionId 一般在前若干条元数据里
                            break
                        if sid in line:
                            return tool, fp
            except OSError:
                continue
    return None


def ai_session_detail(session_id: str, *, max_events: int = 400,
                      include_thinking: bool = False, tail: bool = True) -> dict:
    """还原一个 Agent 会话的**完整上下文**：用户指令、思考、工具调用与工具结果。

    这是「幕僚帮外部 Agent 纠偏」的数据地基：要判断它有没有跑偏，必须看到它
    最初接到的指令、它实际调了哪些工具、工具返回了什么证据——只看结论性发言
    是判断不了的。

    参数：
      max_events      —— 最多返回多少个事件（防超长会话灌爆上下文）。
      include_thinking —— 是否包含思考流。默认关：思考量大且多为自言自语，
                          纠偏判断靠「指令 + 工具证据 + 结论」就够。
      tail            —— True 取最近的事件（默认，纠偏要看最新进展）；
                          False 取最早的（要看最初指令时用）。
    """
    if not permissions.is_granted("ai_logs"):
        return {"granted": False, "events": [], "count": 0,
                "note": "未授权 AI 日志访问"}
    found = _find_session_log(session_id)
    if not found:
        return {"granted": True, "found": False, "events": [], "count": 0,
                "note": f"未找到 session_id={session_id!r} 的日志"}
    tool, fp = found

    events: list[dict] = []
    cwd = ""
    branch = ""
    first_user = ""
    stats = {"user": 0, "assistant": 0, "thinking": 0, "tool_use": 0, "tool_result": 0,
             "tool_errors": 0}
    tools_used: list[str] = []
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
                if not isinstance(o, Mapping):
                    continue
                if not cwd and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"].strip()
                if not branch and isinstance(o.get("gitBranch"), str):
                    branch = o["gitBranch"].strip()
                for ev in _extract_events(dict(o)):
                    kind = ev.get("kind")
                    if kind == "thinking" and not include_thinking:
                        stats["thinking"] += 1
                        continue
                    if kind in stats:
                        stats[kind] += 1
                    if kind == "tool_use" and ev.get("tool"):
                        if ev["tool"] not in tools_used:
                            tools_used.append(ev["tool"])
                    if kind == "tool_result" and ev.get("is_error"):
                        stats["tool_errors"] += 1
                    if kind == "user" and not first_user:
                        first_user = str(ev.get("text") or "")[:400]
                    ev["ts"] = _parse_ts(o.get("timestamp"))
                    events.append(ev)
    except OSError as exc:
        return {"granted": True, "found": True, "events": [], "count": 0,
                "error": type(exc).__name__}

    total = len(events)
    selected = events[-max_events:] if (tail and total > max_events) else events[:max_events]
    return {
        "granted": True, "found": True,
        "session_id": session_id, "tool": tool, "log_file": fp,
        "cwd": cwd, "project": os.path.basename(cwd.rstrip("\\/")) if cwd else "",
        "git_branch": branch,
        "first_user_message": first_user,     # 最初指令：判断跑偏的基准
        "stats": stats,
        "tools_used": tools_used[:20],
        "total_events": total,
        "returned_events": len(selected),
        "truncated": total > len(selected),
        "window": "tail" if tail else "head",
        "events": selected,
    }


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


# ============ 能力项：执行/设备类，独立于只读数据源 ============
def capabilities() -> list:
    """能力项（执行/设备类）的可用性 + 授权状态，供前端渲染独立开关。

    与 sources() 的七类只读数据源分开：能力默认关、不随「全部数据」授权打开，
    只能经 permissions.set_scope(scope, True) 逐项显式授权。本函数只探测可用性，
    绝不调用任何控制功能。每项都带 kind="capability" 以便前端区分能力区。
    """
    # computer_control：需 Windows + pywinauto；仅探测库是否可导入，不触碰控制逻辑。
    if sys.platform == "win32":
        try:
            import pywinauto  # noqa: F401
            cc_available = True
            cc_detail = "pywinauto 就绪（Windows）"
        except Exception:
            cc_available = False
            cc_detail = "需安装 pywinauto（pip install pywinauto）"
    else:
        cc_available = False
        cc_detail = "仅支持 Windows（当前平台 %s）" % sys.platform

    # voice_control：语音子系统已实装；就绪与否取决于本地 ASR 模型是否下载到位。
    voice_available, voice_detail = _voice_available()

    return [
        {
            "id": "computer_control", "name": "电脑控制",
            "desc": "允许幕僚聚焦/切换窗口、点击界面元素、输入文字、打开应用；高风险动作会先请你确认",
            "granted": permissions.is_granted("computer_control"),
            "available": cc_available,
            "detail": cc_detail,
            "note": "高风险动作会先请你确认",
            "sensitive": True,
            "kind": "capability",
        },
        {
            "id": "voice_control", "name": "语音控制",
            "desc": "允许幕僚听「幕僚幕僚」唤醒并执行语音指令（本地识别，不上传录音）；高风险动作会先请你确认",
            "granted": permissions.is_granted("voice_control"),
            "available": voice_available,
            "detail": voice_detail,
            "note": None if voice_available else "需先下载本地语音模型（见 detail）",
            "sensitive": True,
            "kind": "capability",
        },
        {
            "id": "file_content", "name": "读取文件内容",
            "desc": (
                "允许幕僚读取本机文本文件的**正文**（源码、配置、文档），"
                "以便看懂 Agent 报的问题再动手改。内容会进入上游模型上下文"
            ),
            "granted": permissions.is_granted("file_content"),
            "available": True,
            "detail": "按路径读文本；密钥/凭据类文件（config.json、SSH 私钥、.env）一律拒读",
            "note": "敏感度最高：正文会发给上游模型，仅在需要时开启",
            "sensitive": True,
            "kind": "capability",
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


def _voice_available() -> tuple[bool, str]:
    """语音能力是否就绪：voice 包可导入 + 本地 ASR 模型文件在位。

    只探测，绝不启动麦克风/加载模型（避免在渲染权限页时占用设备或耗内存）。
    缺模型时返回具体缺失项，让用户知道要下载什么。
    """
    try:
        import voice.api  # noqa: F401
    except Exception as exc:
        return False, "语音模块不可导入（%s）" % type(exc).__name__
    # 模型目录：与 voice.config 的默认解析保持一致（%PROGRAMDATA%\Muliao\models\sensevoice）
    try:
        from voice.config import VoiceSettings
        sdir = str(VoiceSettings.load().sensevoice_dir)
    except Exception:
        sdir = os.path.join(os.environ.get("PROGRAMDATA") or "C:/ProgramData",
                            "Muliao", "models", "sensevoice")
    need = ("model.int8.onnx", "tokens.txt")
    missing = [f for f in need if not os.path.isfile(os.path.join(sdir, f))]
    if missing:
        return False, "缺本地语音模型：%s（应放在 %s）" % ("、".join(missing), sdir)
    return True, "语音就绪（%s）" % sdir


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
