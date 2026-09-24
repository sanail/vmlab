# vmlab-call-server: runs the ssh Channel's calls in the logged-in user's desktop session.
#
# An SSH session cannot reach the desktop. Instead of a Scheduled Task per call (two PowerShell
# starts, ~1 s), one interactive Scheduled Task starts this server once per boot; it listens on
# 127.0.0.1 only, and the Host reaches it with `ssh -W 127.0.0.1:PORT` over its multiplexed
# connection: no process starts in the Guest but the command itself (vmlab.providers.windows).
#
#   call-server.ps1 -Port N -Start   (over SSH) registers and runs the call server's Scheduled Task, then
#                              waits until the server listens. Exit 3: it did not.
#   call-server.ps1 -Port N          (the task) compiles the server once per version, then serves.
#
# One call per connection. The server first writes "vmlab-call-server VERSION\n", so the Host tells a
# call that never reached it (safe to repeat) from one that failed after. The Host then sends
# lines "file B64", "args B64", "timeout SECONDS", "env B64NAME B64VALUE" (any number),
# "stdin LENGTH", an empty line, then LENGTH bytes of stdin; the server answers with call.ps1's
# result, {"code", "stdout", "stderr"} (streams base64), and closes. What a command sees is
# what call.ps1 gives it (the vmrun Channel's runner): the user's profile folder, no window, no
# elevation, killed a second after its timeout with everything it started; and the user's
# environment as a new logon gets it, as a Scheduled Task per call had it.
#
# Plain ASCII on purpose: Windows PowerShell reads a script without a BOM in the system code page.
param([Parameter(Mandatory = $true)][int]$Port, [switch]$Start)
$ErrorActionPreference = 'Stop'
$version = (Split-Path -Leaf $PSCommandPath) -replace '^vmlab-call-server-|\.ps1$', ''

