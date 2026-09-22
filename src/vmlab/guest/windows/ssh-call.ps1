# SSH half of the Windows ssh Channel: an SSH session cannot reach the desktop, so the call
# runs as an interactive Scheduled Task, in the logged-in user's session, as the vmrun
# Channel's calls do. The Host has already put the call's script (and its stdin) in -Dir with
# scp; this script only runs it and prints its result (call.ps1). Exit 3: no result.
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([Parameter(Mandatory = $true)][string]$Id, [Parameter(Mandatory = $true)][string]$Dir, [Parameter(Mandatory = $true)][int]$Timeout)
$ErrorActionPreference = 'Stop'
$base = "$Dir\vmlab-call-$Id"
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$task = $service.NewTask(0)
# By SID: an SSH session's USERDOMAIN is not the name Task Scheduler takes.
$task.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$task.Principal.LogonType = 3  # interactive: runs in the user's desktop session
$task.Principal.RunLevel = 0  # not elevated, as over vmrun
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
$task.Settings.ExecutionTimeLimit = 'PT0S'
$action = $task.Actions.Create(0)
$action.Path = "$env:SystemRoot\System32\conhost.exe"
$action.Arguments = "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$base.ps1`""
$folder = $service.GetFolder('\')
$name = "vmlab-call-$Id"
[void]$folder.RegisterTaskDefinition($name, $task, 6, $null, $null, 3)
$state = 0
$lastResult = 0
try {
    [void]$folder.GetTask($name).Run($null)
    $deadline = (Get-Date).AddSeconds($Timeout + 5)
    $starting = (Get-Date).AddSeconds(10)  # by then it runs, or nobody is logged in to run it
    while (-not (Test-Path -LiteralPath "$base.result")) {
        $state = $folder.GetTask($name).State  # 2 queued, 3 ready (over), 4 running
        if ((Get-Date) -gt $deadline -or ($state -ne 4 -and (Get-Date) -gt $starting)) { break }
        Start-Sleep -Milliseconds 20
    }
    $lastResult = $folder.GetTask($name).LastTaskResult
} finally {
    $folder.DeleteTask($name, 0)
}
$leftovers = { @("$base.ps1", "$base.in", "$base.result") | Where-Object { Test-Path -LiteralPath $_ } }
if (-not (Test-Path -LiteralPath "$base.result")) {
    [Console]::Error.WriteLine("vmlab: the call's Scheduled Task wrote no result (task state $state, last result $lastResult); is $env:USERNAME logged in to the desktop?")
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath (& $leftovers)
    exit 3
}
[Console]::Out.Write([IO.File]::ReadAllText("$base.result"))
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath (& $leftovers)
exit 0  # the call's own exit code travels in the result; anything else here means this script failed
