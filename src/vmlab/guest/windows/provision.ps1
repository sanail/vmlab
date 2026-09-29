# Provisions a Windows Base guest for vmlab; run elevated, as the Guest user. Idempotent.
#
# -Params: a JSON file {"user", "password", "key", "utc": the Host's time in ms when it sent them}
# readable by the user alone; deleted once read.
# -Log gets what it does, and its last line is "vmlab-host-key: <sshd's ed25519 public key>";
# it exits 1 after an error, which ends the log.
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([Parameter(Mandatory = $true)][string]$Params, [Parameter(Mandatory = $true)][string]$Log)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$since = [Diagnostics.Stopwatch]::StartNew()  # since the Host sent its time, give or take the call's start
$p = Get-Content -Raw -LiteralPath $Params | ConvertFrom-Json
Remove-Item -Force -LiteralPath $Params
Set-Content -LiteralPath $Log -Value @() -Encoding UTF8
function Say($line) { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 }
trap { Say "failed: $_ ($($_.InvocationInfo.PositionMessage))"; exit 1 }

function Set-Value($path, $name, $value, $type = 'DWord') {
    if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
    New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
}

Say 'the clock in UTC (read at the reboot)'
# As Linux and macOS Guests keep it: vmlab gives the VM a hardware clock in UTC (rtc.diffFromUTC),
# Windows reads it as UTC and shows it in time zone UTC, so its time is right whatever the Mac's
# time zone. Nothing sets the zone from the network: Windows' automatic time zone stays off.
Set-Value 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation' RealTimeIsUniversal 1
& tzutil.exe /s UTC
if ($LASTEXITCODE) { throw "tzutil /s UTC failed (exit $LASTEXITCODE)" }
Set-Value 'HKLM:\SYSTEM\CurrentControlSet\Services\tzautoupdate' Start 4
# Until the reboot Windows still reads the clock in its old zone: hours off in UTC, in what the
# rest of provisioning writes. The Host's time, now in time zone UTC, sets it right meanwhile.
[TimeZoneInfo]::ClearCachedData()
Set-Date -Date ([DateTimeOffset]::FromUnixTimeMilliseconds([long]$p.utc).UtcDateTime + $since.Elapsed) | Out-Null

Say 'elevation without a prompt (for this throwaway Guest only)'
$system = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
Set-Value $system ConsentPromptBehaviorAdmin 0
Set-Value $system PromptOnSecureDesktop 0

Say 'OpenSSH server'
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($capability.State -ne 'Installed') {
    Say '  installing it from Windows Update (a few minutes)'
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
# The Guest user is an administrator: sshd reads administrators' keys from here, and only
# if Administrators and SYSTEM alone may change the file.
$keys = "$env:ProgramData\ssh\administrators_authorized_keys"
[IO.File]::WriteAllText($keys, $p.key + "`n")
& icacls.exe $keys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE) { throw "icacls $keys failed" }
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($rule) { $rule | Set-NetFirewallRule -Enabled True -Profile Any }
else { New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Protocol TCP -LocalPort 22 -Action Allow -Profile Any | Out-Null }

Say "autologin as $($p.user)"
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-Value $winlogon AutoAdminLogon '1' String
Set-Value $winlogon DefaultUserName $p.user String
Set-Value $winlogon DefaultDomainName $env:COMPUTERNAME String
Remove-ItemProperty -Path $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
# Windows Hello-only sign-in turns autologin off.
Set-Value 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' DevicePasswordLessBuildVersion 0
# The password as an LSA secret, where Winlogon also looks: not readable from the registry
# by every user, as the DefaultPassword value is.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class VmlabLsa {
    [StructLayout(LayoutKind.Sequential)] struct UNICODE_STRING { public ushort Length, MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory, ObjectName; public uint Attributes; public IntPtr SecurityDescriptor, SecurityQualityOfService; }
    [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref OBJECT_ATTRIBUTES attributes, uint access, out IntPtr policy);
    [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr policy, ref UNICODE_STRING key, ref UNICODE_STRING data);
    [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
    [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
    static UNICODE_STRING Str(string s) {
        return new UNICODE_STRING { Buffer = Marshal.StringToHGlobalUni(s), Length = (ushort)(s.Length * 2), MaximumLength = (ushort)(s.Length * 2 + 2) };
    }
    public static void Store(string name, string secret) {
        var attributes = new OBJECT_ATTRIBUTES();
        IntPtr policy;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x00000800 | 0x00000020, out policy);  // POLICY_CREATE_SECRET | POLICY_TRUST_ADMIN
        if (status != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(status));
        try {
            var key = Str(name); var data = Str(secret);
            status = LsaStorePrivateData(policy, ref key, ref data);
            if (status != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(status));
        } finally { LsaClose(policy); }
    }
}
'@ -Language CSharp
[VmlabLsa]::Store('DefaultPassword', $p.password)

