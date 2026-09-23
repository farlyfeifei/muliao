#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify_bridge.py — 幕僚 Linux 通知桥（零侵入）

用途
----
Linux 上没有「只读的历史通知库」可查（不像 Windows 的 wpndatabase.db、
macOS 的 usernoted db2）。freedesktop 规范里，唯一能「读到别人发的通知」的
正规做法是**自己注册成 org.freedesktop.Notifications 的服务端**——但那会
**抢走系统的通知守护进程**（GNOME/KDE 的弹窗就没了），属于侵入式，不可接受。

本脚本走的是另一条零侵入路线：**被动监听（eavesdrop）**。
用 `dbus-monitor` 旁听会话总线上别人调用 Notify 方法的报文，解析出
app_name / summary / body，落盘成 JSONL，供 notifier.LinuxAdapter 读取。
它**不抢 bus name、不需要 root、不改任何系统设置**——dbus-monitor 只是个
旁听者，对收发双方完全透明。

D-Bus 接口签名（已核实）
------------------------
org.freedesktop.Notifications.Notify 的方法签名固定为：
    in  s   app_name        应用名（如 "Slack"）
    in  u   replaces_id     被替换的通知 id（uint32）
    in  s   app_icon        图标名
    in  s   summary         标题
    in  s   body            正文
    in  as  actions         动作数组
    in  a{sv} hints         提示字典
    in  i   expire_timeout  过期毫秒（int32）
    out u   id              服务端分配的通知 id
来源（已核对，2026-09）：
  · codelif/hyprnotify 的 introspection XML（godbus 实现）
  · canonical/desktop_notifications.dart 的 callMethod('Notify', ...) 实参顺序
  · freedesktop notification-spec（specifications.freedesktop.org，被 Anubis
    反爬挡住，未能直接抓取正文；以上两个独立实现互相印证，足以确认签名）

dbus-monitor 输出格式（已核实）
------------------------------
逐字来自 freedesktop/dbus 源码 tools/dbus-print-message.c：
  · 报文头：
      method call time=... sender=:1.x -> dest=org.freedesktop.Notifications \
      serial=N path=/org/freedesktop/Notifications; \
      interface=org.freedesktop.Notifications; member=Notify
  · 字符串实参：`printf("string \""); printf("%s", val); printf("\"\n")`
    —— **值本身不做任何转义**。后果：
       1) body 含换行时，一条 string 会跨多行（开引号在首行、闭引号在末行）；
       2) body 含双引号时，无法靠引号配对精确切分。
    本解析器据此做「跨行累积 + 取首尾引号之间」的容错处理。
  · 标量：`uint32 N` / `int32 N` / `boolean true`。
  · 数组：`array [` … 换行缩进 … `]`；字典项：`dict entry(` … `)`。
  · 缩进：每层 3 个空格（源码 indent() 写死 3 空格）。

实测状态：**未在真实 Linux/dbus 上跑过**（开发机是 Windows）。
已做的验证：① py_compile 通过；② 用按上述源码格式手工构造的 mock 样本
（含多行 body、含内嵌引号、空 body、中文、含 `member=` 字样的 body）喂给
parse_notifies()，断言字段提取正确。见文件末 _selftest()。

用法
----
    python3 notify_bridge.py            # 前台监听，Ctrl-C 退出
    python3 notify_bridge.py --check    # 只检查环境（dbus-monitor / bus）后退出
    python3 notify_bridge.py --selftest # 跑内置 mock 解析测试后退出（不需要 dbus）
    python3 notify_bridge.py --journal /path/to/file.jsonl   # 自定义落盘路径

依赖：dbus-monitor（Debian/Ubuntu: `sudo apt install dbus-x11`；
Fedora: `sudo dnf install dbus-daemon` 或 `dbus-tools`；Arch: `sudo pacman -S dbus`）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

# 默认 journal 路径必须与 notifier._LINUX_JOURNAL 一致（见 notifier.py）
DEFAULT_JOURNAL = os.path.join(os.path.expanduser("~"), ".muliao", "notifier_journal.jsonl")

# dbus-monitor 过滤器：只旁听会话总线上对 Notifications.Notify 的方法调用
DBUS_FILTER = "type=method_call,interface='org.freedesktop.Notifications',member='Notify'"

