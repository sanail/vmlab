# Installs a language pack into a Windows Base guest (-Language ru-RU), or only lists the installed
# ones (-List); run elevated, over ssh, as the Guest user. The last line of its output is
# "vmlab-languages: <every language whose pack is installed>"; it exits 1 after an error.
#
# The install must change nothing in the Base guest but the pack: Lab copies made before it stay
# current. Install-Language downloads the pack through Delivery Optimization, but installs it only
# while the Windows Update service runs: without it, it waits out its own hour and times out. So the
# service is set to start on demand and started for the install alone, then stopped and disabled
# again. Its scans stay off meanwhile: the Update Orchestrator (UsoSvc), which starts them, stays
# disabled, and so do the scheduled tasks for OS, Store and Edge updates. A check after the install proves
# updates are off as provisioning left them. It may add the language to the user's languages
# and keyboards: they are put back as they were, and checked, since every copy made later has them.
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([string]$Language, [switch]$List)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$self = $PSCommandPath
trap { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $self; [Console]::Error.WriteLine("$_"); exit 1 }
Remove-Item -Force -LiteralPath $self

function Installed {
    # A language counts once its pack is in: Features alone (a keyboard, speech) do not show it.
    @(Get-InstalledLanguage | Where-Object { "$($_.LanguagePacks)" -notin '', 'None' } | ForEach-Object { $_.LanguageId })
}

# What provisioning's no-updates step leaves: the update services disabled, and the tasks that
# would start them or update Store apps off.
function Updates {
    $state = foreach ($service in 'WaaSMedicSvc', 'wuauserv', 'UsoSvc') {
        "$service=$((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$service").Start)"
    }
    $tasks = @{
        '\Microsoft\Windows\InstallService\' = 'ScanForUpdates', 'ScanForUpdatesAsUser', 'SmartRetry'
        '\Microsoft\Windows\WindowsUpdate\' = 'Scheduled Start'
        '\Microsoft\Windows\UpdateOrchestrator\' = '*'
    }
    foreach ($path in $tasks.Keys) {
        foreach ($task in Get-ScheduledTask -TaskPath $path -ErrorAction SilentlyContinue) {
            if (@($tasks[$path] | Where-Object { $task.TaskName -like $_ }).Count -and $task.State -ne 'Disabled') { $state += "task $path$($task.TaskName) is $($task.State)" }
        }
    }
    foreach ($service in 'edgeupdate', 'edgeupdatem') {
        if (Get-Service $service -ErrorAction SilentlyContinue) { $state += "$service=$((Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$service").Start)" }
    }
    foreach ($task in Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdate*' -ErrorAction SilentlyContinue) {
        if ($task.State -ne 'Disabled') { $state += "task $($task.TaskName) is $($task.State)" }
    }
    $state -join '; '
}

function Keyboards { (Get-WinUserLanguageList | ForEach-Object { "$($_.LanguageTag) [$($_.InputMethodTips -join ' ')]" }) -join ', ' }

if (-not $List) {
    $updates = Updates
    if ($updates -notmatch '^WaaSMedicSvc=4; wuauserv=4; UsoSvc=4(; edgeupdatem?=4)*$') { throw "Windows Update is not off as provisioning leaves it ($updates): re-provision the Base guest" }
    $user = Get-WinUserLanguageList
    $keyboards = Keyboards
    "downloading and installing the $Language language pack"
    Set-Service wuauserv -StartupType Manual  # through the service manager: it ignores a registry edit until a reboot
    try {
        Start-Service wuauserv
        Install-Language -Language $Language | Out-Null
    } finally {
        Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
        Set-Service wuauserv -StartupType Disabled
    }
    Set-WinUserLanguageList $user -Force -WarningAction SilentlyContinue
    if (Get-WinUILanguageOverride) { throw "the install set a display language for the user: $(Get-WinUILanguageOverride)" }
    if ((Keyboards) -ne $keyboards) { throw "the user's languages and keyboards are $(Keyboards) after the install, not $keyboards as before" }
    "  the user's languages and keyboards are as before: $keyboards"
    if ((Updates) -ne $updates) { throw "Windows Update is not off after the install: $(Updates)" }
    "  Windows Update is off, as before"
    if ((Installed) -notcontains $Language) { throw "Windows does not list the $Language language pack as installed after Install-Language" }
}
"vmlab-languages: $(Installed)"
