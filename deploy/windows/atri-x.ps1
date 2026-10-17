# ATRI-X Windows 本地部署的极薄引导壳：只负责找到 Python 并转发全部参数。
#
# 真正的实现在同目录的 atri_x.py（Python 重写版，纯标准库）。上游 atri-x.ps1
# 是 822 行的完整 PowerShell 实现；Python 版把它整段搬到 atri_x.py，这里只留
# 一个既能双击、也能被计划任务调用的入口。
#
# 用法：
#   .\deploy\windows\atri-x.ps1 status
#   .\deploy\windows\atri-x.ps1 logs -Follow -Tail 200
#
# 注意：本文件刻意保持极薄——**不要**在这里再加业务逻辑，否则又会分裂成两份实现。

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $scriptDir 'atri_x.py'

if (-not (Test-Path -LiteralPath $entry)) {
    Write-Error "未找到 $entry（Python 版实现应当与引导壳同目录）"
    exit 127
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Error '未找到 python：请安装 Python 3.11+ 并加入 PATH。'
    exit 127
}

& $python.Source $entry @args
exit $LASTEXITCODE
