# vmlab-spawn for Windows Guests: starts and stops g.spawn's background processes
# (vmlab.providers.spawning.WindowsSpawner).
#
# vmlab sends this script with every call, as it does vmlab-ui.ps1: stdin is one line of JSON
# parameters, then this script, run as a script block with the command name and that line.
#
#   start {"argv", "line", "cmd_line", "log", "job"}   prints the process's pid, its start time
#                                                      (FILETIME, UTC) and the log's full path
#   stop {"job", "grace_ms"}                           exit 0: stopped; 4: nothing was left to
#                                                      stop; 1: something still runs
#
# The process runs in a Job Object named job, and so does everything it starts: stop ends the
# whole job, children whose parent has exited too, which a walk of parent pids (taskkill /T)
# misses. A job's name lasts only while a handle to it is open, so a holder (this assembly run
# as a program, outside the job, with no window or console) keeps one until the job has no
# process left; stop then finds no job, and so nothing to stop.
#
# The process itself is started with CreateProcess, not through cmd.exe, so its pid is the
# program's; its stdout and stderr go to the log, and it inherits no other handle (the call's
# own output pipes would keep the call open until it ends). A program other than .exe and .com
# (a batch file) runs in cmd.exe, whose pid it is then.
#
# stop first asks: each visible top-level window of the job's processes gets WM_CLOSE (as
# taskkill without /F does), and if there was one, the job gets grace_ms to end; then
# TerminateJobObject. A console program has no signal to be asked by, so it is ended at once.
#
# Add-Type would compile the C# on every call; it is compiled once per Guest into
# C:\ProgramData\vmlab\spawn, named by a hash of its source (a second, on the first spawn after
# a restore). Plain ASCII on purpose: Windows PowerShell reads a script in the system code page.
param([string]$Command, [string]$Params)
$ErrorActionPreference = 'Stop'

$source = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Threading;

namespace VmlabSpawn {

public static class Job {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct STARTUPINFOEX {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct SECURITY_ATTRIBUTES {
        public int nLength;
        public IntPtr lpSecurityDescriptor;
        public bool bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION {
        public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime;
        public int TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(ref SECURITY_ATTRIBUTES attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool QueryInformationJobObject(IntPtr job, int infoClass, out JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info, int size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool QueryInformationJobObject(IntPtr job, int infoClass, IntPtr info, int size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFile(string name, uint access, uint share, ref SECURITY_ATTRIBUTES attributes, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returnSize);
    [DllImport("kernel32.dll")]
    static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string application, string commandLine, IntPtr processAttributes, IntPtr threadAttributes,
        bool inheritHandles, uint flags, IntPtr environment, string directory, ref STARTUPINFOEX startup, out PROCESS_INFORMATION info);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    delegate bool EnumProc(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")]
    static extern bool EnumWindows(EnumProc callback, IntPtr param);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hwnd, out int pid);
    [DllImport("user32.dll")]
    static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);

    const uint JOB_OBJECT_QUERY = 0x4, JOB_OBJECT_TERMINATE = 0x8;
    const int JobObjectBasicAccountingInformation = 1, JobObjectBasicProcessIdList = 3;
    const uint GENERIC_READ = 0x80000000, GENERIC_WRITE = 0x40000000, SHARE_ALL = 0x7, CREATE_ALWAYS = 2, OPEN_EXISTING = 3;
    const uint CREATE_SUSPENDED = 0x4, DETACHED_PROCESS = 0x8, CREATE_NO_WINDOW = 0x08000000, EXTENDED_STARTUPINFO_PRESENT = 0x80000;
    const int STARTF_USESTDHANDLES = 0x100;
    const uint WM_CLOSE = 0x10;
    static readonly IntPtr PROC_THREAD_ATTRIBUTE_HANDLE_LIST = (IntPtr)0x20002;
    static readonly IntPtr INVALID_HANDLE = (IntPtr)(-1);

    // The holder: keeps the job handle it inherited (its argument) open until the job has no process left.
    public static int Main(string[] args) {
        IntPtr job = (IntPtr)long.Parse(args[0]);
        while (Active(job) > 0) Thread.Sleep(250);
        return 0;
    }

    static Exception Failed(string what) {
        return new Exception(what + ": " + new Win32Exception(Marshal.GetLastWin32Error()).Message);
    }

    static SECURITY_ATTRIBUTES Inheritable() {
        return new SECURITY_ATTRIBUTES { nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)), bInheritHandle = true };
    }

