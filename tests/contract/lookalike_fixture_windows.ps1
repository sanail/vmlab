# Two lookalike windows for the contract tests, in a Windows Guest: one app, one title, one button Add each.
#
#     vmlab-chk-XXXX.exe -NoProfile -STA -ExecutionPolicy Bypass -File lookalike_fixture_windows.ps1 TITLE
#
# (vmlab-chk-XXXX.exe: a copy of powershell.exe, so the app has a name of its own.)
# The second window opens over the first, offset by less than half its size: each has its Add at
# its bottom right, so the second window, not its Add, covers the first's Add. A click renames
# it Added 1 or Added 2.
# Windows PowerShell and .NET only: the Guest installs nothing.
param([string]$Title)
Add-Type -AssemblyName System.Windows.Forms

function Twin([int]$n) {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title
    $form.Width, $form.Height = 320, 200
    $form.StartPosition = 'Manual'
    $form.Location = New-Object System.Drawing.Point((200 + 40 * $n), (200 + 40 * $n))
    $add = New-Object System.Windows.Forms.Button
    $add.Text, $add.Tag = 'Add', $n
    $add.Location = New-Object System.Drawing.Point(200, 120)
    $add.Anchor = 'Bottom, Right'
    $add.Add_Click({ param($sender) $sender.Text = 'Added ' + $sender.Tag })
    $form.Controls.Add($add)
    $form
}
$script:first, $script:second = (Twin 1), (Twin 2)
$first.Add_Shown({ $script:second.Show() })
'windows up'
[System.Windows.Forms.Application]::Run($first)
