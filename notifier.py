"""跨平台系统通知抓取 · 统一适配层

设计原则：
  1) 只读、零侵入——不改系统设置、不注入进程、不需要管理员权限。
  2) 统一输出 schema，平台差异关在各自 adapter 里。
  3) 每个 adapter 自报 capability：ready / unavailable / needs_setup，
     未就绪的平台如实报告原因，绝不伪造数据。

统一通知 schema：
  {
    "id":       "win:5350",       # 平台前缀 + 平台内唯一 id（用于增量拉取）
    "platform": "windows",
    "ts":       1758543600.0,     # epoch 秒；取不到时为 0（排序退化到 id）
    "app":      "Windows.SystemToast.BackgroundAccess",
    "app_name": "背景访问",        # 人类可读名（尽力而为）
    "title":    "节能模式已开启",
    "body":     "",
    "kind":     "toast",          # toast / alert / system / behavior
    "raw_len":  198,
  }
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from typing import Any

PLATFORM = sys.platform  # 'win32' / 'darwin' / 'linux'


# ============ 通用工具 ============
def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


# URL scheme → 应用名（最可靠的应用识别来源：toast 的 launch 属性）
# 比 AUMID/GUID 可靠：很多桌面应用注册的是 GUID，但 launch scheme 是产品自己的标识。
_SCHEME_APP = {
    "weixin": "微信", "wechat": "微信",
    "sslocal": "抖音", "snssdk1128": "抖音", "aweme": "抖音",
    "mqq": "QQ", "qqim": "QQ", "tim": "TIM",
    "dingtalk": "钉钉", "alipays": "支付宝", "alipay": "支付宝",
    "tg": "Telegram", "whatsapp": "WhatsApp", "feishu": "飞书",
    "lark": "飞书", "wxwork": "企业微信", "weixinwork": "企业微信",
    "bilibili": "哔哩哔哩", "xhsdiscover": "小红书", "zhihu": "知乎",
    "netease-cloudmusic": "网易云音乐", "qqmusic": "QQ音乐",
    "microsoft-edge": "Edge", "ms-settings": "系统设置",
}


def _app_from_launch(launch: str) -> str | None:
    """从 toast 的 launch 属性提取应用名。launch 形如 'action=weixin://...' 或 'weixin://...'。

    实测修正：Windows 的 ms-settings 等 URI 用 `scheme:path`（**不带 //**，如
    `ms-settings:batterysaver`），故这里同时接受 `scheme://` 与 `scheme:` 两种写法，
    否则 `ms-settings` 虽在映射表里也匹配不上（实测 wpndatabase.db Id=5350 漏判）。
    """
    if not launch:
        return None
    m = re.search(r"([a-z][a-z0-9+\-.]{1,40}):(//)?", launch.lower())
    if m and m.group(1) in _SCHEME_APP:
        return _SCHEME_APP[m.group(1)]
    return None



def _toast_texts(xml_bytes: bytes) -> tuple[str, str, list[str], str]:
    """解析 Windows toast XML，返回 (title, body, 全部 text 节点, launch 属性)。"""
    try:
        s = xml_bytes.decode("utf-8", errors="replace") if isinstance(xml_bytes, bytes) else str(xml_bytes)
    except Exception:
        return "", "", [], ""
    launch = ""
    m = re.search(r'launch="([^"]*)"', s)
    if m:
        launch = m.group(1)
    # 优先走 XML 解析；失败则退回正则（部分 payload 含非法字符）
    try:
        root = ET.fromstring(s)
        texts = [_text(t) for t in root.iter("text")]
        texts = [t for t in texts if t]
        attrs = root.attrib
        launch = launch or attrs.get("launch", "")
        if texts:
            return texts[0], " ".join(texts[1:])[:600], texts, launch
        return attrs.get("launch", "")[:120], "", [], launch
    except ET.ParseError:
        got = re.findall(r"<text[^>]*>([^<]*)</text>", s)
        got = [g.strip() for g in got if g.strip()]
        if got:
            return got[0], " ".join(got[1:])[:600], got, launch
        return "", "", [], launch


# 常见 AUMID → 中文可读名（尽力而为，未命中则原样返回）
# 下面标注 [实测] 的条目来自本机 wpndatabase.db 的真实 PrimaryId（2026-09 抓取），
# 这些 UWP 包名经 _pretty_app 规整后会退化成厂商内部名（如 windowscommunicationsapps），
# 故按**完整 PrimaryId** 精确映射到可读名。完整串在 _pretty_app 里最先查，优先于任何规整。
_APP_NAME_CN = {
    "Microsoft.SkyDrive.Desktop": "OneDrive",
    "Microsoft.Windows.Store": "Microsoft Store",
    "Microsoft.MicrosoftEdge.Stable": "Edge",
    "Microsoft.Windows.Terminal": "Windows 终端",
    "Windows.SystemToast.BackgroundAccess": "系统·后台访问",
    "Windows.SystemToast.BatterySaver": "系统·省电模式",
    "Windows.SystemToast.SecurityAndMaintenance": "系统·安全与维护",
    "Windows.SystemToast.WiFiManager": "系统·网络",
    "Windows.SystemToast.Volume": "系统·音量",
    "Windows.SystemToast.Power": "系统·电源",
    "Windows.SystemToast.Update": "系统·更新",
    "Windows.Defender": "Windows 安全中心",
    "Microsoft.Windows.UpdateOrchestrator": "Windows 更新",
    "dev.zcode.app": "ZCode",
    "Microsoft.WindowsExplorer": "文件资源管理器",
    # [实测] 邮件/日历共用 windowscommunicationsapps 包，靠 EntryPoint 区分：
    "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.mail": "邮件",
    "microsoft.windowscommunicationsapps_8wekyb3d8bbwe!microsoft.windowslive.calendar": "日历",
    # [实测] WebExperience 的 Widgets 入口 = 任务栏小组件：
    "MicrosoftWindows.Client.WebExperience_cw5n1h2txyewy!Widgets": "小组件",
    # Android 包名 → 可读名（点分包名取尾段会得到 mm/aweme 之类不可读结果，故精确映射）。
    # 这些是业界公认的固定包名，**非猜测**；但因本机无 Android 设备，未在真机核对过，标注于此。
    "com.tencent.mm": "微信", "com.tencent.mobileqq": "QQ", "com.tencent.tim": "TIM",
    "com.tencent.qqmail": "QQ邮箱", "com.tencent.wework": "企业微信",
    "com.ss.android.ugc.aweme": "抖音", "com.ss.android.article.news": "今日头条",
    "com.smile.gifmaker": "快手", "com.xingin.xhs": "小红书", "com.zhihu.android": "知乎",
    "tv.danmaku.bili": "哔哩哔哩", "com.bilibili.app.in": "哔哩哔哩",
    "com.netease.cloudmusic": "网易云音乐", "com.tencent.qqmusic": "QQ音乐",
    "com.alibaba.android.rimet": "钉钉", "com.eg.android.AlipayGphone": "支付宝",
    "com.taobao.taobao": "淘宝", "com.jingdong.app.mall": "京东",
    "com.sankuai.meituan": "美团", "com.dianping.v1": "大众点评",
    "com.autonavi.minimap": "高德地图", "com.baidu.BaiduMap": "百度地图",
    "com.android.chrome": "Chrome", "com.android.vending": "Play 商店",
    "org.telegram.messenger": "Telegram", "com.whatsapp": "WhatsApp",
    "com.spotify.music": "Spotify", "com.instagram.android": "Instagram",
    "com.facebook.katana": "Facebook", "com.twitter.android": "Twitter/X",
}


# AUMID 里常见的随机后缀模式：包名_hash!EntryPoint 或 Name_xxxx
_PKG_TAIL = re.compile(r"_[0-9a-z]{8,}$", re.I)


def _pretty_app(primary_id: str) -> str:
    if not primary_id:
        return "未知来源"
    if primary_id in _APP_NAME_CN:
        return _APP_NAME_CN[primary_id]
    # 先规整：去掉 UWP 的 !EntryPoint 与包哈希后缀（Codex_2p2nqsd0c7 → Codex）
    pid = primary_id.split("!")[0]
    pid = _PKG_TAIL.sub("", pid)
    # 规整后再查一次映射表（如 Microsoft.MicrosoftEdge.Stable_hash → Edge）
    if pid in _APP_NAME_CN:
        return _APP_NAME_CN[pid]
    # 去掉厂商前缀，取最后一段作为可读名
    tail = pid.rsplit(".", 1)[-1]
    return tail or pid or primary_id


# ============ Windows：wpndatabase.db（只读） ============
# 路径：%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db
# 关键表：Notification(Id, HandlerId, Type, Payload, [Order], ArrivalTime, ExpiryTime)
#         + NotificationHandler(RecordId, PrimaryId, HandlerType, SystemDataPropertySet)
# 用 SQLite `mode=ro` 只读打开：不会写库，同时可读取仍在 WAL 中尚未 checkpoint 的最新通知。
#
# 【实测修正 2026-09】原代码注释称「该表无可靠时间列」——错误。PRAGMA table_info(Notification)
# 实测确有 ArrivalTime(INT64)，本机 33/33 行均非空，值为 Windows FILETIME（自 1601-01-01 起
# 的 100ns 间隔）。已据此把 ts 从恒为 0.0 改为真实 epoch 秒，通知面板可正确显示时间。
_WIN_FILETIME_EPOCH_DELTA = 116444736000000000  # 1601→1970 的 100ns 间隔数


def _win_filetime_to_epoch(ft: Any) -> float:
    """Windows FILETIME（100ns since 1601-01-01 UTC）→ epoch 秒。非法/空值返回 0.0。"""
    try:
        ft = int(ft)
    except (TypeError, ValueError):
        return 0.0
    if ft <= 0:
        return 0.0
    sec = (ft - _WIN_FILETIME_EPOCH_DELTA) / 10_000_000
    # 合理性夹取：落在 1970–2200 之外视为脏数据
    if sec < 0 or sec > 7_258_118_400:
        return 0.0
    return sec


def _win_db_path() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "Microsoft", "Windows", "Notifications", "wpndatabase.db",
    )


class WindowsAdapter:
    platform = "windows"

    def __init__(self) -> None:
        self.path = _win_db_path()

    def capability(self) -> dict[str, Any]:
        ok = os.path.isfile(self.path)
        return {
            "platform": self.platform,
            "ready": ok,
            "source": self.path if ok else None,
            "method": "只读 SQLite（wpndatabase.db）+ toast XML 解析",
            "note": None if ok else "未找到通知数据库（可能非 Windows 或通知功能被禁用）",
        }

    def fetch(self, since_id: str = "", limit: int = 200) -> list[dict]:
        if not os.path.isfile(self.path):
            return []
        since = 0
        if since_id.startswith("win:"):
            try:
                since = int(since_id.split(":", 1)[1])
            except ValueError:
                since = 0
        uri = f"file:{self.path}?mode=ro"
        out: list[dict] = []
        try:
            con = sqlite3.connect(uri, uri=True, timeout=3.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # ArrivalTime 实测存在且可靠（见上方注释），一并取出用于 ts。
            rows = cur.execute(
                """SELECT n.Id AS nid, n.Type AS ntype, n.Payload AS payload,
                          n.ArrivalTime AS arrival, h.PrimaryId AS pid
                     FROM Notification n
                LEFT JOIN NotificationHandler h ON h.RecordId = n.HandlerId
                 ORDER BY n.[Order] DESC
                    LIMIT ?""",
                (max(limit * 3, 300),),
            ).fetchall()
            con.close()
        except Exception:
            return []
        for r in rows:
            # 单行解析失败不连累整批（脏 Payload / 异常类型一律跳过）
            try:
                nid = int(r["nid"] or 0)
                if since and nid <= since:
                    continue
                payload = r["payload"] or b""
                title, body, _all, launch = _toast_texts(payload)
                pid = r["pid"] or ""
                # 应用名识别优先级：launch scheme（最可靠）> AUMID 映射表 > AUMID 原值
                from_launch = _app_from_launch(launch)
                if from_launch:
                    app_name = from_launch
                else:
                    app_name = _pretty_app(pid)
                # 纯 GUID 的 AUMID 不可读，标注为「桌面应用」而非暴露 GUID
                if re.fullmatch(r"\{[0-9A-Fa-f\-]{36}\}", pid or "") and not from_launch:
                    app_name = "桌面应用"
                out.append({
                    "id": f"win:{nid}",
                    "platform": self.platform,
                    # 实测：ArrivalTime 是 Windows FILETIME，转 epoch 秒；脏值退回 0.0
                    "ts": _win_filetime_to_epoch(r["arrival"]),
                    "app": pid,
                    "app_name": app_name,
                    "title": title or "(无标题)",
                    "body": body,
                    "kind": str(r["ntype"] or "toast"),
                    "launch": (launch or "")[:200],
                    "raw_len": len(payload) if isinstance(payload, bytes) else 0,
                })
            except Exception:
                continue
        # 排序：先按时间（新→旧），同时间再按 id 数值，保证稳定且与 fetch_all 一致
        out.sort(key=lambda n: (n["ts"], int(n["id"].split(":")[1])), reverse=True)
        return out[:limit]


# ============ macOS：usernoted db2（只读） ============
# 解析 BLOB 需要 plistlib + 苹果的 Notification 结构。原则：「能读就读、读不到如实报
# unavailable，绝不猜字段、绝不伪造」。下面每一项都标了核实来源与是否真机验证过。
#
# 【核实结论 2026-09，三处权威来源交叉印证】
#   1) 路径：macOS 26+(Sequoia) 在 ~/Library/Group Containers/group.com.apple.usernoted/db2/db；
#      High Sierra..更早 在 $DARWIN_USER_DIR/0/com.apple.notificationcenter/db2/db。
#      （来源：blacktop/ipsw pkg/notif/notif.go、ydkhatri/mac_apt notifications.py）
#   2) 表名 `record`、列 `data`(二进制 plist)、`delivered_date`、`app_id` 均确认存在。
#      应用 bundle id 的**可靠来源是 SQL JOIN**：record.app_id → app.app_id，取 app.identifier。
#      plist 内部的 `app` 键是个**字符串**（不是 {bundle-id: ...} 字典）——原代码
#      `(p.get("app") or {}).get("bundle-id")` 是错的，已修正。
#      （来源：ipsw `SELECT app.identifier, record.delivered_date, record.data FROM record
#        LEFT JOIN app ON record.app_id = app.app_id`；mac_apt 同款 JOIN）
#   3) plist 文本字段：req['titl'] / req['subt'] / req['body']，键名是 4 字符缩写。
#      值**可能是 str，也可能是 list**（mac_apt RemoveTabsNewLines 对 list 取第 0 项）。
#   4) delivered_date 是 Cocoa epoch（2001-01-01 = Unix 978307200）。但 mac_apt 指出：
#      High Sierra 起部分值是**纳秒**（abs>0xFFFFFFFF 时除以 1e9），否则按秒。已照此处理。
#   5) ⚠️ TCC 限制（ipsw 源码注释 + objective-see 博客印证）：该库在 macOS 上受 TCC 保护，
#      **stat() 能成功但 open() 会被拒**，除非给运行进程授予「完全磁盘访问」(Full Disk Access)。
#      因此 capability() 不能只看 isfile（那会误报 ready），必须真正 open 探测一次。
#      若拿不到 FDA，本 adapter 在 macOS 上实际不可用——capability 会如实标注 needs_setup。
# 仍未验证：以上全部基于权威工具源码与文档，**未在 macOS 真机跑过**（开发机是 Windows）。
def _mac_db_paths() -> list[str]:
    """候选路径，新版在前。Sequoia 走 group container；旧版走 DARWIN_USER_DIR。"""
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Library/Group Containers/group.com.apple.usernoted/db2/db"),
    ]
    # DARWIN_USER_DIR 形如 /var/folders/xx/yyyy/T/；通知库在其上一级的 0/com.apple.notificationcenter/db2/db
    darwin_dir = os.environ.get("DARWIN_USER_DIR")
    if darwin_dir:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(darwin_dir.rstrip("/"))),
                                  "0/com.apple.notificationcenter/db2/db"))
    import tempfile
    tmp = tempfile.gettempdir()
    cands.append(os.path.join(os.path.dirname(os.path.abspath(tmp)),
                              "0/com.apple.notificationcenter/db2/db"))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# macOS 通知时间是自 2001-01-01 起的秒数（Cocoa epoch）
_MAC_EPOCH = 978307200.0


def _mac_delivered_to_epoch(dd: Any) -> float:
    """delivered_date → Unix epoch 秒。处理秒/纳秒两种精度（mac_apt 同源逻辑）。"""
    try:
        v = float(dd)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    if abs(v) > 0xFFFFFFFF:   # 超过 32 位 → 纳秒精度（High Sierra 起偶见）
        v = v / 1e9
    return v + _MAC_EPOCH


def _mac_req_text(val: Any) -> str:
    """plist 文本字段可能是 str，也可能是 list（mac_apt 对 list 取首项）；统一成 str。"""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8", "replace")
        except Exception:
            return ""
    return str(val)


class MacOSAdapter:
    platform = "macos"

    def __init__(self) -> None:
        self.paths = _mac_db_paths()
        self.path = next((p for p in self.paths if os.path.isfile(p)), self.paths[0])
        # 探测结果缓存，供 capability/fetch 共用（避免重复 open）
        self._probe: dict[str, Any] | None = None

    def _probe_db(self) -> dict[str, Any]:
        """真正 open 一次（TCC 下 stat 成功≠可读）。返回 {found, readable, path, err}。"""
        if self._probe is not None:
            return self._probe
        res = {"found": False, "readable": False, "path": None, "err": None}
        for p in self.paths:
            if not os.path.isfile(p):
                continue
            res["found"] = True
            res["path"] = p
            try:
                # 先按 TCC 行为裸 open 一次：能 open 说明有 FDA；EPERM/EACCES 说明被 TCC 拦
                with open(p, "rb"):
                    pass
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3.0)
                con.execute("SELECT count(*) FROM record").fetchone()
                con.close()
                res["readable"] = True
                self.path = p
                break
            except PermissionError as e:
                res["err"] = f"tcc_denied:{e}"
            except OSError as e:
                res["err"] = f"open_failed:{e}"
            except Exception as e:  # sqlite 打不开/表结构不符
                res["err"] = f"db_failed:{e}"
        self._probe = res
        return res

    def capability(self) -> dict[str, Any]:
        pr = self._probe_db()
        base = {
            "platform": self.platform,
            "method": "只读 SQLite（usernoted/db2 record 表）+ 二进制 plist 解析",
            "verified": "结构已据 ipsw/mac_apt 源码核实；未在 macOS 真机验证",
        }
        if pr["readable"]:
            return {**base, "ready": True, "source": pr["path"], "note": None,
                    "needs_setup": False}
        if pr["found"] and str(pr.get("err", "")).startswith("tcc_denied"):
            return {**base, "ready": False, "source": pr["path"],
                    "note": ("找到通知库但被 TCC 拒绝读取（EPERM）。需在「系统设置 → 隐私与安全性 → "
                             "完全磁盘访问」里，给运行本程序的终端/进程授权后重启。未授权则本 adapter 不可用。"),
                    "needs_setup": True}
        if pr["found"]:
            return {**base, "ready": False, "source": pr["path"],
                    "note": f"找到通知库但无法读取：{pr.get('err')}", "needs_setup": True}
        return {**base, "ready": False, "source": None,
                "note": ("未找到 usernoted 数据库。可能非 macOS，或系统版本不同导致路径不同。"
                         "（注意：即便文件存在，macOS 也需「完全磁盘访问」才能读，未授权则不可用。）"),
                "needs_setup": True}

    def fetch(self, since_id: str = "", limit: int = 200) -> list[dict]:
        pr = self._probe_db()
        if not pr["readable"]:
            return []          # 读不了就如实返回空，绝不伪造
        since = 0
        if since_id.startswith("mac:"):
            try:
                since = int(since_id.split(":", 1)[1])
            except ValueError:
                since = 0
        try:
            import plistlib
        except Exception:
            return []
        out: list[dict] = []
        try:
            con = sqlite3.connect(f"file:{pr['path']}?mode=ro", uri=True, timeout=3.0)
            con.row_factory = sqlite3.Row
            # 首选：JOIN app 表取 bundle id（核实过的可靠来源），按 rec_id 排序。
            # rec_id / app_id / app.identifier 任一列缺失时回退到无 JOIN 的简查询。
            try:
                rows = con.execute(
                    """SELECT record.rec_id AS rid, app.identifier AS app_id_str,
                              record.data AS data, record.delivered_date AS dd
                         FROM record LEFT JOIN app ON record.app_id = app.app_id
                        ORDER BY record.rec_id DESC LIMIT ?""",
                    (max(limit * 3, 300),),
                ).fetchall()
            except Exception:
                rows = con.execute(
                    "SELECT rowid AS rid, data, delivered_date AS dd FROM record "
                    "ORDER BY rowid DESC LIMIT ?",
                    (max(limit * 3, 300),),
                ).fetchall()
            con.close()
        except Exception:
            return []
        for r in rows:
            try:
                rid = int(r["rid"] or 0)
                if since and rid <= since:
                    continue
                blob = r["data"]
                title, body, app = "", "", ""
                # bundle id 优先取 SQL JOIN 出来的 app.identifier（核实过）
                try:
                    app = str(r["app_id_str"] or "")
                except Exception:
                    app = ""
                if blob:
                    try:
                        p = plistlib.loads(bytes(blob))
                        if isinstance(p, dict):
                            req = p.get("req") if isinstance(p.get("req"), dict) else {}
                            title = _mac_req_text(req.get("titl")) or _mac_req_text(p.get("titl"))
                            body = _mac_req_text(req.get("body")) or _mac_req_text(p.get("body"))
                            # plist 内的 app 键是字符串（不是 {bundle-id:...} 字典）——核实修正
                            if not app:
                                pa = p.get("app")
                                app = _mac_req_text(pa) if not isinstance(pa, dict) else _mac_req_text(pa.get("bundle-id"))
                    except Exception:
                        pass
                ts = _mac_delivered_to_epoch(r["dd"])
                out.append({
                    "id": f"mac:{rid}", "platform": self.platform, "ts": ts,
                    "app": app, "app_name": _pretty_app(app) if app else "未知来源",
                    "title": (title or "(无标题)"), "body": body[:600],
                    "kind": "alert", "raw_len": len(blob) if blob else 0,
                })
            except Exception:
                continue
        out.sort(key=lambda n: (n["ts"], int(n["id"].split(":")[1])), reverse=True)
        return out[:limit]


# ============ Linux：DBus 监听 + 本地落盘 ============
# freedesktop 规范里「监听别人的通知」需要充当 notification server（会抢走系统通知），
# 属于侵入式，默认不启用。这里实现的是零侵入路径：
#   读取本面板自己记录的 journal（notifier_journal.jsonl，由 /api/notify 写入），
#   并尝试读取常见桌面环境的通知历史文件（若存在）。
# 因此 Linux 下 capability 如实报告 ready=False + needs_setup 说明。
_LINUX_JOURNAL = os.path.join(os.path.expanduser("~"), ".muliao", "notifier_journal.jsonl")


class LinuxAdapter:
    platform = "linux"

    def __init__(self) -> None:
        self.journal = _LINUX_JOURNAL

    def capability(self) -> dict[str, Any]:
        import shutil
        has_dbus = shutil.which("dbus-monitor") is not None
        ok = os.path.isfile(self.journal)
        return {
            "platform": self.platform,
            "ready": ok,
            "source": self.journal if ok else None,
            "method": "本地 journal（由 notify-bridge 写入）",
            "note": None if ok else (
                "Linux 无统一只读通知库。零侵入做法是运行随附的 notify-bridge"
                "（dbus-monitor 抓 org.freedesktop.Notifications.Notify 并落盘）。"
                + ("" if has_dbus else " 当前未检测到 dbus-monitor。")
            ),
            "needs_setup": not ok,
            "dbus_available": has_dbus,
        }

    def fetch(self, since_id: str = "", limit: int = 200) -> list[dict]:
        if not os.path.isfile(self.journal):
            return []
        since = 0
        if since_id.startswith("linux:"):
            try:
                since = int(since_id.split(":", 1)[1])
            except ValueError:
                since = 0
        out: list[dict] = []
        try:
            import json as _json
            with open(self.journal, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = _json.loads(line)
                        if not isinstance(d, dict):
                            continue
                        nid = int(d.get("seq") or 0)
                        ts = float(d.get("ts") or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if since and nid <= since:
                        continue
                    out.append({
                        "id": f"linux:{nid}", "platform": self.platform,
                        "ts": ts,
                        "app": str(d.get("app") or ""), "app_name": _pretty_app(str(d.get("app") or "")),
                        "title": str(d.get("title") or "(无标题)"), "body": str(d.get("body") or "")[:600],
                        "kind": "alert", "raw_len": len(line),
                    })
        except Exception:
            return []
        out.sort(key=lambda n: int(n["id"].split(":")[1]), reverse=True)
        return out[:limit]


# ============ Android：ADB 桥（需连接设备） ============
# Android 通知被沙箱隔离，PC 侧只能经 adb 读取。**正规做法是设备端 APK +
# NotificationListenerService**（需用户在「设置 → 通知访问权限」里手动授权，无法静默）；
# 这里的 `adb shell dumpsys notification` 只是**兜底**：能读「当前仍在通知栏里」的通知，
# 读不到已划走的历史，且输出格式随 ROM/版本变化。未连接设备时如实报 needs_setup，不伪造。
#
# 【核实结论 2026-09，来源 AOSP 源码 Reginer/aosp-android-jar，API 31/33/34/35/36 一致】
#   · 记录块以 `NotificationRecord(0x%08x: pkg=%s user=%s id=%d tag=%s importance=%d key=%s: %s)`
#     开头（NotificationRecord.toString），**无编号前缀**。原代码用 `\n(?=\s*\d+:\s)` 切块——
#     实测该正则匹配不到任何 AOSP 记录，等于抓不到东西。已改为按 `NotificationRecord(` 切块。
#   · key 字段 = StatusBarNotification.getKey() = `userId|pkg|id|tag|uid`，**跨进程/跨次稳定**，
#     用它做 id 的来源（原代码用 abs(hash(b))，而 Python 字符串 hash 每进程随机化，
#     实测同一通知三次取到 601650175/328825576/921914102 三个不同 id → since_id 增量彻底失效）。
#   · 标题/正文在 extras 里，格式是 `android.title=<Type> (<值>)`（NotificationRecord.dumpNotification
#     对非数组非位图值打 `Type (String.valueOf(val))`）。原代码 `android\.title=([^,\n]+)` 会把
#     `String (你好)` 连类型和括号一起吞进去。已改为剥掉 `Type (`…`)` 外壳。
#   · ⚠️ 不加 `--noredact` 时，title/text 会被打码成 `android.title=String [length=N]`
#     （shouldRedactStringExtra 只豁免 EXTRA_SUBSTITUTE_APP_NAME / EXTRA_TEMPLATE）。所以
#     **必须带 --noredact** 才有正文；运行时还会检测输出是否被打码并如实上报。
#   · 时间：`when=<毫秒>`（notification.when，epoch 毫秒）。原代码对每条都填 ts=time.time()（现在），
#     等于丢失真实时间。已改为解析 when。
# 仍未验证：本机是 Windows 且无已授权 Android 设备，**未在真机 dumpsys 输出上跑过**；
#   解析逻辑（_parse_dumpsys_notifications）已用按上述 AOSP 格式手工构造的 mock 样本文测通过。
#   「部分 ROM 拒绝 --noredact」一说未能找到权威证据，故不作断言，改为运行时检测打码。

# Android extras 文本字段优先级（标题 / 正文）
_ANDROID_TITLE_KEYS = ("android.title", "android.title.big")
_ANDROID_BODY_KEYS = ("android.text", "android.bigText", "android.subText", "android.infoText")


def _android_extra_value(block: str, key: str) -> tuple[str, bool]:
    """从记录块里取 extras[key] 的值。返回 (值, 是否被打码)。

    格式（AOSP dumpNotification）：`    android.title=String (实际文本)`。
    打码时：`    android.title=String [length=5]`（无括号）。
    用 `key=<Type> (...)` 抓取，贪婪到行尾最后一个 `)` 以容忍正文里含 `)`。
    """
    # 转义 key 里的点；Type 是 \w+（String/SpannableString/...）
    pat = re.compile(rf"(?m)^\s*{re.escape(key)}=(\w+)\s*\((.*)\)\s*$")
    m = pat.search(block)
    if m:
        return m.group(2), False
    # 打码形态
    m2 = re.search(rf"(?m)^\s*{re.escape(key)}=\w+\s*\[length=\d+\]\s*$", block)
    if m2:
        return "", True
    return "", False


def _parse_dumpsys_notifications(txt: str, limit: int = 200) -> list[dict]:
    """把 `adb shell dumpsys notification --noredact` 的文本解析成统一 schema。

    可测试：纯函数，喂 mock 文本即可，不依赖 adb。抓不到就返回空，绝不编造。
    """
    import zlib
    if not txt or "NotificationRecord(" not in txt:
        return []
    # 优先只在「Notification List:」段内解析，避开 mArchive/Enqueued 段的重复记录；
    # 段标题找不到时退回全文（兼容 ROM 差异），再靠 key 去重兜底。
    scope = txt
    m_list = re.search(r"(?m)^ {1,4}Notification List:\s*$", txt)
    if m_list:
        rest = txt[m_list.end():]
        # 下一个同级段标题（2 空格缩进 + 非空白，或行首大写段名）作为结束
        m_end = re.search(r"(?m)^ {1,2}\S.*:$|^ {1,2}m[A-Z]\w+=|^  \* ", rest)
        scope = rest[:m_end.start()] if m_end else rest

    # 按 NotificationRecord( 切块；块首即 header（header 形如
    # `NotificationRecord(0x1a2b3c4d: pkg=com.foo user=UserHandle{0} id=1 tag=null
    #  importance=IMPORTANCE_DEFAULT key=0|com.foo|1|null|10123: Notification(...))`）
    parts = re.split(r"(?=NotificationRecord\()", scope)
    out: list[dict] = []
    seen_keys: set[str] = set()
    for blk in parts:
        if not blk.startswith("NotificationRecord("):
            continue
        try:
            pkg = re.search(r"pkg=([\w.$]+)", blk)
            key = re.search(r"key=(\S+)", blk)
            uid = re.search(r"\buser=(\S+)", blk)
            nid = re.search(r"\bid=(-?\d+)", blk)
            when = re.search(r"(?m)^\s*when=(\d+)", blk)
            pkg_s = pkg.group(1) if pkg else ""
            key_s = key.group(1) if key else ""
            if not pkg_s and not key_s:
                continue
            # 稳定 id：优先 key（跨进程稳定），用 crc32 转成正整数；退化用 pkg+id+uid
            basis = key_s or f"{pkg_s}|{uid.group(1) if uid else ''}|{nid.group(1) if nid else ''}"
            if basis in seen_keys:
                continue
            seen_keys.add(basis)
            idnum = zlib.crc32(basis.encode("utf-8")) & 0x7FFFFFFF

            title, body = "", ""
            t_red = b_red = False
            for k in _ANDROID_TITLE_KEYS:
                title, t_red = _android_extra_value(blk, k)
                if title or t_red:
                    break
            for k in _ANDROID_BODY_KEYS:
                body, b_red = _android_extra_value(blk, k)
                if body or b_red:
                    break
            # tickerText 作为标题兜底（AOSP 里它是裸打印，无 Type/括号）
            if not title:
                mt = re.search(r"(?m)^\s*tickerText=(.+)$", blk)
                if mt and mt.group(1).strip() not in ("null", "", "..."):
                    title = mt.group(1).strip()
            ts = (int(when.group(1)) / 1000.0) if when else 0.0
            out.append({
                "id": f"android:{idnum}", "platform": "android", "ts": ts,
                "app": pkg_s, "app_name": _pretty_app(pkg_s) if pkg_s else "未知来源",
                "title": title or "(无标题)", "body": body[:600],
                "kind": "alert", "raw_len": len(blk),
                "redacted": bool(t_red or b_red),
            })
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


class AndroidAdapter:
    platform = "android"

    def capability(self) -> dict[str, Any]:
        import shutil
        import subprocess
        adb = shutil.which("adb")
        base_method = "adb shell dumpsys notification --noredact（兜底；正规做法是设备端 NotificationListenerService APK）"
        caveat = ("局限：只能读到当前仍在通知栏的通知（读不到已划走的历史）；输出格式随 ROM/版本变化，"
                  "解析尽力而为；必须 --noredact 否则标题/正文被打码。")
        if not adb:
            return {"platform": self.platform, "ready": False, "source": None,
                    "method": base_method,
                    "note": "未检测到 adb（Android Platform Tools）。装好并连上设备后自动可用。" + caveat,
                    "needs_setup": True}
        try:
            r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=8)
            lines = [l for l in (r.stdout or "").splitlines()[1:] if l.strip() and "device" in l]
            if not lines:
                return {"platform": self.platform, "ready": False, "source": None,
                        "method": base_method,
                        "note": "adb 已安装，但没有已授权的设备。手机上需开启 USB 调试并允许本机。" + caveat,
                        "needs_setup": True}
            return {"platform": self.platform, "ready": True,
                    "source": lines[0].split()[0],
                    "method": base_method,
                    "note": f"已连接 {len(lines)} 台设备。" + caveat, "needs_setup": False}
        except Exception as e:
            return {"platform": self.platform, "ready": False, "source": None,
                    "method": base_method, "note": f"adb 探测失败：{e}",
                    "needs_setup": True}

    def fetch(self, since_id: str = "", limit: int = 200) -> list[dict]:
        import shutil
        import subprocess
        adb = shutil.which("adb")
        if not adb:
            return []
        try:
            r = subprocess.run([adb, "shell", "dumpsys", "notification", "--noredact"],
                               capture_output=True, text=True, timeout=12)
            txt = r.stdout or ""
        except Exception:
            return []
        # 注意：Android 是「当前快照」而非单调增长的日志，id 用内容 crc32（稳定但不递增），
        # 故 here 不套用 since_id 的 int 比较（那对非单调 id 无意义）；增量语义见 capability 说明。
        try:
            items = _parse_dumpsys_notifications(txt, limit=limit)
        except Exception:
            return []
        items.sort(key=lambda n: (n["ts"], n["id"]), reverse=True)
        return items[:limit]


# ============ 微信：WeChatBridge 归档 / 微信导出（跨平台） ============
# 依据 freestylefly/WeChatBridge（MIT，macOS Share Extension）的产物格式：
#   · Obsidian 笔记：frontmatter 含 `source: WeChat`、`chat:`、`exported: yyyy-MM-dd HH:mm`、
#     `messages: N`、`archive: "附件/xxx.zip"`，正文首行为 `# 标题`，
#     次行 `> 来源：微信 · 原始归档：[[附件/xxx]]`，附件 wikilink 形如 [[附件/name]]。
#   · 原始 ZIP：微信「合并转发」导出包，内含 `聊天记录.txt`（主文本）+ 图片/视频。
# WeChatBridge 本体仅 macOS；但它产出的 Obsidian vault 可同步到任何平台，
# 且微信 PC 版「迁移与备份 → 导出」也产出同构 ZIP。故此 adapter 在四端通用：
# 读归档文件，不依赖 WeChatBridge 进程，也不读取/解密微信数据库。
def _wechat_roots() -> list[str]:
    """候选归档根目录：显式配置 > 各平台 Obsidian 常见位置 > 微信导出目录。"""
    roots: list[str] = []
    env = os.environ.get("MULIAO_WECHAT_DIR")
    if env:
        roots.append(os.path.expanduser(env))
    home = os.path.expanduser("~")
    cand = [
        os.path.join(home, "Documents", "Obsidian"),          # 跨平台常见 vault 父目录
        os.path.join(home, "Obsidian"),
        os.path.join(home, "Library", "Mobile Documents", "iCloud~md~obsidian", "Documents"),  # macOS iCloud vault
        os.path.join(home, "Documents", "WeChatBridge"),
        os.path.join(home, "Documents", "微信导出"),
        os.path.join(home, "Documents", "WeChat Files"),
        os.path.join(home, "Documents", "xwechat_files"),     # 微信 4.x
    ]
    roots.extend(cand)
    return [r for r in roots if r and os.path.isdir(r)]


# frontmatter 解析：只取 WeChatBridge 实际写出的键，未知键忽略，不猜
_FM_KEYS = ("title", "source", "chat", "scene", "exported", "messages", "archive")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        if k in _FM_KEYS:
            fm[k] = v.strip().strip('"').strip("'")
    return fm, body


def _wechat_transcript_preview(body: str, limit: int = 240) -> str:
    """从归档正文提取前几条消息摘要（去掉标题行与 wikilink 噪音）。"""
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith(">"):
            continue
        line = re.sub(r"!?\[\[[^\]]*\]\]", "", line).strip()   # 去附件 wikilink
        line = re.sub(r"[#>*_`]", "", line).strip()
        if len(line) < 2:
            continue
        out.append(line)
        if sum(len(x) for x in out) > limit:
            break
    return " / ".join(out)[:limit]


class WeChatBridgeAdapter:
    """读 WeChatBridge / 微信导出的归档，转成统一通知 schema（kind='wechat'）。"""
    platform = "wechat"

    def __init__(self) -> None:
        self.roots = _wechat_roots()

    def capability(self) -> dict[str, Any]:
        ok = bool(self.roots)
        return {
            "platform": self.platform,
            "ready": ok,
            "source": self.roots[0] if ok else None,
            "roots": self.roots[:5],
            "method": "只读归档文件（Obsidian 笔记 / 微信导出 ZIP），不读微信数据库",
            "note": None if ok else (
                "未找到微信归档目录。设置环境变量 MULIAO_WECHAT_DIR 指向 WeChatBridge 的 "
                "Obsidian vault 或微信导出目录即可启用（支持 mac/linux/Windows，Android 不适用）。"
            ),
            "needs_setup": not ok,
            "upstream": "https://github.com/freestylefly/WeChatBridge",
        }

    def _iter_files(self, limit: int):
        seen = 0
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                # 跳过 vault 的内部目录，避免扫到无关内容
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
                for fn in filenames:
                    low = fn.lower()
                    if low.endswith((".md", ".zip", ".txt")):
                        yield os.path.join(dirpath, fn)
                        seen += 1
                        if seen >= limit:
                            return

    def fetch(self, since_id: str = "", limit: int = 200) -> list[dict]:
        if not self.roots:
            return []
        since = 0
        if since_id.startswith("wechat:"):
            try:
                since = int(since_id.split(":", 1)[1])
            except ValueError:
                since = 0
        out: list[dict] = []
        import zipfile
        for path in self._iter_files(limit * 20):
            try:
                st = os.stat(path)
            except OSError:
                continue
            mtime = st.st_mtime
            base = os.path.basename(path)
            low = base.lower()
            title, chat, body, msgs = "", "", "", None
            if low.endswith(".md"):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read(64 * 1024)
                except OSError:
                    continue
                fm, text_body = _parse_frontmatter(text)
                # 只认 WeChatBridge 产物：source: WeChat
                if (fm.get("source") or "").lower() != "wechat":
                    continue
                title = fm.get("title") or base[:-3]
                chat = fm.get("chat") or ""
                body = _wechat_transcript_preview(text_body)
                msgs = fm.get("messages")
            elif low.endswith(".zip"):
                # 微信「合并转发」导出包：读 聊天记录.txt
                try:
                    with zipfile.ZipFile(path) as z:
                        names = z.namelist()
                        txt = next((n for n in names if os.path.basename(n) == "聊天记录.txt"), None)
                        if not txt:
                            continue
                        with z.open(txt) as fh:
                            raw = fh.read(64 * 1024).decode("utf-8", errors="replace")
                except Exception:
                    continue
                title = base[:-4]
                body = _wechat_transcript_preview(raw)
                msgs = str(len([l for l in raw.splitlines() if l.strip()]))
            elif low.endswith(".txt") and base in ("聊天记录.txt", "chatlog.txt"):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read(64 * 1024)
                except OSError:
                    continue
                title = os.path.basename(os.path.dirname(path)) or "微信聊天记录"
                body = _wechat_transcript_preview(raw)
                msgs = str(len([l for l in raw.splitlines() if l.strip()]))
            else:
                continue
            # 纳秒时间 + 路径哈希：同秒多文件不冲突，返回后的 since_id 也不会重复命中同一条。
            import zlib
            nid = st.st_mtime_ns * 100000 + (zlib.crc32(os.path.normcase(path).encode("utf-8")) % 100000)
            if since and nid <= since:
                continue
            out.append({
                "id": f"wechat:{nid}", "platform": self.platform, "ts": float(mtime),
                "app": "WeChatBridge", "app_name": "微信归档",
                "title": title[:120] or "(无标题)",
                "body": (f"[{chat}] " if chat else "") + (body or ""),
                "kind": "wechat",
                "messages": msgs,
                "raw_len": st.st_size,
            })
        out.sort(key=lambda n: n["ts"], reverse=True)
        return out[:limit]


# ============ 统一入口 ============
def _adapters() -> list:
    """返回当前平台的首选 adapter + 其余平台的 adapter（用于状态展示与跨端预留）。"""
    win, mac, lin, droid = WindowsAdapter(), MacOSAdapter(), LinuxAdapter(), AndroidAdapter()
    wechat = WeChatBridgeAdapter()
    if PLATFORM == "win32":
        return [win, wechat, mac, lin, droid]
    if PLATFORM == "darwin":
        return [mac, wechat, win, lin, droid]
    return [lin, wechat, win, mac, droid]


def capabilities() -> list[dict]:
    """各 adapter 自报能力。单个 adapter 的 capability() 抛异常不能拖垮整个状态接口，
    故逐个兜底，失败的以 ready=False + err 如实占位。"""
    out: list[dict] = []
    for a in _adapters():
        try:
            out.append(a.capability())
        except Exception as e:  # noqa: BLE001
            out.append({"platform": getattr(a, "platform", "?"), "ready": False,
                        "source": None, "method": None, "note": f"capability 探测异常：{e}",
                        "needs_setup": True})
    return out


def fetch_all(since_id: str = "", limit: int = 200, all_platforms: bool = False) -> dict:
    """抓通知。默认抓当前平台 + 微信归档（跨平台通用）；
    all_platforms=True 时尝试所有 ready 的平台（Android 走 adb，可能跨设备）。

    健壮性：adapters 只构造一次（原代码调 _adapters() 两次 = 构造 10 个对象）；
    每个 adapter 的 fetch() 单独 try/except 兜底，**单个平台崩了不让 /api/notify 500**。

    关于 since_id 一致性：各 adapter 都用自己的前缀守卫（win:/mac:/linux:/wechat:），
    传入异类前缀会被忽略（since=0，不过滤），因此「id 单调整数」与「wechat 用 mtime 当 id」
    两种口径彼此隔离、互不比较，安全。Android 是当前快照（id 非单调），不参与 since_id 增量。
    """
    adapters = _adapters()
    primary = adapters[0].platform
    items: list[dict] = []
    used: list[str] = []
    errors: list[dict] = []
    for a in adapters:
        # 微信归档在任何平台都尝试（它是文件产物，不绑定 OS）
        if not all_platforms and a.platform not in (primary, "wechat"):
            continue
        try:
            got = a.fetch(since_id=since_id, limit=limit)
        except Exception as e:  # noqa: BLE001
            got = []
            errors.append({"platform": a.platform, "err": str(e)})
        if got:
            items.extend(got)
            used.append(a.platform)
    # 排序：先按时间（Windows/macOS/Android 现均有真实 ts），同时间按 id 数值兜底
    items.sort(key=lambda n: (n.get("ts") or 0.0, float(n["id"].split(":")[1])), reverse=True)
    res = {"items": items[:limit], "platforms_used": used, "count": len(items[:limit])}
    if errors:
        res["adapter_errors"] = errors
    return res
