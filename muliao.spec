# -*- mode: python ; coding: utf-8 -*-
# 幕僚 Muliáo — PyInstaller 打包配置（单文件 / 无控制台窗口）
# 重新构建：pyinstaller muliao.spec --noconfirm --clean   （或直接跑 build.bat）
# 注意：static/ 整个目录通过 datas 打进去；运行时 server.py 用 sys._MEIPASS 定位。
#
# 关键教训（务必保留）：collectors.py / notifier.py 里大量「函数内 import」
# （import psutil / import win32gui / import win32process / import ctypes /
#   import plistlib）以及 collectors._has() 用 __import__(mod) 按字符串动态导入，
# PyInstaller 静态分析**抓不全**。一旦漏包，exe 里采集源 available=False，
# 用户授权了也采不到任何数据。故此处用 collect_submodules 把整棵子模块树拉进来，
# 并对每个动态导入的模块显式列出 hiddenimports。改完必须实测 /api/permissions
# 的 available 与 /api/collect/<scope> 的真实数据，不能只看打包日志没报错。

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# ---- 用 collect_submodules 把动态导入风险最高的几棵树整体拉进来 ----
_hidden = []
_hidden += collect_submodules('uvicorn')     # uvicorn 用 import_from_string 动态加载 loop/http/ws/lifespan
_hidden += collect_submodules('psutil')      # psutil 平台子模块 _pswindows + C 扩展 _psutil_windows
_hidden += collect_submodules('anyio')       # anyio 后端（asyncio/trio）按字符串动态导入
_hidden += collect_submodules('starlette')   # starlette / fastapi 运行时组件
_hidden += collect_submodules('fastapi')

# ---- 显式补齐：本项目模块 + 函数内动态 import + pywin32 + 标准库 ----
_hidden += [
    # 本项目模块（被 server.py 顶层 import，但显式列出更稳妥）
    'collectors', 'permissions', 'notifier', 'machine_tools', 'runtime_paths',
    'swarm_models', 'swarm_store', 'capsule', 'swarm', 'swarm_api', 'swarm_runtime',
    # pywin32：collectors.windows() 里函数内 import win32gui / win32process，
    # 且 collectors._has('win32gui') 走 __import__ 动态导入 → 必须显式
    'win32gui', 'win32process', 'win32api', 'win32con', 'win32event',
    'pywintypes', 'pythoncom', 'win32timezone',
    # psutil 关键平台/C 扩展子模块（双保险，collect_submodules 已含）
    'psutil', 'psutil._pswindows', 'psutil._psutil_windows', 'psutil._common', 'psutil._psposix',
    # 标准库：collectors.system_info() 内 import ctypes / socket / platform；
    # notifier.MacOSAdapter 内 import plistlib；其余在函数体内动态 import
    'sqlite3', 'plistlib', 'ctypes', 'socket', 'platform', 'shutil', 'subprocess',
    'zipfile', 'json', 'datetime', 'xml.etree.ElementTree', 'webbrowser', 'asyncio',
    'threading', 'logging', 'logging.handlers', 'tempfile', 'time', 're', 'os',
    # HTTP / ASGI 栈
    'httpx', 'h11', 'httpcore', 'anyio', 'sniffio', 'idna', 'certifi',
    'click', 'typing_extensions', 'annotated_types', 'pydantic',
    # uvicorn 动态加载点（import_from_string 字符串，双保险）
    'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
    'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off',
    'uvicorn.middleware', 'uvicorn.middleware.message_logger',
]

# 去重，保持稳定顺序
_seen = set()
hiddenimports = []
for _m in _hidden:
    if _m not in _seen:
        _seen.add(_m)
        hiddenimports.append(_m)

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],          # 把 static/ 整个目录打进去（含 icon-glow.svg）
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Muliáo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                                # 有 UPX 才压缩，没有也不报错
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                           # 无控制台窗口（windowed）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',                         # exe 图标（项目根目录）
)
