# A window of tick states for the contract tests, in a Windows Guest: WinForms check boxes and radio buttons.
#
#     vmlab-chk-XXXX.exe -NoProfile -STA -ExecutionPolicy Bypass -File checks_fixture_windows.ps1 TITLE
#
# (vmlab-chk-XXXX.exe: a copy of powershell.exe, so the app has a name of its own.)
# Its window, titled TITLE: Pictures (ticked), Music (unticked), Some (in its middle
# state), radio buttons Light (chosen) and Dark, and a Save button.
# Windows PowerShell and .NET only: the Guest installs nothing.
param([string]$Title)
Add-Type -AssemblyName System.Windows.Forms

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width, $form.Height = 320, 300
$panel = New-Object System.Windows.Forms.FlowLayoutPanel
$panel.FlowDirection = 'TopDown'
$panel.Dock = 'Fill'
foreach ($c in @(@('Pictures', 'Checked'), @('Music', 'Unchecked'), @('Some', 'Indeterminate'))) {
    $box = New-Object System.Windows.Forms.CheckBox
    $box.Text, $box.ThreeState, $box.CheckState = $c[0], ($c[1] -eq 'Indeterminate'), $c[1]
    $panel.Controls.Add($box)
}
foreach ($r in @(@('Light', $true), @('Dark', $false))) {
    $radio = New-Object System.Windows.Forms.RadioButton
    $radio.Text, $radio.Checked = $r[0], $r[1]
    $panel.Controls.Add($radio)
}
$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save'
$panel.Controls.Add($save)
$form.Controls.Add($panel)
'window up'
[System.Windows.Forms.Application]::Run($form)