    // Starts commandLine suspended, inheriting only the handles inherit (application: its program's
    // path, or null). The caller closes the handles in the result.
    static PROCESS_INFORMATION Create(string application, string commandLine, string directory, uint flags, IntPtr[] inherit, IntPtr input, IntPtr output) {
        IntPtr handles = Marshal.AllocHGlobal(inherit.Length * IntPtr.Size), list = IntPtr.Zero, size = IntPtr.Zero;
        try {
            Marshal.Copy(inherit, 0, handles, inherit.Length);
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            list = Marshal.AllocHGlobal(size);
            if (!InitializeProcThreadAttributeList(list, 1, 0, ref size)) throw Failed("InitializeProcThreadAttributeList");
            try {
                if (!UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, (IntPtr)(inherit.Length * IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                    throw Failed("UpdateProcThreadAttribute");
                var startup = new STARTUPINFOEX();
                startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
                startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
                startup.StartupInfo.hStdInput = input;
                startup.StartupInfo.hStdOutput = output;
                startup.StartupInfo.hStdError = output;
                startup.lpAttributeList = list;
                PROCESS_INFORMATION process;
                if (!CreateProcess(application, commandLine, IntPtr.Zero, IntPtr.Zero, true,
                        flags | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT, IntPtr.Zero, directory, ref startup, out process))
                    throw Failed("cannot run " + commandLine);
                return process;
            } finally {
                DeleteProcThreadAttributeList(list);
            }
        } finally {
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
            Marshal.FreeHGlobal(handles);
        }
    }

    // Starts commandLine in a new job named job, with its holder (holder: this assembly's path).
    // Returns its pid and start time.
    public static long[] Start(string application, string commandLine, string log, string job, string directory, string holder) {
        var inheritable = Inheritable();
        IntPtr output = CreateFile(log, GENERIC_WRITE, SHARE_ALL, ref inheritable, CREATE_ALWAYS, 0, IntPtr.Zero);
        if (output == INVALID_HANDLE) throw Failed("cannot create " + log);
        IntPtr input = CreateFile("NUL", GENERIC_READ, SHARE_ALL, ref inheritable, OPEN_EXISTING, 0, IntPtr.Zero);
        IntPtr jobHandle = CreateJobObject(ref inheritable, job);
        try {
            if (input == INVALID_HANDLE) throw Failed("cannot open NUL");
            if (jobHandle == IntPtr.Zero) throw Failed("CreateJobObject");
            PROCESS_INFORMATION process = Create(application, commandLine, directory, CREATE_NO_WINDOW, new[] { input, output }, input, output);
            try {
                if (!AssignProcessToJobObject(jobHandle, process.hProcess)) {
                    Exception e = Failed("AssignProcessToJobObject");
                    TerminateProcess(process.hProcess, 1);
                    throw e;
                }
                PROCESS_INFORMATION held = Create(holder, "\"" + holder + "\" " + jobHandle.ToInt64(), directory, DETACHED_PROCESS, new[] { jobHandle }, IntPtr.Zero, IntPtr.Zero);
                ResumeThread(held.hThread);
                CloseHandle(held.hThread);
                CloseHandle(held.hProcess);
                long created, exited, kernel, user;
                GetProcessTimes(process.hProcess, out created, out exited, out kernel, out user);
                if (ResumeThread(process.hThread) == 0xFFFFFFFF) {
                    Exception e = Failed("ResumeThread");
                    TerminateJobObject(jobHandle, 1);
                    throw e;
                }
                return new long[] { process.dwProcessId, created };
            } catch {
                TerminateProcess(process.hProcess, 1);
                throw;
            } finally {
                CloseHandle(process.hThread);
                CloseHandle(process.hProcess);
            }
        } finally {
            if (jobHandle != IntPtr.Zero) CloseHandle(jobHandle);
            if (input != INVALID_HANDLE) CloseHandle(input);
            CloseHandle(output);
        }
    }

    // 0: stopped; 4: the job has no process left (or is gone); 1: something still runs.
    public static int Stop(string job, int graceMs) {
        IntPtr handle = OpenJobObject(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, false, job);
        if (handle == IntPtr.Zero) return 4;
        try {
            if (Active(handle) == 0) return 4;
            if (CloseWindows(Pids(handle)) && Ended(handle, graceMs)) return 0;
            TerminateJobObject(handle, 1);
            return Ended(handle, 2000) ? 0 : 1;
        } finally {
            CloseHandle(handle);
        }
    }

    static int Active(IntPtr job) {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION info;
        if (!QueryInformationJobObject(job, JobObjectBasicAccountingInformation, out info, Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)), IntPtr.Zero))
            throw Failed("QueryInformationJobObject");
        return info.ActiveProcesses;
    }

