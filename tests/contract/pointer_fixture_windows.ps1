# A window that prints the pointer's buttons, for the contract tests, in a Windows Guest: WinForms.
#
#     vmlab-chk-XXXX.exe -NoProfile -STA -ExecutionPolicy Bypass -File pointer_fixture_windows.ps1 TITLE
#
# (vmlab-chk-XXXX.exe: a copy of powershell.exe, so the app has a name of its own.)
# Its window, titled TITLE, prints a line per button press, release and move with a button
# held: "down|move|up BUTTON MODIFIERS", BUTTON left, right, middle, back or forward,
# MODIFIERS like ctrl+shift, or - for none. Windows PowerShell and .NET only: the Guest installs nothing.
#
# The handlers are C#, compiled before the window is up: Windows keeps only the last of the moves
# an app has not yet taken, and a PowerShell handler readied on its first call cost a drag its moves.
param([string]$Title)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Windows.Forms;

public class PointerPad : Form {
    static string ButtonName(MouseButtons button) {
        switch (button) {
            case MouseButtons.Left: return "left";
            case MouseButtons.Right: return "right";
            case MouseButtons.Middle: return "middle";
            case MouseButtons.XButton1: return "back";
            case MouseButtons.XButton2: return "forward";
        }
        return button.ToString();
    }

    static void Say(string what, MouseButtons button) {
        Keys keys = Control.ModifierKeys;
        string held = ((keys & Keys.Control) != 0 ? "+ctrl" : "") + ((keys & Keys.Alt) != 0 ? "+alt" : "") + ((keys & Keys.Shift) != 0 ? "+shift" : "");
        Console.Out.WriteLine(what + " " + ButtonName(button) + " " + (held.Length > 0 ? held.Substring(1) : "-"));
    }

    protected override void OnMouseDown(MouseEventArgs e) { Say("down", e.Button); base.OnMouseDown(e); }
    protected override void OnMouseMove(MouseEventArgs e) { if (e.Button != MouseButtons.None) Say("move", e.Button); base.OnMouseMove(e); }
    protected override void OnMouseUp(MouseEventArgs e) { Say("up", e.Button); base.OnMouseUp(e); }
}
'@

$form = New-Object PointerPad
$form.Text = $Title
$form.Width, $form.Height = 600, 400
'window up'
[System.Windows.Forms.Application]::Run($form)
