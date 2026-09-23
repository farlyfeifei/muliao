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

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

block_cipher = None

# ---- 用 collect_submodules 把动态导入风险最高的几棵树整体拉进来 ----
_hidden = []
_hidden += collect_submodules('uvicorn')     # uvicorn 用 import_from_string 动态加载 loop/http/ws/lifespan
_hidden += collect_submodules('psutil')      # psutil 平台子模块 _pswindows + C 扩展 _psutil_windows
_hidden += collect_submodules('anyio')       # anyio 后端（asyncio/trio）按字符串动态导入
_hidden += collect_submodules('starlette')   # starlette / fastapi 运行时组件
_hidden += collect_submodules('fastapi')
_hidden += collect_submodules('voice')       # 言出法随语音子系统：44 模块，server.py 顶层 import voice.api
# 语音重库全是「函数内延迟 import」（sherpa_onnx/pyaudio/webrtcvad 在 capture/asr/tts 里才导入），
# PyInstaller 静态分析抓不到 → 必须整树拉进来，否则 exe 里语音功能 ImportError。
_hidden += collect_submodules('sherpa_onnx') # 本地 ASR/TTS 推理（onnxruntime 绑定）
_hidden += collect_submodules('numpy')       # 音频帧数值处理
_hidden += collect_submodules('pyaudio')     # 麦克风采集/播放（_portaudio C 扩展）

# ---- 显式补齐：本项目模块 + 函数内动态 import + pywin32 + 标准库 ----
_hidden += [
    # 本项目模块（被 server.py 顶层 import，但显式列出更稳妥）
    'collectors', 'permissions', 'notifier', 'machine_tools', 'runtime_paths',
    'swarm_models', 'swarm_store', 'capsule', 'swarm', 'swarm_api', 'swarm_runtime',
    'action_gate', 'machine_control', 'model_catalog',
    # 语音子系统：server.py 顶层 import voice.api；其余 voice.* 由 service 延迟导入
    'voice', 'voice.api', 'voice.service', 'voice.runtime', 'voice.config',
    'voice.capture', 'voice.wake', 'voice.asr_local', 'voice.asr_streaming',
    'voice.asr_mimo', 'voice.asr_fallback', 'voice.tts_local', 'voice.tts_mimo',
    'voice.tts_vits', 'voice.audio_player', 'voice.permission_gate', 'voice.safety',
    'voice.events', 'voice.models', 'voice.engine', 'voice.orchestrator',
    'voice.jev_router', 'voice.jev_cache', 'voice.goal_router', 'voice.goal_runtime',
    # 语音延迟导入的第三方库（函数体内 import，静态分析抓不到）
    'sherpa_onnx', 'pyaudio', '_portaudio', 'webrtcvad', '_webrtcvad', 'numpy',
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

# ---- 语音原生运行库 DLL（sherpa_onnx 的 onnxruntime/c-api、pyaudio 的 portaudio 等）----
# 这些是 C 扩展，静态分析不会自动收；用 collect_dynamic_libs 把每个包的二进制拉全。
_binaries = []
for _pkg in ('sherpa_onnx', 'pyaudio', 'webrtcvad'):
    try:
        _binaries += collect_dynamic_libs(_pkg)
    except Exception:
        pass

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=_binaries,
    # static/ 整个目录（含 icon-glow.svg 与 static/voice/**）；voice-models.json 必须放包顶层：
    # voice/service.py 用 Path(__file__).parents[1]/"voice-models.json" 定位，冻结态即 _MEIPASS 根。
    datas=[('static', 'static'), ('voice-models.json', '.')],
    hiddenimports=hiddenimports,
    hookspath=['pyinstaller-hooks'],   # 本地 hook 覆盖 contrib 里坏掉的 hook-webrtcvad.py
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
