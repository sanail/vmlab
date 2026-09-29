# Gives a new Lab copy the Lab language; run elevated, over ssh, as the autologin user, whose profile
# the ssh session loads. The next sign-in shows it. Its last line starts with "vmlab-language-set: ". Its pack is installed already (add-language.ps1).
#
# One tag names the display language and the regional formats, as on macOS and Linux. The Lab
# language comes first in the user's languages, as a Windows installed in it would have it (Store apps
# such as Notepad take their language from that list), with English (United States) after it. Its
# keyboard is installed next to US, and US stays the default, active at sign-in: g.press sends keys,
# not characters, and would type Cyrillic under a Russian one. g.type sends characters, whatever the
# keyboard. The system locale for non-Unicode programs stays as provisioning left it (UTF-8).
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([Parameter(Mandatory = $true)][string]$Language)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$self = $PSCommandPath
trap { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $self; [Console]::Error.WriteLine("$_"); exit 1 }
Remove-Item -Force -LiteralPath $self

# A task at sign-in, 30 s after it (\Microsoft\Windows\International\Synchronize Language Settings),
# writes the user's languages back as they were: a list set before it ran in this sign-in is lost.
# So first the desktop's sign-in (explorer), then that task's run after it.
$deadline = (Get-Date).AddSeconds(180)
while (-not ($explorer = Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1)) {
    if ((Get-Date) -gt $deadline) { throw 'the autologin user did not sign in to the desktop within 3 minutes' }
    Start-Sleep -Seconds 2
}
function Sync { Get-ScheduledTask -TaskPath '\Microsoft\Windows\International\' -TaskName 'Synchronize Language Settings' -ErrorAction SilentlyContinue }
while (($sync = Sync) -and $sync.State -ne 'Disabled') {
    if (($sync | Get-ScheduledTaskInfo).LastRunTime -gt $explorer.StartTime -and $sync.State -ne 'Running') { break }
    if ((Get-Date) -gt $deadline) { throw "the sign-in's Synchronize Language Settings task did not run within 3 minutes" }
    Start-Sleep -Seconds 2
}

$us = '0409:00000409'
$languages = New-WinUserLanguageList $Language
if ($Language -ne 'en-US') {
    $languages.Add('en-US')
    $english = $languages | Where-Object { $_.LanguageTag -eq 'en-US' }
    $english.InputMethodTips.Clear()
    $english.InputMethodTips.Add($us)
}
Set-WinUserLanguageList $languages -Force -WarningAction SilentlyContinue
Set-WinUILanguageOverride -Language $Language -WarningAction SilentlyContinue
Set-WinDefaultInputMethodOverride -InputTip $us
Set-Culture $Language
Set-WinHomeLocation -GeoId ([Globalization.RegionInfo]::new($Language)).GeoId
Set-SystemPreferredUILanguage -Language $Language
# The welcome screen and new users: the sign-in screen, and system accounts' messages.
Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
$desktop = Get-ItemProperty 'HKCU:\Control Panel\Desktop'
"vmlab-language-set: display language $($desktop.PreferredUILanguages -join ','), formats $((Get-ItemProperty 'HKCU:\Control Panel\International').LocaleName), keyboards $((Get-WinUserLanguageList).InputMethodTips -join ' ')"
