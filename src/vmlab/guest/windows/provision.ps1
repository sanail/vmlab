# Provisions a Windows Base guest for vmlab; run elevated, as the Guest user. Idempotent.
#
# -Params: a JSON file {"user", "password", "key"} readable by the user alone; deleted once read.
# -Log gets what it does, and its last line is "vmlab-host-key: <sshd's ed25519 public key>";
# it exits 1 after an error, which ends the log.
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([Parameter(Mandatory = $true)][string]$Params, [Parameter(Mandatory = $true)][string]$Log)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$p = Get-Content -Raw -LiteralPath $Params | ConvertFrom-Json
Remove-Item -Force -LiteralPath $Params
Set-Content -LiteralPath $Log -Value @() -Encoding UTF8
function Say($line) { Add-Content -LiteralPath $Log -Value $line -Encoding UTF8 }
trap { Say "failed: $_ ($($_.InvocationInfo.PositionMessage))"; exit 1 }

function Set-Value($path, $name, $value, $type = 'DWord') {
    if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
    New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
}

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

Say 'no updates, sleep, screen saver or lock screen'
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1
Set-Value 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoRebootWithLoggedOnUsers 1
foreach ($service in 'wuauserv', 'UsoSvc') {
    Stop-Service $service -Force -ErrorAction SilentlyContinue
    Set-Service $service -StartupType Disabled -ErrorAction SilentlyContinue
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
