param([int]$Port = 8090)
# MyJev service stopper: kills every process LISTENing on $Port (ASCII-only for PS 5.1 consoles).
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
if (-not $pids -or $pids.Count -eq 0) {
    Write-Output "[stop] port $Port is free, nothing to do"
    exit 0
}
foreach ($procId in $pids) {
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    $name = if ($p) { $p.ProcessName } else { "?" }
    Write-Output "[stop] killing PID=$procId ($name) on port $Port"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 600
$left = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($left.Count -eq 0) {
    Write-Output "[stop] done, port $Port released"
    exit 0
} else {
    Write-Output "[stop] WARN: port $Port still held (try: Stop-Process -Force as admin)"
    exit 1
}