Say 'no updates (Windows, Store apps, Edge), sleep, screen saver or lock screen'
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoRebootWithLoggedOnUsers 1
# The policy covers the operating system's updates only. Windows Update Medic sets wuauserv back to
# start on demand, and minutes after every boot of a clone the Microsoft Store updates its apps
# through it (Notepad among them, during a Run): about 2 GB of each clone's disk. Its tasks and
# the Medic are protected from administrators: SYSTEM turns them off, in a task run once.
$noUpdates = @'
$log = Join-Path $env:ProgramData 'vmlab-no-updates.log'
Set-Content -LiteralPath $log -Value @()
$tasks = @{
    '\Microsoft\Windows\InstallService\' = 'ScanForUpdates', 'ScanForUpdatesAsUser', 'SmartRetry'
    '\Microsoft\Windows\WindowsUpdate\' = 'Scheduled Start'
    '\Microsoft\Windows\UpdateOrchestrator\' = '*'
}
foreach ($path in $tasks.Keys) {
    foreach ($task in Get-ScheduledTask -TaskPath $path -ErrorAction SilentlyContinue) {
        if (-not @($tasks[$path] | Where-Object { $task.TaskName -like $_ }).Count) { continue }
        # A task Windows removes meanwhile ("Element not found") needs nothing.
        Disable-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue | Out-Null
    }
}
foreach ($service in 'WaaSMedicSvc', 'wuauserv', 'UsoSvc') {
    Stop-Service $service -Force -ErrorAction SilentlyContinue
    try { Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$service" Start 4 -ErrorAction Stop }
    catch { Add-Content -LiteralPath $log -Value "failed: $service stays on: $_" }
}
Add-Content -LiteralPath $log -Value 'done'
'@
$script = Join-Path $env:ProgramData 'vmlab-no-updates.ps1'
$result = Join-Path $env:ProgramData 'vmlab-no-updates.log'
Set-Content -LiteralPath $script -Value $noUpdates -Encoding ASCII
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $result
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
Register-ScheduledTask -TaskName 'vmlab-no-updates' -Action $action -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName 'vmlab-no-updates'
$done = $false
for ($i = 0; $i -lt 120 -and -not $done; $i++) {
    Start-Sleep -Seconds 1
    $done = @(Get-Content -LiteralPath $result -ErrorAction SilentlyContinue) -contains 'done'
}
Unregister-ScheduledTask -TaskName 'vmlab-no-updates' -Confirm:$false
$failures = @(Get-Content -LiteralPath $result -ErrorAction SilentlyContinue | Where-Object { $_ -like 'failed:*' })
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $script, $result
if (-not $done) { throw 'turning Windows Update off as SYSTEM did not finish in 2 minutes' }
if ($failures) { throw ($failures -join '; ') }
# Edge updates itself apart from Windows Update: its tasks run at every boot of a clone and replace
# its version during a Run (and in the Base guest while vmlab installs a language into it).
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate' UpdateDefault 0
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate' AutoUpdateCheckPeriodMinutes 0
Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdate*' -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
foreach ($service in 'edgeupdate', 'edgeupdatem') {
    if (Get-Service $service -ErrorAction SilentlyContinue) { Stop-Service $service -Force -ErrorAction SilentlyContinue; Set-Service $service -StartupType Disabled }
}
foreach ($setting in 'standby-timeout-ac', 'monitor-timeout-ac', 'hibernate-timeout-ac', 'disk-timeout-ac') { & powercfg.exe /change $setting 0 }
& powercfg.exe /hibernate off
& powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' NoLockScreen 1
Set-Value 'HKCU:\Control Panel\Desktop' ScreenSaveActive '0' String
# "Let's finish setting up your device" and tips, which cover the desktop after a boot.
Set-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement' ScoobeSystemSettingEnabled 0
Set-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager' SubscribedContent-310093Enabled 0
Set-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager' SoftLandingEnabled 0

Say 'one keyboard: the display language''s own'
# As Linux and macOS Guests have it: Windows Setup adds the keyboard of the region it was given (a
# Russian one, say, next to US), and every Lab would get it. A Lab in another language gets its
# keyboard next to US when its copy is made (set-language.ps1).
# A task 30 s after sign-in (\Microsoft\Windows\International\Synchronize Language Settings) writes
# the list back as it was: first its run in this sign-in.
$explorer = Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1
for ($i = 0; $explorer -and $i -lt 90; $i++) {
    $sync = Get-ScheduledTask -TaskPath '\Microsoft\Windows\International\' -TaskName 'Synchronize Language Settings' -ErrorAction SilentlyContinue
    if (-not $sync -or $sync.State -eq 'Disabled' -or (($sync | Get-ScheduledTaskInfo).LastRunTime -gt $explorer.StartTime -and $sync.State -ne 'Running')) { break }
    Start-Sleep -Seconds 2
}
Add-Type -Name UiLanguage -Namespace Vmlab -MemberDefinition '[DllImport("kernel32.dll")] public static extern ushort GetUserDefaultUILanguage();'
$display = [Globalization.CultureInfo]::new([int][Vmlab.UiLanguage]::GetUserDefaultUILanguage()).Name
Set-WinUserLanguageList (New-WinUserLanguageList $display) -Force
Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
Say "  $((Get-WinUserLanguageList | ForEach-Object { "$($_.LanguageTag) [$($_.InputMethodTips -join ' ')]" }) -join ', ')"

Say 'no recent documents'
# Every Run opens files (Staged documents): each would leave one more shortcut in Recent Items.
Set-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer' NoRecentDocsHistory 1
Set-Value 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer' ClearRecentDocsOnExit 1

Say 'no OneDrive'
# OneDrive starts at logon (the user's Run key, then a Scheduled Task ten minutes later) and a
# few minutes on shows "Turn On Windows Backup" at the bottom right of the screen, over the app
# under test (the prompt is OneDrive's own). The policy makes OneDrive quit as soon as it starts,
# however it is started; its prompts to back up the user's folders are blocked as well, and
# neither the Run key nor its Scheduled Tasks start it any more.
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive' DisableFileSyncNGSC 1
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\OneDrive' KFMBlockOptIn 1
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name OneDrive -ErrorAction SilentlyContinue
Get-ScheduledTask -TaskName 'OneDrive *' -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null
Get-Process -Name OneDrive, OneDrive.Sync.Service -ErrorAction SilentlyContinue | Stop-Process -Force

Say 'UTF-8 as the code page of every program (after the reboot)'
$codepages = 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage'
foreach ($name in 'ACP', 'OEMCP', 'MACCP') { Set-Value $codepages $name '65001' String }

Say 'native code for PowerShell (NGEN; about a minute)'
# Most calls start PowerShell (recipes, the UI helper, the vmrun Channel's runner, vmlab's call server
# once per boot). A new Windows has no native images for its own architecture yet, so each
# start compiles PowerShell's assemblies (~1 s more per start).
# Windows makes them in idle maintenance, which a Guest loses when it is restored to its Clean
# state: they go in the Base guest. First load what the calls use (call.ps1, the call server, the
# UI helper), then compile every assembly this session has loaded from the GAC.
$null = '{}' | ConvertFrom-Json | ConvertTo-Json
$null = New-Object -ComObject Schedule.Service
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase, System.Windows.Forms, System.Drawing, System.Web.Extensions
$ngen = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'ngen.exe'
foreach ($assembly in [AppDomain]::CurrentDomain.GetAssemblies() | Where-Object { $_.GlobalAssemblyCache }) {
    & $ngen install $assembly.Location /nologo 2>&1 | Out-Null
    if ($LASTEXITCODE) { Say "  ngen failed for $($assembly.GetName().Name) (exit $LASTEXITCODE): its calls start slower" }
}

$hostKey = "$env:ProgramData\ssh\ssh_host_ed25519_key.pub"
for ($i = 0; -not (Test-Path $hostKey) -and $i -lt 100; $i++) { Start-Sleep -Milliseconds 100 }  # sshd writes it when it first starts
Say ("vmlab-host-key: " + (Get-Content -Raw $hostKey).Trim())
