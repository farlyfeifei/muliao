@echo off
chcp 65001 >nul
REM ============================================================
REM  幕僚 Muliáo — 一键打包脚本（Windows）
REM  产物：dist\Muliáo.exe（单文件可执行）+ dist\幕僚Muliao-Setup.exe（安装器）
REM  用法：在 muliao 目录双击运行，或命令行 build.bat
REM ============================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set MULIAO_NO_BROWSER=1

echo [1/3] 生成图标 icon.ico ...
python make_icon.py
if errorlevel 1 ( echo 图标生成失败 & exit /b 1 )

echo [2/3] PyInstaller 打包单文件 exe ...
python -m PyInstaller muliao.spec --noconfirm --clean
if errorlevel 1 ( echo PyInstaller 打包失败 & exit /b 1 )

echo [3/3] Inno Setup 生成安装包 ...
REM 自动探测 ISCC.exe（常见安装位置）
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo [警告] 未找到 ISCC.exe（Inno Setup）。已生成 dist\Muliáo.exe，可双击直接运行。
  echo        如需安装器，请安装 Inno Setup 6 后重跑本脚本，或手动执行：
  echo        "ISCC.exe 完整路径" installer.iss
  goto :done
)
"%ISCC%" installer.iss
if errorlevel 1 ( echo Inno Setup 编译失败 & exit /b 1 )

:done
echo.
echo 完成！产物在 dist\ ：
dir /b dist
endlocal
