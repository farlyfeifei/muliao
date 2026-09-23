"""幕僚 Muliáo · 构建、精确卸载、固定安装到 D 盘并验收。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import winreg
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
SETUP_EXE = os.path.join(DIST, "幕僚Muliáo-Setup.exe")
DIST_EXE = os.path.join(DIST, "Muliáo.exe")
APP_EXE_NAME = "Muliáo.exe"
INSTALL_DIR = r"D:\Program Files\幕僚Muliáo"
APP_SHORTCUT = "幕僚 Muliáo.lnk"
SETUP_SHORTCUT = "幕僚 Muliáo · 安装到D盘.lnk"
APP_UNINSTALL_KEY = r"{D0C8DD92-F4A6-4E0B-9998-9E7D55B75D89}_is1"
UNINSTALL_BASES = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
LEGACY_DIRS = (
    INSTALL_DIR,
    r"D:\Program Files\Muliao",
    r"D:\_s1",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Muliao"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "幕僚Muliáo"),
)


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n{'=' * 58}\n{title}\n{'=' * 58}", flush=True)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_iscc() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    for base in (local, pf86, pf):
        if not base:
            continue
        p = (os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe")
             if base == local else os.path.join(base, "Inno Setup 6", "ISCC.exe"))
        if os.path.isfile(p):
            return p
    return shutil.which("iscc")


def build() -> bool:
    section("① 构建单文件 exe + 安装包")
    _kill_running_app()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MULIAO_NO_BROWSER": "1"}

    def run(cmd: list[str], timeout: int) -> bool:
        log("$ " + " ".join(cmd))
        try:
            r = subprocess.run(
                cmd, cwd=HERE, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"✗ 超时（{timeout}s）")
            return False
        except Exception as e:
            log(f"✗ 启动失败：{type(e).__name__}: {e}")
            return False
        if r.returncode != 0:
            log(f"✗ 失败 rc={r.returncode}")
            log((r.stderr or r.stdout or "")[-1600:])
            return False
        for line in [x for x in (r.stdout or "").splitlines() if x.strip()][-3:]:
            log(line[:150])
        return True

    if not run([sys.executable, "make_icon.py"], 120):
        return False
    if not run([sys.executable, "-m", "PyInstaller", "muliao.spec", "--noconfirm", "--clean"], 900):
        return False
    if not os.path.isfile(DIST_EXE):
        log(f"✗ 未产出 {DIST_EXE}")
        return False
    iscc = _find_iscc()
    if not iscc:
        log("✗ 未找到 Inno Setup 6 的 ISCC.exe")
        return False
    if not run([iscc, os.path.join(HERE, "installer.iss")], 600):
        return False
    if not os.path.isfile(SETUP_EXE):
        log(f"✗ 未产出 {SETUP_EXE}")
        return False
    log(f"✓ exe {os.path.getsize(DIST_EXE) / 1024 / 1024:.1f} MB · 安装包 {os.path.getsize(SETUP_EXE) / 1024 / 1024:.1f} MB")
    return True


def _reg_value(key, name: str) -> str:
    try:
        return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def find_installs() -> list[dict]:
    """只识别本产品固定 AppId，绝不按 DisplayName 模糊卸载。"""
    out = []
    for hive, hname in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                        (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for base in UNINSTALL_BASES:
            path = base + "\\" + APP_UNINSTALL_KEY
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            out.append({
                "hive": hname, "hive_obj": hive, "base": base,
                "key": APP_UNINSTALL_KEY, "name": _reg_value(key, "DisplayName"),
                "version": _reg_value(key, "DisplayVersion"),
                "location": _reg_value(key, "InstallLocation"),
                "uninstall": _reg_value(key, "UninstallString"),
                "quiet": _reg_value(key, "QuietUninstallString"),
            })
            winreg.CloseKey(key)
    return out


def _install_record_exists(item: dict) -> bool:
    try:
        k = winreg.OpenKey(item["hive_obj"], item["base"] + "\\" + item["key"])
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def _split_windows_commandline(command: str) -> list[str]:
    if not command.strip():
        return []
    argc = ctypes.c_int()
    ctypes.windll.shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    ctypes.windll.shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = ctypes.windll.shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _recognized_install_dir(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return os.path.isfile(os.path.join(path, APP_EXE_NAME)) or os.path.isfile(os.path.join(path, "unins000.exe"))


def _terminate_tree(pid: int) -> None:
    try:
        import psutil
        root = psutil.Process(pid)
        children = root.children(recursive=True)
        for p in reversed(children):
            try:
                p.terminate()
            except Exception:
                pass
        try:
            root.terminate()
        except Exception:
            pass
        _, alive = psutil.wait_procs(children + [root], timeout=8)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass


def _kill_running_app() -> None:
    try:
        import psutil
    except ImportError:
        log("⚠ 缺 psutil，无法自动结束运行中的幕僚")
        return
    roots = {os.path.normcase(os.path.abspath(x)) for x in LEGACY_DIRS if x}
    roots.add(os.path.normcase(os.path.abspath(DIST)))
    pids = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = os.path.normcase(os.path.abspath(p.info.get("exe") or ""))
            name = (p.info.get("name") or "").casefold()
            in_root = any(exe == r or exe.startswith(r + os.sep) for r in roots)
            exact_name = name in {"muliáo.exe".casefold(), "muliao.exe"}
            if in_root or (exact_name and os.path.basename(exe).casefold() in {"muliáo.exe".casefold(), "muliao.exe"}):
                pids.append(p.pid)
        except Exception:
            continue
    for pid in sorted(set(pids), reverse=True):
        _terminate_tree(pid)
    if pids:
        log("✓ 已结束运行中的幕僚进程")
        time.sleep(2)
    else:
        log("无运行中的幕僚进程")


def _run_inno(exe: str, args: list[str], wait_for, timeout: int) -> bool:
    log(f"$ {os.path.basename(exe)} {' '.join(args)}")
    try:
        proc = subprocess.Popen([exe] + args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"✗ 启动失败：{type(e).__name__}: {e}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        try:
            if wait_for():
                log("✓ 完成")
                return True
        except Exception:
            pass
        if proc.poll() is not None and time.time() + 3 >= deadline:
            break
    log(f"✗ {timeout}s 内未达成完成条件")
    _terminate_tree(proc.pid)
    return False


def uninstall_old() -> bool:
    section("② 精确卸载旧版（固定 AppId）")
    _kill_running_app()
    for item in find_installs():
        log(f"发现：{item['name']} v{item['version']} @ {item['location'] or '(未记录)'}")
        argv = _split_windows_commandline(item["quiet"] or item["uninstall"])
        if not argv or not os.path.isfile(argv[0]):
            log(f"✗ 找不到已登记卸载器：{argv[0] if argv else '(空)'}")
            return False
        extra = [x for x in argv[1:] if x]
        for flag in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"):
            if not any(x.upper() == flag for x in extra):
                extra.append(flag)
        if not _run_inno(argv[0], extra, lambda item=item: not _install_record_exists(item), 180):
            return False

    # 只清理已确认包含幕僚 exe/卸载器的历史目录。
    for path in LEGACY_DIRS:
        if _recognized_install_dir(path):
            log(f"清理已确认旧目录：{path}")
            shutil.rmtree(path, ignore_errors=False)
        if path and os.path.isdir(path):
            log(f"✗ 目录仍存在：{path}")
            return False
    if find_installs():
        log("✗ 固定 AppId 卸载项仍存在")
        return False
    log("✓ 旧版和已确认残留目录已清理")
    return True


def _installed_matches() -> bool:
    exe = os.path.join(INSTALL_DIR, APP_EXE_NAME)
    if not (os.path.isfile(exe) and os.path.isfile(DIST_EXE)):
        return False
    records = find_installs()
    expected = os.path.normcase(os.path.abspath(INSTALL_DIR)).rstrip("\\/")
    registered = any(os.path.normcase(os.path.abspath(x.get("location") or "")).rstrip("\\/") == expected
                     for x in records)
    return registered and sha256(exe) == sha256(DIST_EXE)


def install_to_d() -> bool:
    section("③ 安装到唯一 D 盘目录")
    if not os.path.isfile(SETUP_EXE) or not os.path.isfile(DIST_EXE):
        log("✗ 缺少最新 exe 或安装包")
        return False
    os.makedirs(os.path.dirname(INSTALL_DIR), exist_ok=True)
    ok = _run_inno(
        SETUP_EXE,
        ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={INSTALL_DIR}"],
        _installed_matches,
        240,
    )
    if not ok or not _installed_matches():
        log("✗ 安装器未完成，或已安装 exe 与本次构建哈希不一致")
        return False
    exe = os.path.join(INSTALL_DIR, APP_EXE_NAME)
    log(f"✓ 已安装 {exe} · sha256 {sha256(exe)[:16]}…")
    return True


def make_shortcuts() -> bool:
    section("④ 建立桌面快捷方式")
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        log("✗ 缺少 pywin32，无法创建快捷方式")
        return False
    pythoncom.CoInitialize()
    sh = win32com.client.Dispatch("WScript.Shell")
    desktop = sh.SpecialFolders("Desktop")
    os.makedirs(desktop, exist_ok=True)
    app_exe = os.path.join(INSTALL_DIR, APP_EXE_NAME)
    install_copy = os.path.join(os.path.dirname(INSTALL_DIR), "幕僚Muliáo-Setup.exe")
    shutil.copy2(SETUP_EXE, install_copy)

    for name in (APP_SHORTCUT, SETUP_SHORTCUT):
        p = os.path.join(desktop, name)
        if os.path.isfile(p):
            os.remove(p)

    p = os.path.join(desktop, APP_SHORTCUT)
    s = sh.CreateShortCut(p)
    s.TargetPath = app_exe
    s.WorkingDirectory = INSTALL_DIR
    s.IconLocation = app_exe + ",0"
    s.Description = "幕僚 Muliáo · 本地 AI 参谋"
    s.save()

    p2 = os.path.join(desktop, SETUP_SHORTCUT)
    s = sh.CreateShortCut(p2)
    s.TargetPath = install_copy
    s.Arguments = f'/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /DIR="{INSTALL_DIR}"'
    s.WorkingDirectory = os.path.dirname(install_copy)
    s.IconLocation = install_copy + ",0"
    s.Description = f"幕僚 Muliáo 安装包 · 固定安装到 {INSTALL_DIR}"
    s.save()

    # 只删除目标确实指向已知旧幕僚目录的历史快捷方式。
    known = {os.path.normcase(os.path.abspath(x)) for x in LEGACY_DIRS if x}
    for old in ("Echo 回音幕僚.lnk", "Echo.lnk", "回音幕僚.lnk"):
        op = os.path.join(desktop, old)
        if not os.path.isfile(op):
            continue
        try:
            target = os.path.normcase(os.path.abspath(sh.CreateShortCut(op).TargetPath or ""))
            if any(target.startswith(root + os.sep) for root in known):
                os.remove(op)
        except Exception:
            pass

    good = os.path.isfile(os.path.join(desktop, APP_SHORTCUT)) and os.path.isfile(os.path.join(desktop, SETUP_SHORTCUT))
    log(f"{'✓' if good else '✗'} 桌面：{APP_SHORTCUT} / {SETUP_SHORTCUT}")
    return good


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port_range() -> int:
    for _ in range(200):
        base = random.SystemRandom().randint(22000, 55000)
        if all(_port_free(p) for p in range(base, base + 21)):
            return base
    raise RuntimeError("找不到连续空闲端口")


def smoke_test() -> bool:
    section("⑤ 隔离冒烟测试（真实安装 exe）")
    exe = os.path.join(INSTALL_DIR, APP_EXE_NAME)
    if not os.path.isfile(exe):
        log("✗ 已安装 exe 不存在")
        return False
    tmp_appdata = tempfile.mkdtemp(prefix="muliao_smoke_")
    base_port = _free_port_range()
    token = uuid.uuid4().hex
    env = {**os.environ, "APPDATA": tmp_appdata, "MULIAO_NO_BROWSER": "1",
           "MULIAO_PORT": str(base_port), "MULIAO_INSTANCE_TOKEN": token,
           "PYTHONIOENCODING": "utf-8"}
    proc = None
    service_pid = None
    port = None
    ok = True

    def get(base: str, path: str, timeout=30):
        return json.load(urllib.request.urlopen(base + path, timeout=timeout))

    def post(base: str, path: str, obj, timeout=60, headers=None):
        hs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode(), headers=hs)
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    try:
        proc = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 80
        while time.time() < deadline and port is None:
            if proc.poll() is not None:
                raise RuntimeError(f"exe 提前退出 rc={proc.returncode}")
            for candidate in range(base_port, base_port + 21):
                try:
                    inst = get(f"http://127.0.0.1:{candidate}", "/api/instance", 1)
                    if inst.get("token") == token and inst.get("app") == "muliao":
                        port = candidate
                        service_pid = int(inst.get("pid") or 0)
                        break
                except Exception:
                    pass
            if port is None:
                time.sleep(1)
        if port is None:
            raise RuntimeError("本次实例未就绪")
        base = f"http://127.0.0.1:{port}"
        log(f"✓ 本次实例 token 匹配 · pid={service_pid} · port={port}")

        status = get(base, "/api/status", 45)
        log(f"status: model={status.get('model')} engine={status.get('engine')} jev_ok={status.get('jev_ok')}")
        ok &= status.get("model") == "gpt-5.6-sol-free"
        for path in ("/", "/app.js", "/styles.css", "/icon-glow.svg"):
            r = urllib.request.urlopen(base + path, timeout=10)
            body = r.read()
            log(f"{path:18} {r.status} · {len(body)} bytes")
            ok &= r.status == 200 and bool(body)

        assert get(base, "/api/collect/processes?limit=2").get("granted") is False
        post(base, "/api/permissions/agree", {"all": True})
        processes = get(base, "/api/collect/processes?limit=3")
        system = get(base, "/api/collect/system")
        caps = get(base, "/api/notify/capabilities", 45)
        top = (processes.get("items") or [{}])[0]
        log(f"进程 {processes.get('count')} 条 · top={top.get('name')} {top.get('mem_mb')}MB")
        log(f"系统 {system.get('hostname')} · 磁盘 {len(system.get('disks') or [])} 个")
        ok &= processes.get("count", 0) > 0 and (top.get("mem_mb") or 0) > 100
        ok &= bool(system.get("hostname")) and bool(system.get("disks"))
        ok &= any(x.get("platform") == "windows" and x.get("ready") for x in caps.get("adapters", []))

        try:
            post(base, "/api/permissions/agree", {"all": "false"})
            ok = False
            log("✗ 非布尔 all 被错误接受")
        except urllib.error.HTTPError as e:
            ok &= e.code == 422
        try:
            post(base, "/api/notify/journal", {"title": "fake"}, headers={"Origin": "https://evil.example"})
            ok = False
            log("✗ 跨源 journal 被错误接受")
        except urllib.error.HTTPError as e:
            ok &= e.code == 403
        post(base, "/api/permissions/revoke", {})
        ok &= get(base, "/api/collect/processes?limit=2").get("granted") is False
    except Exception as e:
        log(f"✗ 冒烟测试异常：{type(e).__name__}: {e}")
        ok = False
    finally:
        if service_pid:
            _terminate_tree(service_pid)
        if proc is not None:
            _terminate_tree(proc.pid)
        time.sleep(2)
        shutil.rmtree(tmp_appdata, ignore_errors=True)
    log("✓ 冒烟测试全部通过" if ok else "✗ 冒烟测试有失败项")
    return ok


def main() -> int:
    args = sys.argv[1:]
    section("幕僚 Muliáo · 部署到 D 盘")
    log(f"项目目录 {HERE}")
    log(f"唯一安装目标 {INSTALL_DIR}")
    if "--uninstall" in args:
        return 0 if uninstall_old() else 1
    if "--no-build" not in args and not build():
        return 1
    if "--no-build" in args and (not os.path.isfile(SETUP_EXE) or not os.path.isfile(DIST_EXE)):
        log("✗ dist 中缺少现有产物")
        return 1
    if not uninstall_old():
        return 1
    if not install_to_d():
        return 1
    if not make_shortcuts():
        return 1
    ok = smoke_test()
    section("部署完成" if ok else "部署完成（冒烟测试失败）")
    log(f"安装位置 {INSTALL_DIR}")
    log(f"安装包 {SETUP_EXE}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
