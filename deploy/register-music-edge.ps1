$bat = Join-Path $PSScriptRoot 'start-music-edge.bat'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $bat + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'MusicEdge' -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName 'MusicEdge'