# 报文头：缩进 0、含 interface 与 member=Notify
_HEADER_RE = re.compile(
    r"^(?:method call|signal|method return|error)\b.*"
    r"interface=org\.freedesktop\.Notifications;\s*member=Notify\b"
)
# 任意新报文头（用于在收满 5 个前导实参后跳过剩余行直到下一条报文）
_ANY_HEADER_RE = re.compile(r"^(?:method call|signal|method return|error)\b")

# 前导标量实参的类型 token（按 Notify 签名顺序）
_STRING_RE = re.compile(r'^(\s*)string "(.*)$')     # 捕获缩进 + 起始内容（闭引号可能不在本行）
_UINT_RE = re.compile(r"^\s*uint32 (\d+)\s*$")
_INT_RE = re.compile(r"^\s*int32 (-?\d+)\s*$")
# 报文结束符：Notify 最后一个实参 expire_timeout 是顶层 int32（缩进恰为 3 空格，
# 因为 dbus-print-message.c 的 INDENT=3）。hints 里的 variant int32 缩进更深，不会误命中。
_TERM_RE = re.compile(r"^ {3}int32 -?\d+\s*$")


def _install_hint() -> str:
    return (
        "未找到 dbus-monitor。请安装：\n"
        "  Debian/Ubuntu : sudo apt install dbus-x11\n"
        "  Fedora/RHEL   : sudo dnf install dbus-daemon   （或 dbus-tools）\n"
        "  Arch          : sudo pacman -S dbus\n"
        "  openSUSE      : sudo zypper install dbus-1\n"
    )


def check_environment(verbose: bool = True) -> tuple[bool, str]:
    """检查 dbus-monitor 与会话总线是否可用。返回 (ok, 说明)。"""
    if shutil.which("dbus-monitor") is None:
        msg = _install_hint()
        if verbose:
            print("[notify-bridge] " + msg.replace("\n", "\n[notify-bridge] "), file=sys.stderr)
        return False, "dbus-monitor 未安装"
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        # 没有会话总线地址：可能在纯 TTY / systemd 服务里跑。dbus-monitor 仍可能
        # 通过默认地址连上，但多数桌面会话才有通知流量。如实告知，不强制失败。
        msg = ("未检测到 DBUS_SESSION_BUS_ADDRESS——需在图形会话（GNOME/KDE 等）里运行，"
               "dbus-monitor 才能旁听到通知。")
        if verbose:
            print("[notify-bridge] " + msg, file=sys.stderr)
        return True, msg
    if verbose:
        print("[notify-bridge] 环境就绪：dbus-monitor 可用，会话总线已检测到。", file=sys.stderr)
    return True, "ok"


def parse_notifies(lines) -> "list[dict]":
    """把 dbus-monitor 的输出行流解析成通知记录列表。

    每条记录：{app, title, body, icon, replaces_id}。
    只取 Notify 的前 5 个实参（app_name/replaces_id/app_icon/summary/body）；
    后面的 actions/hints/expire_timeout 不影响展示，收满 5 个后直接跳到下一条报文。

    可测试：传入任意可迭代的字符串行（mock 或真实），不依赖 dbus。
    """
    out: list[dict] = []
    buf = list(lines)
    i = 0
    n = len(buf)
    while i < n:
        line = buf[i]
        if not _HEADER_RE.match(line):
            i += 1
            continue
        # 命中一条 Notify 调用头，开始按签名顺序收前 5 个实参
        i += 1
        args: list[tuple[str, object]] = []
        while i < n and len(args) < 5:
            cur = buf[i]
            # 遇到下一条报文头 → 本条提前结束（异常，但安全）
            if _ANY_HEADER_RE.match(cur) and not cur.startswith(" "):
                break
            ms = _STRING_RE.match(cur)
            if ms:
                # 可能是跨行 string：把剩余行交给续读器
                # 判断本行是否已闭引号：group(2) 以 " 结尾且不是空 `"` 转义场景
                acc = ms.group(2)
                j = i + 1
                while not acc.endswith('"') and j < n:
                    nxt = buf[j]
                    if _ANY_HEADER_RE.match(nxt) and not nxt.startswith(" "):
                        break
                    acc = acc + "\n" + nxt
                    j += 1
                if acc.endswith('"'):
                    acc = acc[:-1]
                args.append(("s", acc))
                i = j
                continue
            mu = _UINT_RE.match(cur)
            if mu:
                args.append(("u", int(mu.group(1))))
                i += 1
                continue
            mi = _INT_RE.match(cur)
            if mi:
                args.append(("i", int(mi.group(1))))
                i += 1
                continue
            # 其它顶层 token（理论上 Notify 前 5 个实参不会是这些）：跳过该行
            i += 1
        # 收满（或部分）后，构造记录
        if args:
            # 按位置映射；缺失则留空
            def _at(idx, default):
                return args[idx][1] if idx < len(args) else default
            rec = {
                "app": str(_at(0, "") or ""),
                "replaces_id": int(_at(1, 0) or 0),
                "icon": str(_at(2, "") or ""),
                "title": str(_at(3, "") or ""),
                "body": str(_at(4, "") or ""),
            }
            out.append(rec)
        # 跳过本条报文剩余行（actions/hints/expire_timeout），直到下一条报文头
        while i < n and not (_ANY_HEADER_RE.match(buf[i]) and not buf[i].startswith(" ")):
            i += 1
    return out