    static bool Ended(IntPtr job, int ms) {
        for (DateTime end = DateTime.UtcNow.AddMilliseconds(ms); Active(job) > 0; Thread.Sleep(50)) {
            if (DateTime.UtcNow > end) return false;
        }
        return true;
    }

    static HashSet<int> Pids(IntPtr job) {
        // JOBOBJECT_BASIC_PROCESS_ID_LIST: two counts, then the ids. More than fit: none are asked.
        int size = 8 + 1024 * IntPtr.Size;
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            var pids = new HashSet<int>();
            if (!QueryInformationJobObject(job, JobObjectBasicProcessIdList, buffer, size, IntPtr.Zero)) return pids;
            int count = Marshal.ReadInt32(buffer, 4);
            for (int i = 0; i < count; i++) pids.Add((int)Marshal.ReadIntPtr(buffer, 8 + i * IntPtr.Size).ToInt64());
            return pids;
        } finally {
            Marshal.FreeHGlobal(buffer);
        }
    }

    // Posts WM_CLOSE to each visible top-level window of pids; did any have one?
    static bool CloseWindows(HashSet<int> pids) {
        bool any = false;
        EnumWindows(delegate(IntPtr hwnd, IntPtr param) {
            int pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (pids.Contains(pid) && IsWindowVisible(hwnd)) {
                PostMessage(hwnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
                any = true;
            }
            return true;
        }, IntPtr.Zero);
        return any;
    }
}
}
'@

$dir = Join-Path $env:ProgramData 'vmlab\spawn'
$sha = [Security.Cryptography.SHA1]::Create()
$hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))[0..5] | ForEach-Object { $_.ToString('x2') })
$exe = Join-Path $dir "vmlab-spawn-$hash.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # Compiled aside and renamed: a concurrent call never loads half a file. The name ends in
    # .exe: compiled to another, the program does not run (on ARM64, at least).
    $part = Join-Path $dir "vmlab-spawn-$hash.$PID.part.exe"
    Add-Type -TypeDefinition $source -OutputAssembly $part -OutputType ConsoleApplication
    try { Move-Item -LiteralPath $part -Destination $exe -ErrorAction Stop } catch { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $part }
    # Other vmlab versions' copies; one still holding a job stays until next time.
    Get-ChildItem -LiteralPath $dir -Filter 'vmlab-spawn-*.exe' | Where-Object { $_.FullName -ne $exe -and $_.Name -notlike '*.part.exe' } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
[void][Reflection.Assembly]::LoadFrom($exe)  # Add-Type -Path takes no .exe
$p = ConvertFrom-Json $Params

if ($Command -eq 'stop') {
    exit [VmlabSpawn.Job]::Stop($p.job, $p.grace_ms)
}
# start
$argv = @($p.argv)
# Escaped: -Name takes a wildcard pattern, and would run whatever matched first.
$app = Get-Command -CommandType Application -Name ([Management.Automation.WildcardPattern]::Escape($argv[0])) -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $app) { [Console]::Error.WriteLine('no such command: ' + $argv[0]); exit 127 }
$log = [Environment]::ExpandEnvironmentVariables($p.log)
if (@('.exe', '.com') -contains [IO.Path]::GetExtension($app.Path).ToLowerInvariant()) {
    $file = $app.Path
    $line = $p.line
} else {
    # A batch file (or a script Windows opens by its type): cmd.exe runs it.
    if ($p.cmd_line -match "[`r`n]") {
        [Console]::Error.WriteLine('spawn cannot pass a line break in an argument to ' + $app.Path + ' (cmd.exe ends the command there); put the text in a file with g.put')
        exit 2
    }
    $file = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $line = 'cmd.exe /d /s /c "' + $p.cmd_line + '"'
}
try {
    $started = [VmlabSpawn.Job]::Start($file, $line, $log, $p.job, $env:USERPROFILE, $exe)
} catch {
    $e = $_.Exception
    if ($e.InnerException) { $e = $e.InnerException }
    [Console]::Error.WriteLine($e.Message)
    exit 1
}
$started[0]
$started[1]
$log
