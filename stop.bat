@echo off
chcp 65001 >nul
title Y2A-Auto - Stop
setlocal

rem Project root: only stop this project processes (DB_Sync 5000 / PLC 5100 stay alive)
set "APP_ROOT=D:\Program Files\Y2A-Auto"

echo.
echo ========================================
echo   Stopping Y2A-Auto (app / yt-dlp / ffmpeg / port 5300)
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $root='%APP_ROOT%'; $self=$PID; $parentPid=(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $self)).ParentProcessId; $killed=New-Object System.Collections.ArrayList; for ($round=1; $round -le 5; $round++) { $targets=New-Object System.Collections.ArrayList; foreach ($o in @(Get-NetTCPConnection -LocalPort 5300 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)) { if ($o -and -not $targets.Contains([int]$o)) { [void]$targets.Add([int]$o) } }; foreach ($p in (Get-CimInstance Win32_Process)) { if (-not $p.CommandLine) { continue }; if ($p.ProcessId -eq $self -or $p.ProcessId -eq $parentPid) { continue }; $cl=$p.CommandLine; $hit=$false; if ($cl.Contains($root) -and $cl -match '(?i)python|yt[_-]dlp|ffmpeg') { $hit=$true }; if ($cl -match '(?i)-m\s+yt_dlp|yt-dlp\.exe|yt-dlp\.py') { $hit=$true }; if ($p.Name -match '(?i)^Y2A') { $hit=$true }; if ($p.Name -match '(?i)^python' -and $cl -match '(?i)\bapp\.py\b') { $hit=$true }; if ($hit -and -not $targets.Contains([int]$p.ProcessId)) { [void]$targets.Add([int]$p.ProcessId) } }; if ($targets.Count -eq 0) { break }; foreach ($t in $targets) { if ($killed.Contains($t)) { continue }; $proc=Get-Process -Id $t -ErrorAction SilentlyContinue; if ($proc) { Write-Output ('  结束 ' + $proc.ProcessName + ' (PID ' + $t + ')'); Stop-Process -Id $t -Force -ErrorAction SilentlyContinue; if (-not $killed.Contains($t)) { [void]$killed.Add($t) } } }; Start-Sleep -Seconds 2 }; if ($killed.Count -eq 0) { Write-Output '未发现正在运行的 Y2A-Auto 进程' } else { Write-Output ('共结束 ' + $killed.Count + ' 个进程（含下载器/ffmpeg 子进程）') }; $left=@(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $self -and $_.ProcessId -ne $parentPid -and $_.CommandLine -and $_.CommandLine.Contains($root) -and ($_.CommandLine -match '(?i)python|yt[_-]dlp|ffmpeg') }); if ($left.Count -gt 0) { Write-Output '警告：以下进程仍未退出，请手动结束'; foreach ($l in $left) { Write-Output ('  PID ' + $l.ProcessId + ' ' + $l.Name) } } else { Write-Output 'Y2A-Auto 已全部停止，端口 5300 已释放' }"

echo.
echo Only this project is affected: %APP_ROOT%
echo DB_Sync (5000) and PLC collector (5100) are left untouched.
echo.
pause
