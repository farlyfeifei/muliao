# 本地 PyInstaller hook：覆盖 _pyinstaller_hooks_contrib 里坏掉的 hook-webrtcvad.py。
#
# 上游那个 hook 调 copy_metadata('webrtcvad')，但 webrtcvad 是裸 .py + _webrtcvad.pyd、
# 没有安装元数据（PackageNotFoundError），导致整个打包在加载 hook 阶段就崩。
# webrtcvad 运行时并不需要自己的 dist-info 元数据，所以这里什么都不收集：
# 模块本体由 hiddenimports('webrtcvad','_webrtcvad') 拉进来，C 扩展 .pyd 由
# PyInstaller 的扩展模块机制自动收。
#
# hookspath=['pyinstaller-hooks'] 在 muliao.spec 里注册，本地 hook 优先级高于 contrib。

hiddenimports = ['_webrtcvad']
