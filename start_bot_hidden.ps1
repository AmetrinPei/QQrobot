# 隐藏窗口启动塔米米机器人（bot.py）
# 用法：powershell -ExecutionPolicy Bypass -File start_bot_hidden.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$dataDir = Join-Path $Root "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
$outLog = Join-Path $dataDir "bot_out.log"
$errLog = Join-Path $dataDir "bot_err.log"

function Stop-BotProcesses {
    $listen = netstat -ano | Select-String "LISTENING" | Select-String ":8080"
    foreach ($line in $listen) {
        if ("$line" -match "\s(\d+)\s*$") {
            $procId = $Matches[1]
            if ($procId -and $procId -ne "0") {
                & taskkill.exe /F /PID $procId 2>$null | Out-Null
            }
        }
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'bot(_niko)?\.py') } |
        ForEach-Object { & taskkill.exe /F /PID $_.ProcessId 2>$null | Out-Null }
}

Stop-BotProcesses
Start-Sleep -Seconds 1

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if (-not (Test-Path $python)) {
    throw "未找到 python.exe"
}

# Hidden：不弹控制台；日志写到 data/
$proc = Start-Process -FilePath $python -ArgumentList "bot.py" -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

Start-Sleep -Seconds 4
$ok = netstat -ano | Select-String "LISTENING" | Select-String ":8080"
if ($ok) {
    Write-Host "OK hidden bot.py PID=$($proc.Id) on :8080"
    Write-Host "logs: data\bot_out.log / data\bot_err.log"
} else {
    Write-Host "FAIL - see data\bot_err.log"
    if (Test-Path $errLog) { Get-Content $errLog -Tail 40 }
    exit 1
}
