# One command for a vmlab Channel, run in the Guest user's desktop session.
#
# The Host prepends $Request, base64 of the call's JSON:
#   {"file", "args", "env", "timeout", "stdin", "result"}
# file and args are the command line (args already quoted by Windows rules);
# stdin and result are Guest paths, stdin null for no input. The result file
# gets {"code", "stdout", "stderr"} (the streams base64, their bytes as the
# command wrote them). It appears whole or not at all: written aside, then renamed.
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in
# the system code page.

$ErrorActionPreference = 'Stop'
$call = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Request)) | ConvertFrom-Json

function Write-Result([int]$code, [byte[]]$stdout, [byte[]]$stderr) {
    $json = @{ code = $code; stdout = [Convert]::ToBase64String($stdout); stderr = [Convert]::ToBase64String($stderr) } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($call.result + '.part', $json)
    Move-Item -Force -LiteralPath ($call.result + '.part') -Destination $call.result
}

# Calls whose Host went away leave their files behind.
Get-ChildItem -LiteralPath (Split-Path $call.result) -Filter 'vmlab-call-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-1) } | Remove-Item -Force -ErrorAction SilentlyContinue

# Whatever the Host sends as stdin must arrive byte for byte: .NET encodes the command's
# standard input with the console's encoding, and UTF-8 with a BOM (this Guest's code page)
# puts that BOM in front of the first byte the command reads. .NET Framework has no
# StandardInputEncoding, so the console's own encoding is what decides it.
# Only when there is input: setting it hangs in a Scheduled Task's console (the ssh Channel).
if ($call.stdin) { try { [Console]::InputEncoding = New-Object Text.UTF8Encoding $false } catch { } }
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = $call.file
$info.Arguments = $call.args
$info.UseShellExecute = $false
$info.CreateNoWindow = $true  # console programs get no window; GUI apps still show theirs
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$info.WorkingDirectory = $env:USERPROFILE
foreach ($pair in $call.env.PSObject.Properties) { $info.EnvironmentVariables[$pair.Name] = [string]$pair.Value }
try {
    $process = [Diagnostics.Process]::Start($info)
} catch {
    # What cmd.exe answers for a program it cannot find.
    Write-Result 9009 @() ([Text.Encoding]::UTF8.GetBytes("vmlab: cannot run $($call.file): $($_.Exception.InnerException.Message)"))
    exit 0
}
$stdout = New-Object IO.MemoryStream
$stderr = New-Object IO.MemoryStream
$copies = @($process.StandardOutput.BaseStream.CopyToAsync($stdout), $process.StandardError.BaseStream.CopyToAsync($stderr))
if ($call.stdin) {
    $in = [IO.File]::OpenRead($call.stdin)
    try { $in.CopyTo($process.StandardInput.BaseStream) } catch [IO.IOException] { }  # the command stopped reading
    $in.Close()
}
try { $process.StandardInput.Close() } catch [IO.IOException] { }
# Killed in the Guest too, a second after the Host gave up on it, so a call that timed out does not run on.
if (-not $process.WaitForExit([int](($call.timeout + 1) * 1000))) {
    & taskkill.exe /T /F /PID $process.Id 2>&1 | Out-Null
    $process.WaitForExit()
}
# A program the command started (an app it launched) may hold the streams open: don't wait for it.
[void][Threading.Tasks.Task]::WaitAll($copies, 2000)
Write-Result $process.ExitCode $stdout.ToArray() $stderr.ToArray()
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath (@($PSCommandPath) + @($call.stdin | Where-Object { $_ }))
