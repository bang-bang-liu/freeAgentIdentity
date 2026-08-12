$ErrorActionPreference = "SilentlyContinue"
$ports = @(8000)
foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -State Listen -LocalPort $port
    foreach ($connection in $connections) {
        if ($connection.OwningProcess -and $connection.OwningProcess -ne $PID) {
            Stop-Process -Id $connection.OwningProcess -Force
            Write-Host "Stopped PID $($connection.OwningProcess) on port $port"
        }
    }
}
