# A window that prints the pointer's buttons, for the contract tests, in a Windows Guest: WinForms.
#
#     vmlab-chk-XXXX.exe -NoProfile -STA -ExecutionPolicy Bypass -File pointer_fixture_windows.ps1 TITLE
#
# (vmlab-chk-XXXX.exe: a copy of powershell.exe, so the app has a name of its own.)
# Its window, titled TITLE, prints a line per button press, release and move with a button
# held: "down|move|up BUTTON MODIFIERS", BUTTON left, right, middle, back or forward,
# MODIFIERS like ctrl+shift, or - for none. Windows PowerShell and .NET only: the Guest installs nothing.
param([string]$Title)
Add-Type -AssemblyName System.Windows.Forms

$buttons = @{ Left = 'left'; Right = 'right'; Middle = 'middle'; XButton1 = 'back'; XButton2 = 'forward' }

function Line($what, $button) {
    $keys = [System.Windows.Forms.Control]::ModifierKeys
    $held = @()
    if ($keys -band [System.Windows.Forms.Keys]::Control) { $held += 'ctrl' }
    if ($keys -band [System.Windows.Forms.Keys]::Alt) { $held += 'alt' }
    if ($keys -band [System.Windows.Forms.Keys]::Shift) { $held += 'shift' }
    "$what $($buttons[[string]$button]) $(if ($held) { $held -join '+' } else { '-' })"
}

# PowerShell compiles a function on its first call, and Windows keeps only the last of the moves
# an app has not yet taken: compiled during the drag's press, Line cost the drag all but one move.
$null = Line 'down' 'Left'

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width, $form.Height = 600, 400
$form.add_MouseDown({ param($s, $e) [Console]::Out.WriteLine((Line 'down' $e.Button)) })
$form.add_MouseMove({ param($s, $e) if ($e.Button -ne 'None') { [Console]::Out.WriteLine((Line 'move' $e.Button)) } })
$form.add_MouseUp({ param($s, $e) [Console]::Out.WriteLine((Line 'up' $e.Button)) })
'window up'
[System.Windows.Forms.Application]::Run($form)
