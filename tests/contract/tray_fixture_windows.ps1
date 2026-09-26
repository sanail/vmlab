# A Tray icon for the contract tests, in a Windows Guest: a WinForms NotifyIcon with a ContextMenuStrip.
#
#     vmlab-tray-XXXX.exe -NoProfile -STA -ExecutionPolicy Bypass -File tray_fixture_windows.ps1 RECORD
#
# (vmlab-tray-XXXX.exe: a copy of powershell.exe, so the app has a name of its own.)
# Its Tray menu: Open, a separator, Settings (a submenu: Advanced, and Dark mode,
# checked), Pinned, checked, Update, disabled, Help (a submenu: About, and Links, a
# submenu: Website), and Archive, disabled (a submenu: Old). Choosing an item, a
# submenu's parent too, appends its text (with its mnemonic) to the file RECORD.
# Windows PowerShell and .NET only: the Guest installs nothing.
param([string]$Record)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$menu = New-Object System.Windows.Forms.ContextMenuStrip
function Entry([string]$text, [bool]$checked = $false, [bool]$enabled = $true) {
    $item = New-Object System.Windows.Forms.ToolStripMenuItem($text)
    $item.Checked = $checked
    $item.Enabled = $enabled
    $item.add_Click({ param($sender) Add-Content -LiteralPath $Record -Value $sender.Text -Encoding UTF8 })
    $item
}
[void]$menu.Items.Add((Entry '&Open'))
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$settings = Entry '&Settings'  # a Click handler of its own: reading or choosing a submenu's parent records it
[void]$settings.DropDownItems.Add((Entry '&Advanced'))
[void]$settings.DropDownItems.Add((Entry '&Dark mode' $true))
[void]$menu.Items.Add($settings)
[void]$menu.Items.Add((Entry '&Pinned' $true))
[void]$menu.Items.Add((Entry '&Update' $false $false))
$help = Entry '&Help'
[void]$help.DropDownItems.Add((Entry '&About'))
$links = Entry '&Links'
[void]$links.DropDownItems.Add((Entry '&Website'))
[void]$help.DropDownItems.Add($links)
[void]$menu.Items.Add($help)
$archive = Entry '&Archive' $false $false
[void]$archive.DropDownItems.Add((Entry '&Old'))
[void]$menu.Items.Add($archive)

$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Text = 'vmlab tray fixture'
$icon.ContextMenuStrip = $menu
$icon.Visible = $true
'tray icon up'
[System.Windows.Forms.Application]::Run()
