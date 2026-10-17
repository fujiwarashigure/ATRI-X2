@echo off
REM ATRI-X Windows 本地部署的极薄引导壳：只负责找到 Python 并转发全部参数。
REM 真正的实现在同目录的 atri_x.py（Python 重写版，纯标准库）。
REM 上游 atri-x.cmd 只是转发给 atri-x.ps1；这里把转发目标换成 Python。
setlocal
set "ATRIX_WINDOWS_DIR=%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [x] 未找到 python：请安装 Python 3.11+ 并加入 PATH。
    exit /b 127
)
python "%ATRIX_WINDOWS_DIR%atri_x.py" %*
exit /b %ERRORLEVEL%