class JournalWriter:
    """把记录追加写入 JSONL，字段与 notifier.LinuxAdapter / server.py 一致：
    {seq, ts, app, title, body}。seq 使用完整 epoch 纳秒并保证进程内单调递增，
    不取模，避免约 11.6 天回绕后新通知被旧游标永久过滤。"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._last_seq = 0

    def _next_seq(self) -> int:
        s = time.time_ns()
        if s <= self._last_seq:
            s = self._last_seq + 1
        self._last_seq = s
        return s

    def write(self, rec: dict) -> None:
        line = {
            "seq": self._next_seq(),
            "ts": time.time(),
            "app": rec.get("app", ""),
            "title": rec.get("title", ""),
            "body": rec.get("body", ""),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _iter_monitor_lines(proc):
    for raw in proc.stdout:
        # dbus-monitor 输出为 UTF-8；个别字节非法时替换而非崩溃
        yield raw.rstrip("\n")


def run(journal_path: str) -> int:
    ok, msg = check_environment(verbose=True)
    if not ok:
        return 2
    writer = JournalWriter(journal_path)
    print(f"[notify-bridge] 开始旁听 org.freedesktop.Notifications.Notify → {journal_path}",
          file=sys.stderr)
    print("[notify-bridge] Ctrl-C 停止。（被动监听，不抢 bus name，不需要 root）",
          file=sys.stderr)
    try:
        proc = subprocess.Popen(
            ["dbus-monitor", "--session", DBUS_FILTER],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        print("[notify-bridge] " + _install_hint().replace("\n", "\n[notify-bridge] "),
              file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[notify-bridge] 启动 dbus-monitor 失败：{e}", file=sys.stderr)
        return 2

    # 流式解析：dbus-monitor 一条报文头 + 若干实参行。为了能「来一条立刻写一条」，
    # 需要判断报文何时结束。Notify 的最后一个实参恒为 expire_timeout（int32，
    # 顶层缩进恰为 3 空格；hints 里的 variant int32 缩进更深，不会误判），
    # 故以「顶层 int32 行」为报文结束符即时落盘；同时保留「遇到下条报文头再 flush」
    # 作为兜底（万一某 ROM 缺省 expire_timeout）。
    block: list[str] = []

    def _flush():
        if not block:
            return
        for rec in parse_notifies(block):
            if rec.get("title") or rec.get("body"):
                writer.write(rec)
                print(f"[notify-bridge] +1 {rec.get('app','')!r} "
                      f"{rec.get('title','')[:40]!r}", file=sys.stderr)
        block.clear()

    try:
        for line in _iter_monitor_lines(proc):
            if _HEADER_RE.match(line):
                _flush()                 # 兜底：上一条若未靠 int32 结束，这里补 flush
                block.append(line)
            else:
                block.append(line)
                if _TERM_RE.match(line):  # 顶层 int32 = expire_timeout = 报文结束
                    _flush()
        _flush()
    except KeyboardInterrupt:
        _flush()
        print("\n[notify-bridge] 已停止。", file=sys.stderr)
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    return 0


# ============ 内置 mock 自测（不需要 dbus） ============
def _selftest() -> int:
    """用按 dbus-print-message.c 真实格式构造的样本验证解析逻辑。"""
    HDR = ("method call time=1758600000.000000 sender=:1.42 -> "
           "dest=org.freedesktop.Notifications serial=88 "
           "path=/org/freedesktop/Notifications; "
           "interface=org.freedesktop.Notifications; member=Notify")

    cases = []

    # 1) 常规单行
    cases.append(("常规", [
        HDR,
        '   string "Slack"',
        '   uint32 0',
        '   string "slack"',
        '   string "Alice"',
        '   string "Hey, lunch?"',
        '   array [',
        '   ]',
        '   array [',
        '      dict entry(',
        '         string "desktop-entry"',
        '         variant             string "Slack"',
        '      )',
        '   ]',
        '   int32 -1',
    ], {"app": "Slack", "title": "Alice", "body": "Hey, lunch?"}))

    # 2) 多行 body（dbus-monitor 不转义换行 → string 跨行）
    cases.append(("多行body", [
        HDR,
        '   string "Notify OSD"',
        '   uint32 7',
        '   string ""',
        '   string "Build finished"',
        '   string "Line one',
        'Line two',
        'Line three"',
        '   array [',
        '   ]',
        '   int32 5000',
    ], {"app": "Notify OSD", "title": "Build finished", "body": "Line one\nLine two\nLine three"}))

    # 3) body 含内嵌双引号
    cases.append(("内嵌引号", [
        HDR,
        '   string "git"',
        '   uint32 0',
        '   string ""',
        '   string "commit"',
        '   string "He said "hi" loudly"',
        '   array [',
        '   ]',
    ], {"app": "git", "title": "commit", "body": 'He said "hi" loudly'}))

    # 4) 空 body
    cases.append(("空body", [
        HDR,
        '   string "spotify"',
        '   uint32 0',
        '   string ""',
        '   string "Now playing"',
        '   string ""',
        '   array [',
        '   ]',
    ], {"app": "spotify", "title": "Now playing", "body": ""}))

    # 5) 中文 + body 里含 `member=` 字样（不应被误判为报文头）
    cases.append(("中文与陷阱", [
        HDR,
        '   string "微信"',
        '   uint32 0',
        '   string ""',
        '   string "张三"',
        '   string "method call member=Notify 是陷阱文本"',
        '   array [',
        '   ]',
    ], {"app": "微信", "title": "张三", "body": "method call member=Notify 是陷阱文本"}))

    # 6) 两条连发
    two = [
        HDR,
        '   string "A"', '   uint32 0', '   string ""', '   string "t1"', '   string "b1"',
        '   array [', '   ]',
        HDR,
        '   string "B"', '   uint32 0', '   string ""', '   string "t2"', '   string "b2"',
        '   array [', '   ]',
    ]

    failures = 0
    for name, lines, expect in cases:
        got = parse_notifies(lines)
        if not got:
            print(f"  [FAIL] {name}: 没有解析出记录")
            failures += 1
            continue
        g = got[0]
        bad = {k: (g.get(k), expect[k]) for k in expect if g.get(k) != expect[k]}
        if bad:
            print(f"  [FAIL] {name}: {bad}")
            failures += 1
        else:
            print(f"  [ok]   {name}: app={g['app']!r} title={g['title']!r} body={g['body']!r}")

    got2 = parse_notifies(two)
    if len(got2) == 2 and got2[0]["title"] == "t1" and got2[1]["title"] == "t2":
        print(f"  [ok]   连发两条: {[r['title'] for r in got2]}")
    else:
        print(f"  [FAIL] 连发两条: 得到 {got2}")
        failures += 1

    print(f"\n_selftest: {'全部通过' if failures == 0 else str(failures) + ' 个用例失败'}")
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="幕僚 Linux 通知桥（被动监听 dbus）")
    ap.add_argument("--journal", default=DEFAULT_JOURNAL, help=f"JSONL 落盘路径（默认 {DEFAULT_JOURNAL}）")
    ap.add_argument("--check", action="store_true", help="只检查环境后退出")
    ap.add_argument("--selftest", action="store_true", help="跑内置 mock 解析测试后退出（不需要 dbus）")
    args = ap.parse_args(argv)

    if args.selftest:
        print("[notify-bridge] mock 自测（按 dbus-print-message.c 真实格式构造样本）：")
        return _selftest()
    if args.check:
        ok, msg = check_environment(verbose=True)
        return 0 if ok else 2
    return run(args.journal)


if __name__ == "__main__":
    sys.exit(main())