if ($Start) {
    $name = "vmlab-call-server-$Port"
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $task = $service.NewTask(0)
    # By SID: an SSH session's USERDOMAIN is not the name Task Scheduler takes.
    $task.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $task.Principal.LogonType = 3  # interactive: runs in the user's desktop session
    $task.Principal.RunLevel = 0  # not elevated, as over vmrun
    $task.Settings.DisallowStartIfOnBatteries = $false
    $task.Settings.StopIfGoingOnBatteries = $false
    $task.Settings.ExecutionTimeLimit = 'PT0S'
    $task.Settings.MultipleInstances = 2  # a second start while one runs is ignored
    $action = $task.Actions.Create(0)
    # A console with no window, which would take the focus; the server needs a console (see below).
    $action.Path = "$env:SystemRoot\System32\conhost.exe"
    $action.Arguments = "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
    $folder = $service.GetFolder('\')
    [void]$folder.RegisterTaskDefinition($name, $task, 6, $null, $null, 3)
    [void]$folder.GetTask($name).Run($null)
    $deadline = (Get-Date).AddSeconds(60)  # its first start compiles it
    $starting = (Get-Date).AddSeconds(10)  # by then it runs, or nobody is logged in to run it
    while ($true) {
        $client = New-Object Net.Sockets.TcpClient
        try { $client.Connect([Net.IPAddress]::Loopback, $Port); exit 0 } catch { } finally { $client.Close() }
        $state = $folder.GetTask($name).State  # 2 queued, 3 ready (over), 4 running
        if ((Get-Date) -gt $deadline -or ($state -ne 4 -and (Get-Date) -gt $starting)) {
            [Console]::Error.WriteLine("vmlab: the call server's Scheduled Task did not start it (task state $state, last result $($folder.GetTask($name).LastTaskResult)); is $env:USERNAME logged in to the desktop?")
            exit 3
        }
        Start-Sleep -Milliseconds 50
    }
}

$source = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace VmlabCallServer {

public static class Server {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    [DllImport("userenv.dll", SetLastError = true)]
    static extern bool CreateEnvironmentBlock(out IntPtr block, IntPtr token, bool inherit);
    [DllImport("userenv.dll", SetLastError = true)]
    static extern bool DestroyEnvironmentBlock(IntPtr block);

    static readonly object starting = new object();

    public static void Serve(int port, string version) {
        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        Private(listener.Server);
        byte[] greeting = Encoding.ASCII.GetBytes("vmlab-call-server " + version + "\n");
        while (true) {
            Socket client = listener.AcceptSocket();
            Private(client);
            ThreadPool.QueueUserWorkItem(delegate { Handle(client, greeting); });
        }
    }

    // .NET Framework sockets are inheritable: a program a call starts (an app it launches) would
    // hold the connection open, and the Host would wait for it to quit.
    static void Private(Socket socket) {
        SetHandleInformation(socket.Handle, 1, 0);
    }

    static void Handle(Socket client, byte[] greeting) {
        using (var stream = new NetworkStream(client, true)) {
            try {
                stream.Write(greeting, 0, greeting.Length);
                byte[] result;
                try {
                    result = Run(stream);
                } catch (Exception e) {
                    if (e is IOException) throw;
                    result = Encoding.UTF8.GetBytes("vmlab-call-server failed: " + e);  // the Host reports it as the Channel's failure
                }
                stream.Write(result, 0, result.Length);
                client.Shutdown(SocketShutdown.Both);
            } catch (IOException) {
                // the Host went away; the call's own timeout still ends what it started
            } catch (SocketException) {
            }
        }
    }

    static string Line(Stream stream) {
        var line = new List<byte>();
        while (true) {
            int b = stream.ReadByte();
            if (b < 0) throw new IOException("the Host closed the call early");
            if (b == '\n') return Encoding.ASCII.GetString(line.ToArray());
            line.Add((byte)b);
        }
    }

    static string Decode(string b64) {
        return Encoding.UTF8.GetString(Convert.FromBase64String(b64));
    }

    static byte[] Run(Stream stream) {
        string file = null, args = "";
        int timeout = 0;
        long stdin = 0;
        var env = new List<KeyValuePair<string, string>>();
        for (string line = Line(stream); line != ""; line = Line(stream)) {
            string[] words = line.Split(' ');
            switch (words[0]) {
                case "file": file = Decode(words[1]); break;
                case "args": args = Decode(words[1]); break;
                case "timeout": timeout = int.Parse(words[1]); break;
                case "env": env.Add(new KeyValuePair<string, string>(Decode(words[1]), Decode(words[2]))); break;
                case "stdin": stdin = long.Parse(words[1]); break;
            }
        }

        // Read whole before the command starts: a command that does not read it must still be
        // killed at its timeout, and bytes left unread make Windows reset the connection, which
        // could lose the answer.
        var input = new MemoryStream();
        var buffer = new byte[65536];
        for (long left = stdin; left > 0; ) {
            int n = stream.Read(buffer, 0, (int)Math.Min(buffer.Length, left));
            if (n <= 0) throw new IOException("the Host closed the call early");
            input.Write(buffer, 0, n);
            left -= n;
        }

        Dictionary<string, string> logon = LogonEnvironment();
        var info = new ProcessStartInfo(file, args);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;  // console programs get no window; GUI apps still show theirs
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.WorkingDirectory = Environment.GetEnvironmentVariable("USERPROFILE");
        info.EnvironmentVariables.Clear();
        foreach (var pair in logon) info.EnvironmentVariables[pair.Key] = pair.Value;
        foreach (var pair in env) info.EnvironmentVariables[pair.Key] = pair.Value;

        Process process;
        try {
            lock (starting) {
                // Windows finds the program on the server's own PATH: the logon's, as call.ps1's is.
                Environment.SetEnvironmentVariable("PATH", logon.ContainsKey("Path") ? logon["Path"] : null);
                process = Process.Start(info);
            }
        } catch (Win32Exception e) {
            // What cmd.exe answers for a program it cannot find.
            return Result(9009, new byte[0], Encoding.UTF8.GetBytes("vmlab: cannot run " + file + ": " + e.Message));
        }
        using (process) {
            var stdout = new Output(process.StandardOutput.BaseStream);
            var stderr = new Output(process.StandardError.BaseStream);
            byte[] data = input.ToArray();
            Stream pipe = process.StandardInput.BaseStream;
            Task.Run(delegate {
                try { pipe.Write(data, 0, data.Length); } catch (IOException) { }  // the command stopped reading
                try { pipe.Close(); } catch (IOException) { }
            });
            // Killed in the Guest too, a second after the Host gave up on it, so a call that timed out does not run on.
            if (!process.WaitForExit((timeout + 1) * 1000)) {
                using (var kill = Process.Start(new ProcessStartInfo("taskkill.exe", "/T /F /PID " + process.Id) { UseShellExecute = false, CreateNoWindow = true })) {
                    kill.WaitForExit();
                }
                process.WaitForExit();
            }
            // A program the command started (an app it launched) may hold the streams open: don't wait for it.
            try { Task.WaitAll(new Task[] { stdout.Done, stderr.Done }, 2000); } catch (AggregateException) { }
            return Result(process.ExitCode, stdout.Bytes(), stderr.Bytes());
        }
    }

    // A stream's bytes as they come, readable while more are still coming.
    class Output {
        readonly MemoryStream bytes = new MemoryStream();
        public readonly Task Done;

        public Output(Stream stream) {
            Done = Task.Run(delegate {
                var buffer = new byte[65536];
                for (int n; (n = stream.Read(buffer, 0, buffer.Length)) > 0; ) {
                    lock (bytes) bytes.Write(buffer, 0, n);
                }
            });
        }

        public byte[] Bytes() {
            lock (bytes) return bytes.ToArray();
        }
    }

    // The environment a new logon of this user gets, as the Scheduled Task behind every vmrun call
    // has: what an installer changed since the server started is there. Variables of the session
    // itself (SESSIONNAME, ...) come from the server.
    static Dictionary<string, string> LogonEnvironment() {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry pair in Environment.GetEnvironmentVariables()) result[(string)pair.Key] = (string)pair.Value;
        IntPtr block;
        bool made;
        using (var identity = WindowsIdentity.GetCurrent()) made = CreateEnvironmentBlock(out block, identity.Token, false);
        if (!made) return result;
        try {
            for (IntPtr at = block; ; ) {
                string entry = Marshal.PtrToStringUni(at);
                if (string.IsNullOrEmpty(entry)) break;
                at = new IntPtr(at.ToInt64() + (entry.Length + 1) * 2);
                int eq = entry.IndexOf('=', 1);
                if (eq > 0) result[entry.Substring(0, eq)] = entry.Substring(eq + 1);
            }
        } finally {
            DestroyEnvironmentBlock(block);
        }
        return result;
    }

    static byte[] Result(int code, byte[] stdout, byte[] stderr) {
        return Encoding.ASCII.GetBytes(string.Format("{{\"code\": {0}, \"stdout\": \"{1}\", \"stderr\": \"{2}\"}}",
            code, Convert.ToBase64String(stdout), Convert.ToBase64String(stderr)));
    }
}

}
'@

$dir = Join-Path $env:ProgramData 'vmlab\call-server'
$dll = Join-Path $dir "vmlab-call-server-$version.dll"
if (-not (Test-Path -LiteralPath $dll)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # Compiled aside and renamed: a concurrent start never loads half a file.
    $part = "$dll.$PID.part"
    Add-Type -TypeDefinition $source -OutputAssembly $part -OutputType Library
    try { Move-Item -LiteralPath $part -Destination $dll -ErrorAction Stop } catch { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $part }
    Get-ChildItem -LiteralPath $dir -Filter 'vmlab-call-server-*.dll' | Where-Object { $_.FullName -ne $dll } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
Add-Type -Path $dll
# .NET writes the console's input encoding in front of a command's stdin: with this Guest's
# UTF-8 code page, a BOM. Byte for byte means UTF-8 without one. Needs the console conhost gives.
[Console]::InputEncoding = New-Object Text.UTF8Encoding $false
[VmlabCallServer.Server]::Serve($Port, $version)
