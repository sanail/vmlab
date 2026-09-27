# vmlab-ui for Windows Guests: the Windows side of vmlab's UI contract.
#
# vmlab sends this script with every call, so it is never out of step with the
# Host: stdin is one line of JSON parameters, then this script, which the call's
# command line runs as a script block with the command name and that line
# (vmlab.uihelpers.WindowsHelper). It prints one JSON object; on failure it
# exits 1 with a message on stderr. Commands and parameters are those of the
# macOS helper (guest/macos/vmlab-ui.swift); the Host maps native roles to
# vmlab's cross-OS roles and does all matching (vmlab.ui).
#
# The work is done in C# below: UI Automation for the tree, SendInput for the
# pointer and keys, user32 for windows and focus. Add-Type would compile it on
# every call (seconds), so it is compiled once per Guest into
# C:\ProgramData\vmlab\ui, named by a hash of its source.
#
# Traps (README: "Windows Guests with VMware Fusion"):
# - A process that is not in the foreground may not take it: SetForegroundWindow
#   quietly returns false. The helper joins the foreground window's input queue
#   (AttachThreadInput) first, where the switch is allowed. No Alt tap: in an
#   app with a menu bar it enters the menu, and the next chord goes there.
# - The helper is per-monitor DPI aware, so the tree, clicks and screenshots
#   all use physical pixels. Unaware, Windows scales some answers and not others.
# - Chords are sent as virtual keys, which keep their meaning on any keyboard
#   layout (Ctrl+C copies under a Cyrillic layout too); punctuation keys are the
#   US layout's. Text is sent as Unicode characters, which need no layout at all.
# - Plain ASCII on purpose: Windows PowerShell reads a script in the system code page.
param([string]$Command, [string]$Params)
$ErrorActionPreference = 'Stop'

$source = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using Accessibility;
using Microsoft.Win32;

namespace VmlabUi {

public class Fail : Exception {
    public Fail(string message) : base(message) { }
}

public class Win {
    public IntPtr Handle;
    public int Pid;
    public string Title;
}

/// A Staged document open in an editor: a tab of its window, or the window itself.
public class Staged {
    public Win Window;
    public AutomationElement Tab;  // null: an editor without tabs
}

static class Native {
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public INPUTUNION u; }
    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    public delegate bool EnumProc(IntPtr hwnd, IntPtr param);

    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint code, uint mapType);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out int pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint from, uint to, bool attach);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, IntPtr param);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hwnd);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);

    [StructLayout(LayoutKind.Sequential)]
    public struct NOTIFYICONIDENTIFIER { public int cbSize; public IntPtr hWnd; public uint uID; public Guid guidItem; }
    [DllImport("shell32.dll")] public static extern int Shell_NotifyIconGetRect(ref NOTIFYICONIDENTIFIER id, out RECT rect);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object accessible);
    [DllImport("oleacc.dll")] public static extern int AccessibleChildren(IAccessible parent, int start, int count, [Out] object[] children, out int got);
    // winsqlite3.dll: the SQLite that ships with Windows, for the notification database
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_open_v2(byte[] file, out IntPtr db, int flags, IntPtr vfs);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_busy_timeout(IntPtr db, int ms);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int length, out IntPtr statement, IntPtr tail);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_step(IntPtr statement);
    [DllImport("winsqlite3.dll")] public static extern IntPtr sqlite3_column_blob(IntPtr statement, int column);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_column_bytes(IntPtr statement, int column);
    [DllImport("winsqlite3.dll")] public static extern long sqlite3_column_int64(IntPtr statement, int column);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_finalize(IntPtr statement);
    [DllImport("winsqlite3.dll")] public static extern int sqlite3_close(IntPtr db);
    [DllImport("winsqlite3.dll")] public static extern IntPtr sqlite3_errmsg(IntPtr db);
}

public static class Helper {
    const int VERSION = 1;
    const int MAX_NODES = 5000;
    const int MAX_TEXT = 100000;
    const int DEFAULT_DEPTH = 60;
    const double POLL = 0.1;
    // Between typed characters. Faster, Notepad (WinUI) lost characters now and then: 5 ms
    // dropped some in one call of three, 15 and 30 ms none in eleven.
    const int TYPE_PAUSE_MS = 20;
    const string STAGE = "vmlab-stage-";  // + 8 hex digits: the name of every file stage-text opens
    static Regex STAGED = new Regex("^" + STAGE + "[0-9a-fA-F]{8}\\.txt$", RegexOptions.IgnoreCase);
    const double CLOSE_WAIT = 5;  // s for one staged document to close
    const double SELECT_WAIT = 2;  // s for a select-all to select a Staged document's text, before it is pressed again

    static int nodes;
    static bool truncated;
    static Dictionary<int, string> names = new Dictionary<int, string>();
    static JavaScriptSerializer json = new JavaScriptSerializer();

    // Key names (vmlab.ui) to virtual keys; extended keys set KEYEVENTF_EXTENDEDKEY.
    static Dictionary<string, ushort> VK = new Dictionary<string, ushort>();
    static HashSet<string> EXTENDED = new HashSet<string>(new string[] {
        "delete", "up", "down", "left", "right", "home", "end", "pageup", "pagedown", "cmd" });

    static Helper() {
        json.MaxJsonLength = int.MaxValue;
        for (char c = 'a'; c <= 'z'; c++) VK[c.ToString()] = (ushort)char.ToUpper(c);
        for (char c = '0'; c <= '9'; c++) VK[c.ToString()] = (ushort)c;
        for (int n = 1; n <= 12; n++) VK["f" + n] = (ushort)(0x6F + n);
        string[] keyNames = { "space", "enter", "tab", "escape", "backspace", "delete", "up", "down", "left", "right",
            "home", "end", "pageup", "pagedown", "minus", "equal", "comma", "period", "slash", "semicolon", "quote",
            "backslash", "grave", "leftbracket", "rightbracket", "ctrl", "alt", "shift", "cmd" };
        ushort[] codes = { 0x20, 0x0D, 0x09, 0x1B, 0x08, 0x2E, 0x26, 0x28, 0x25, 0x27,
            0x24, 0x23, 0x21, 0x22, 0xBD, 0xBB, 0xBC, 0xBE, 0xBF, 0xBA, 0xDE,
            0xDC, 0xC0, 0xDB, 0xDD, 0x11, 0x12, 0x10, 0x5B };
        for (int i = 0; i < keyNames.Length; i++) VK[keyNames[i]] = codes[i];
    }

    public static int Run(string command, string parameters) {
        try {
            DpiAware();
            Dictionary<string, object> p = string.IsNullOrEmpty(parameters)
                ? new Dictionary<string, object>()
                : json.DeserializeObject(parameters) as Dictionary<string, object>;
            if (p == null) throw new Fail("parameters are not a JSON object: " + parameters);
            Emit(Dispatch(command, p));
            return 0;
        } catch (Fail f) {
            Write(Console.OpenStandardError(), "vmlab-ui: " + f.Message + "\n");
        } catch (Exception e) {
            Write(Console.OpenStandardError(), "vmlab-ui: " + e.GetType().Name + ": " + e.Message + "\n");
        }
        return 1;
    }

    static readonly string[] inputCommands = { "click", "press", "type", "focus", "stage-text", "close-staged", "tray" };

    static object Dispatch(string command, Dictionary<string, object> p) {
        if (Array.IndexOf(inputCommands, command) >= 0) MoveBannersAside();
        switch (command) {
            case "version": return Version();
            case "tree": return Tree(p);
            case "click": return Click(p);
            case "press": {
                string key = Str(p, "key");
                List<string> modifiers = Strings(p, "modifiers");
                Press(key, modifiers);
                return Obj("key", key, "modifiers", modifiers);
            }
            case "type": {
                string text = Str(p, "text");
                if (text == null) throw new Fail("type needs text");
                return Obj("typed", Type(text));
            }
            case "clipboard":
                if (p.ContainsKey("set")) SetClipboard(Str(p, "set") ?? "");
                return Obj("text", Clipboard());
            case "focus": return Focus(p);
            case "stage-text": return StageText(p);
            case "close-staged": return CloseStaged(p);
            case "tray": return Tray(p);
            case "notifications": return Notifications();
        }
        throw new Fail("unknown command " + command);
    }

    // MARK: - Plumbing

    static void Write(Stream stream, string text) {
        byte[] bytes = new UTF8Encoding(false).GetBytes(text);
        stream.Write(bytes, 0, bytes.Length);
        stream.Flush();
    }

    static void Emit(object value) {
        Write(Console.OpenStandardOutput(), json.Serialize(value) + "\n");
    }

    static Dictionary<string, object> Obj(params object[] pairs) {
        Dictionary<string, object> d = new Dictionary<string, object>();
        for (int i = 0; i < pairs.Length; i += 2) d[(string)pairs[i]] = pairs[i + 1];
        return d;
    }

    static string Str(Dictionary<string, object> p, string key) {
        object v;
        return p.TryGetValue(key, out v) && v != null ? Convert.ToString(v) : null;
    }

    static double Num(Dictionary<string, object> p, string key, double fallback) {
        object v;
        return p.TryGetValue(key, out v) && v != null ? Convert.ToDouble(v) : fallback;
    }

    static bool Flag(Dictionary<string, object> p, string key) {
        object v;
        return p.TryGetValue(key, out v) && v is bool && (bool)v;
    }

    static List<string> Strings(Dictionary<string, object> p, string key) {
        List<string> list = new List<string>();
        object v;
        if (p.TryGetValue(key, out v) && v is IEnumerable && !(v is string))
            foreach (object item in (IEnumerable)v) list.Add(Convert.ToString(item));
        return list;
    }

    static void DpiAware() {
        // -4: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703 and later)
        try { if (Native.SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch (EntryPointNotFoundException) { }
        Native.SetProcessDPIAware();
    }

    static T WaitFor<T>(DateTime deadline, Func<T> what) where T : class {
        while (true) {
            T value = what();
            if (value != null) return value;
            if (DateTime.UtcNow >= deadline) return null;
            Thread.Sleep(TimeSpan.FromSeconds(POLL));
        }
    }

    static DateTime Deadline(Dictionary<string, object> p) {
        return DateTime.UtcNow.AddSeconds(Num(p, "timeout", 30));
    }

    static Dictionary<string, object> Version() {
        return Obj("helper", "windows", "version", VERSION, "trusted", true,
            "screen", Screen(), "dpi", (int)Math.Round(96.0 * Scale()));
    }

    static Dictionary<string, object> Screen() {
        // SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        return Obj("x", Native.GetSystemMetrics(76), "y", Native.GetSystemMetrics(77),
            "w", Native.GetSystemMetrics(78), "h", Native.GetSystemMetrics(79));
    }

    static double Scale() {
        using (System.Drawing.Graphics g = System.Drawing.Graphics.FromHwnd(IntPtr.Zero)) return g.DpiX / 96.0;
    }

    // MARK: - Windows and apps

    /// The app name of a process: its process name, as Get-Process shows it (e.g. "Notepad", "explorer").
    static string AppName(int pid) {
        string name;
        if (names.TryGetValue(pid, out name)) return name;
        try { name = Process.GetProcessById(pid).ProcessName; } catch (ArgumentException) { name = ""; }
        names[pid] = name;
        return name;
    }

    static bool IsApp(int pid, string wanted) {
        return string.Equals(AppName(pid), wanted, StringComparison.OrdinalIgnoreCase);
    }

    static string Title(IntPtr hwnd) {
        StringBuilder text = new StringBuilder(Native.GetWindowTextLength(hwnd) + 1);
        Native.GetWindowText(hwnd, text, text.Capacity);
        return text.ToString();
    }

    /// Top-level windows a user can see and switch to, frontmost first.
    static List<Win> Windows() {
        List<Win> found = new List<Win>();
        Native.EnumWindows(delegate(IntPtr hwnd, IntPtr param) {
            if (!Native.IsWindowVisible(hwnd) || Native.GetWindow(hwnd, 4) != IntPtr.Zero) return true;  // GW_OWNER
            if ((Native.GetWindowLong(hwnd, -20) & 0x80) != 0) return true;  // GWL_EXSTYLE: WS_EX_TOOLWINDOW
            int cloaked;
            if (Native.DwmGetWindowAttribute(hwnd, 14, out cloaked, 4) == 0 && cloaked != 0) return true;  // DWMWA_CLOAKED
            string title = Title(hwnd);
            if (title.Length == 0) return true;
            int pid;
            Native.GetWindowThreadProcessId(hwnd, out pid);
            Win w = new Win();
            w.Handle = hwnd; w.Pid = pid; w.Title = title;
            found.Add(w);
            return true;
        }, IntPtr.Zero);
        return found;
    }

    static string Frontmost() {
        IntPtr hwnd = Native.GetForegroundWindow();
        return hwnd == IntPtr.Zero ? "" : AppName(WindowPid(hwnd));
    }

    /// A Notification's banner takes the foreground when the window in front of it closes, and in a Guest
    /// nobody touches it stays up for good: keys go to it, clicks under it land on it, and no window can
    /// take the foreground from it (SetForegroundWindow refuses, whatever the helper does first). Before any
    /// input, the helper presses each banner's own "Move this notification to Notification Center" button,
    /// which keeps the Notification recorded. Only a banner's: Notification Center, the same process and
    /// window class, has buttons with the same id that clear Notifications.
    static void MoveBannersAside() {
        PropertyCondition banner = new PropertyCondition(AutomationElement.AutomationIdProperty, "NormalToastView");
        PropertyCondition dismissId = new PropertyCondition(AutomationElement.AutomationIdProperty, "DismissButton");
        for (int round = 0; round < 5; round++) {
            AutomationElement dismiss = null;
            try {
                foreach (IntPtr hwnd in ShellWindows()) {
                    AutomationElement window = AutomationElement.FromHandle(hwnd);
                    AutomationElement toast = window.FindFirst(TreeScope.Descendants, banner);
                    dismiss = toast == null ? null : toast.FindFirst(TreeScope.Descendants, dismissId);
                    if (dismiss != null) break;
                }
                if (dismiss == null) return;
                ((InvokePattern)dismiss.GetCurrentPattern(InvokePattern.Pattern)).Invoke();
            } catch (ElementNotAvailableException) {
                return;
            } catch (InvalidOperationException) {
                return;
            }
            WaitFor<string>(DateTime.UtcNow.AddSeconds(2), delegate() {
                try { return dismiss.Current.IsOffscreen ? "gone" : null; } catch (ElementNotAvailableException) { return "gone"; }
            });
        }
    }

    /// The shell's windows on screen: banners, Notification Center, flyouts. Not through EnumWindows,
    /// which leaves out windows in the shell's own z-order bands (a banner not in front among them).
    static List<IntPtr> ShellWindows() {
        List<IntPtr> found = new List<IntPtr>();
        IntPtr hwnd = IntPtr.Zero;
        while ((hwnd = Native.FindWindowEx(IntPtr.Zero, hwnd, "Windows.UI.Core.CoreWindow", null)) != IntPtr.Zero) {
            int cloaked;
            if (!Native.IsWindowVisible(hwnd) || !IsApp(WindowPid(hwnd), "ShellExperienceHost")) continue;
            if (Native.DwmGetWindowAttribute(hwnd, 14, out cloaked, 4) == 0 && cloaked != 0) continue;  // DWMWA_CLOAKED
            found.Add(hwnd);
        }
        return found;
    }

    static int WindowPid(IntPtr hwnd) {
        int pid;
        Native.GetWindowThreadProcessId(hwnd, out pid);
        return pid;
    }

    /// Make hwnd the foreground window, or fail. Only the foreground's own input queue may hand it over,
    /// so the helper joins that queue for the switch.
    static void BringToFront(IntPtr hwnd, string app, DateTime deadline) {
        string done = WaitFor<string>(deadline, delegate() {
            IntPtr front = Native.GetForegroundWindow();
            if (front == hwnd) return "done";
            if (Native.IsIconic(hwnd)) Native.ShowWindow(hwnd, 9);  // SW_RESTORE, only when minimized: it would un-maximize
            int pid;
            uint thread = front == IntPtr.Zero ? 0 : Native.GetWindowThreadProcessId(front, out pid);
            uint mine = Native.GetCurrentThreadId();
            bool attached = thread != 0 && thread != mine && Native.AttachThreadInput(mine, thread, true);
            Native.BringWindowToTop(hwnd);
            Native.SetForegroundWindow(hwnd);
            if (attached) Native.AttachThreadInput(mine, thread, false);
            return null;
        });
        if (done != null) return;
        string frontmost = Frontmost();
        throw new Fail(app + " did not come to the front in time; frontmost is " + (frontmost.Length > 0 ? frontmost : "nothing"));
    }

    static Dictionary<string, object> Focus(Dictionary<string, object> p) {
        string wanted = Str(p, "app");
        if (string.IsNullOrEmpty(wanted)) throw new Fail("focus needs an app");
        DateTime deadline = Deadline(p);
        List<Win> windows = Windows().FindAll(delegate(Win w) { return IsApp(w.Pid, wanted); });
        if (windows.Count == 0) {
            bool running = Process.GetProcessesByName(wanted).Length > 0;
            throw new Fail(running ? wanted + " has no windows" : wanted + " is not running");
        }
        Win target = windows[0];
        string raised = null;
        string title = Str(p, "window");
        if (title != null) {
            target = windows.Find(delegate(Win w) { return w.Title.Contains(title); });
            if (target == null) {
                List<string> titles = windows.ConvertAll(delegate(Win w) { return w.Title; });
                throw new Fail(wanted + " has no window titled like \"" + title + "\"; its windows: " + json.Serialize(titles));
            }
            raised = target.Title;
        }
        BringToFront(target.Handle, AppName(target.Pid), deadline);
        return Obj("app", AppName(target.Pid), "window", raised, "frontmost", Frontmost());
    }

    // MARK: - Reading the tree

    static CacheRequest Request() {
        CacheRequest request = new CacheRequest();
        request.TreeScope = TreeScope.Subtree;
        request.TreeFilter = Automation.ControlViewCondition;
        AutomationProperty[] properties = {
            AutomationElement.NameProperty, AutomationElement.ControlTypeProperty, AutomationElement.BoundingRectangleProperty,
            AutomationElement.HasKeyboardFocusProperty, AutomationElement.IsEnabledProperty, AutomationElement.HelpTextProperty,
            AutomationElement.NativeWindowHandleProperty,
            ValuePattern.ValueProperty, ValuePattern.IsReadOnlyProperty, RangeValuePattern.ValueProperty, WindowPattern.IsModalProperty,
        };
        foreach (AutomationProperty property in properties) request.Add(property);
        request.Add(ValuePattern.Pattern);
        request.Add(RangeValuePattern.Pattern);
        request.Add(TextPattern.Pattern);
        request.Add(WindowPattern.Pattern);
        return request;
    }

    static Dictionary<string, object> Bounds(System.Windows.Rect r) {
        if (r.IsEmpty || double.IsInfinity(r.Width) || r.Width <= 0 || r.Height <= 0) return null;
        if (r.X <= -30000 || r.Y <= -30000) return null;  // a minimized window's parking place
        return Obj("x", (int)Math.Round(r.X), "y", (int)Math.Round(r.Y), "w", (int)Math.Round(r.Width), "h", (int)Math.Round(r.Height));
    }

    static T Cached<T>(AutomationElement e, AutomationProperty property, T fallback) {
        try {
            object v = e.GetCachedPropertyValue(property, true);
            return v is T ? (T)v : fallback;
        } catch (Exception) {
            return fallback;
        }
    }

    /// Text as the other OSes give it: \n line breaks.
    static string Lines(string text) {
        return text == null ? null : text.Replace("\r\n", "\n").Replace('\r', '\n');
    }

    static string RangeText(TextPatternRange range) {
        return Lines(range.GetText(MAX_TEXT));
    }

    static string Text(AutomationElement e) {
        try {
            object pattern;
            if (!e.TryGetCachedPattern(TextPattern.Pattern, out pattern)) return null;
            return RangeText(((TextPattern)pattern).DocumentRange);
        } catch (Exception) {
            return null;
        }
    }

    static Dictionary<string, object> Node(AutomationElement e, int depth, int maxDepth) {
        nodes++;
        ControlType type = Cached<ControlType>(e, AutomationElement.ControlTypeProperty, ControlType.Custom);
        string native = type.ProgrammaticName.Replace("ControlType.", "");
        string name = Cached<string>(e, AutomationElement.NameProperty, "");
        string help = Cached<string>(e, AutomationElement.HelpTextProperty, "");
        Dictionary<string, object> node = Obj(
            "native_role", native, "name", name, "value", null, "description", help.Length > 0 ? help : null,
            "bounds", Bounds(Cached<System.Windows.Rect>(e, AutomationElement.BoundingRectangleProperty, System.Windows.Rect.Empty)),
            "focused", Cached<bool>(e, AutomationElement.HasKeyboardFocusProperty, false),
            "enabled", Cached<bool>(e, AutomationElement.IsEnabledProperty, true));
        object pattern;
        bool editable = false;
        if (e.TryGetCachedPattern(ValuePattern.Pattern, out pattern)) {
            node["value"] = Lines(Cached<string>(e, ValuePattern.ValueProperty, null));
            editable = !Cached<bool>(e, ValuePattern.IsReadOnlyProperty, true);
        } else if (e.TryGetCachedPattern(RangeValuePattern.Pattern, out pattern)) {
            double value = Cached<double>(e, RangeValuePattern.ValueProperty, double.NaN);
            if (!double.IsNaN(value)) node["value"] = value == Math.Floor(value) ? ((long)value).ToString() : value.ToString("G6", System.Globalization.CultureInfo.InvariantCulture);
        }
        if (type == ControlType.Edit || type == ControlType.Document) {
            // Rich edits and documents give their text through TextPattern; Value is empty or cut short.
            string text = Text(e);
            if (text != null && (node["value"] == null || text.Length > ((string)node["value"]).Length)) node["value"] = text;
            if (type == ControlType.Document && editable) node["role"] = "textarea";
            if (type == ControlType.Edit && Multiline(e)) node["role"] = "textarea";
        }
        if (type == ControlType.Window && Cached<bool>(e, WindowPattern.IsModalProperty, false)) node["role"] = "dialog";
        List<object> children = new List<object>();
        node["children"] = children;
        AutomationElementCollection kids;
        try { kids = e.CachedChildren; } catch (Exception) { return node; }
        if (kids.Count > 0 && depth >= maxDepth) { truncated = true; return node; }
        foreach (AutomationElement child in kids) {
            if (nodes >= MAX_NODES) { truncated = true; break; }
            children.Add(Node(child, depth + 1, maxDepth));
        }
        return node;
    }

    static bool Multiline(AutomationElement e) {
        int hwnd = Cached<int>(e, AutomationElement.NativeWindowHandleProperty, 0);
        return hwnd != 0 && (Native.GetWindowLong(new IntPtr(hwnd), -16) & 0x4) != 0;  // GWL_STYLE: ES_MULTILINE
    }

    static Dictionary<string, object> Tree(Dictionary<string, object> p) {
        string wanted = Str(p, "app");
        int maxDepth = (int)Num(p, "depth", DEFAULT_DEPTH);
        IntPtr front = Native.GetForegroundWindow();
        int frontPid = 0;
        if (front != IntPtr.Zero) Native.GetWindowThreadProcessId(front, out frontPid);
        CacheRequest top = new CacheRequest();
        top.Add(AutomationElement.ProcessIdProperty);
        top.Add(AutomationElement.IsOffscreenProperty);
        AutomationElementCollection windows;
        using (top.Activate()) windows = AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition);
        CacheRequest request = Request();
        List<object> apps = new List<object>();
        Dictionary<int, Dictionary<string, object>> byPid = new Dictionary<int, Dictionary<string, object>>();
        foreach (AutomationElement window in windows) {
            int pid = Cached<int>(window, AutomationElement.ProcessIdProperty, 0);
            if (Cached<bool>(window, AutomationElement.IsOffscreenProperty, false)) continue;  // minimized or cloaked
            if (wanted != null && !IsApp(pid, wanted)) continue;
            if (nodes >= MAX_NODES) { truncated = true; break; }
            AutomationElement full;
            try { full = window.GetUpdatedCache(request); } catch (ElementNotAvailableException) { continue; }
            Dictionary<string, object> app;
            if (!byPid.TryGetValue(pid, out app)) {
                app = Obj("native_role", "application", "name", AppName(pid), "value", null, "description", null, "bounds", null,
                    "focused", pid == frontPid, "enabled", true, "pid", pid, "children", new List<object>());
                byPid[pid] = app;
                apps.Add(app);
            }
            ((List<object>)app["children"]).Add(Node(full, 1, maxDepth));
        }
        return Obj("native_role", "desktop", "name", "", "value", null, "description", null, "bounds", Screen(),
            "focused", false, "enabled", true, "truncated", truncated, "children", apps);
    }

    // MARK: - Acting

    static string Label(AutomationElement e) {
        try {
            AutomationElement.AutomationElementInformation c = e.Current;
            if (!string.IsNullOrEmpty(c.Name) && c.Name.Trim().Length > 0) return c.Name.Trim();
            object pattern;
            if (e.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) {
                string value = Lines(((ValuePattern)pattern).Current.Value);
                if (!string.IsNullOrEmpty(value) && value.Trim().Length > 0) return value.Trim();
            }
            if (e.TryGetCurrentPattern(TextPattern.Pattern, out pattern)) {
                string text = RangeText(((TextPattern)pattern).DocumentRange);
                if (text.Trim().Length > 0) return text.Trim();
            }
            return (c.HelpText ?? "").Trim();
        } catch (Exception) {
            return "";
        }
    }

    // FromPoint stops at a host that does not hit-test its content: the Windows 11
    // taskbar answers with Shell_TrayWnd, never the button under the point. So the
    // smallest element under what it answers whose bounds hold the point stands for
    // it; walking down child by child would not do, since the taskbar's XAML host
    // reports its own bounds unscaled (half size at 200 %). A window over the point
    // is still what FromPoint answers, so what it covers stays out of reach.
    static AutomationElement Deepest(AutomationElement e, System.Windows.Point at) {
        if (e == null) return null;
        return SmallestAt(e, at) ?? e;
    }

    // The smallest on-screen descendant of e whose bounds hold the point, or null.
    static AutomationElement SmallestAt(AutomationElement e, System.Windows.Point at) {
        CacheRequest request = new CacheRequest();
        request.Add(AutomationElement.BoundingRectangleProperty);
        request.Add(AutomationElement.IsOffscreenProperty);
        AutomationElementCollection all;
        try {
            using (request.Activate()) all = e.FindAll(TreeScope.Descendants, Automation.ControlViewCondition);
        } catch (ElementNotAvailableException) { return null; }
        AutomationElement best = null;
        double area = double.MaxValue;
        foreach (AutomationElement d in all) {
            if (Cached<bool>(d, AutomationElement.IsOffscreenProperty, true)) continue;
            System.Windows.Rect r = Cached<System.Windows.Rect>(d, AutomationElement.BoundingRectangleProperty, System.Windows.Rect.Empty);
            if (r.IsEmpty || !r.Contains(at) || r.Width * r.Height > area) continue;
            best = d;  // on a tie the later one, drawn on top
            area = r.Width * r.Height;
        }
        return best;
    }

    static Dictionary<string, object> Click(Dictionary<string, object> p) {
        if (!p.ContainsKey("x") || !p.ContainsKey("y")) throw new Fail("click needs x and y");
        int x = (int)Num(p, "x", 0), y = (int)Num(p, "y", 0);
        Dictionary<string, object> screen = Screen();
        int sx = (int)screen["x"], sy = (int)screen["y"], sw = (int)screen["w"], sh = (int)screen["h"];
        if (x < sx || y < sy || x >= sx + sw || y >= sy + sh)
            throw new Fail("(" + x + ", " + y + ") is off the screen (" + sw + "x" + sh + ")");
        // The chain from the deepest element at the point up to its window, as (label, bounds).
        List<KeyValuePair<string, Dictionary<string, object>>> chain = new List<KeyValuePair<string, Dictionary<string, object>>>();
        try {
            System.Windows.Point at = new System.Windows.Point(x, y);
            AutomationElement e = Deepest(AutomationElement.FromPoint(at), at);
            TreeWalker walker = TreeWalker.ControlViewWalker;
            while (e != null && chain.Count < 12 && !Automation.Compare(e, AutomationElement.RootElement)) {
                chain.Add(new KeyValuePair<string, Dictionary<string, object>>(Label(e), Bounds(e.Current.BoundingRectangle)));
                e = walker.GetParent(e);
            }
        } catch (ElementNotAvailableException) { }
        object expect;
        if (p.TryGetValue("expect", out expect) && expect is Dictionary<string, object>) {
            // Refuse rather than click blind: an element scrolled out of view, or covered, still has bounds.
            Dictionary<string, object> want = (Dictionary<string, object>)expect;
            string label = Str(want, "label") ?? "";
            object wb;
            want.TryGetValue("bounds", out wb);
            bool hit = chain.Count > 0 && (
                (label.Length > 0 && chain[0].Key == label)
                || (wb is Dictionary<string, object> && chain.Exists(delegate(KeyValuePair<string, Dictionary<string, object>> link) {
                    return SameBounds(link.Value, (Dictionary<string, object>)wb); }))
                || (label.Length == 0 && wb == null));
            if (!hit) {
                string what = chain.Count > 0 ? "\"" + chain[0].Key + "\"" : "nothing";
                throw new Fail("something else is at (" + x + ", " + y + "): " + what + "; the element may be covered or scrolled out of view");
            }
        }
        Native.SetCursorPos(x, y);
        Thread.Sleep(50);
        Mouse(0x0002);  // MOUSEEVENTF_LEFTDOWN
        Thread.Sleep(30);
        Mouse(0x0004);  // MOUSEEVENTF_LEFTUP
        Thread.Sleep(100);
        return Obj("x", x, "y", y, "under", chain.Count > 0 ? chain[0].Key : null);
    }

    static bool SameBounds(Dictionary<string, object> a, Dictionary<string, object> b) {
        if (a == null || b == null) return false;
        foreach (string k in new string[] { "x", "y", "w", "h" })
            if (!b.ContainsKey(k) || Convert.ToInt32(a[k]) != Convert.ToInt32(b[k])) return false;
        return true;
    }

    static void Send(List<Native.INPUT> inputs) {
        Native.INPUT[] array = inputs.ToArray();
        if (Native.SendInput((uint)array.Length, array, Marshal.SizeOf(typeof(Native.INPUT))) != array.Length)
            throw new Fail("SendInput was refused (error " + Marshal.GetLastWin32Error() + "); is a window of an elevated app in front?");
    }

    static void Mouse(uint flags) {
        Native.INPUT input = new Native.INPUT();
        input.type = 0;  // INPUT_MOUSE
        input.u.mi.dwFlags = flags;
        Send(new List<Native.INPUT> { input });
    }

    static Native.INPUT Key(ushort vk, ushort scan, uint flags) {
        Native.INPUT input = new Native.INPUT();
        input.type = 1;  // INPUT_KEYBOARD
        input.u.ki.wVk = vk;
        input.u.ki.wScan = scan;
        input.u.ki.dwFlags = flags;
        return input;
    }

    static Native.INPUT VirtualKey(string name, bool up) {
        ushort vk = VK[name];
        uint flags = (EXTENDED.Contains(name) ? 0x1u : 0u) | (up ? 0x2u : 0u);  // KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP
        return Key(vk, (ushort)Native.MapVirtualKey(vk, 0), flags);  // MAPVK_VK_TO_VSC
    }

    static void Press(string key, List<string> modifiers) {
        if (key == null) throw new Fail("press needs a key");
        List<string> all = new List<string>(modifiers);
        all.Add(key);
        foreach (string name in all)
            if (!VK.ContainsKey(name)) throw new Fail("unknown key " + name);
        List<Native.INPUT> inputs = new List<Native.INPUT>();
        foreach (string m in modifiers) inputs.Add(VirtualKey(m, false));
        inputs.Add(VirtualKey(key, false));
        inputs.Add(VirtualKey(key, true));
        for (int i = modifiers.Count - 1; i >= 0; i--) inputs.Add(VirtualKey(modifiers[i], true));
        Send(inputs);
        Thread.Sleep(100);  // let the target handle it before the next call looks
    }

    /// Types text as Unicode characters (line breaks and tabs as keys); returns how many code points were typed.
    static int Type(string text) {
        int typed = 0;
        for (int i = 0; i < text.Length; i++) {
            char c = text[i];
            List<Native.INPUT> inputs = new List<Native.INPUT>();
            if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n') {
                typed++;  // CRLF is one Enter, but two characters of the text
                continue;
            }
            if (c == '\n' || c == '\r' || c == '\t') {
                string name = c == '\t' ? "tab" : "enter";
                inputs.Add(VirtualKey(name, false));
                inputs.Add(VirtualKey(name, true));
            } else {
                inputs.Add(Key(0, c, 0x4));  // KEYEVENTF_UNICODE
                inputs.Add(Key(0, c, 0x4 | 0x2));
                if (char.IsHighSurrogate(c) && i + 1 < text.Length) {
                    char low = text[++i];
                    inputs.Insert(1, Key(0, low, 0x4));
                    inputs.Add(Key(0, low, 0x4 | 0x2));
                }
            }
            Send(inputs);
            typed++;
            Thread.Sleep(TYPE_PAUSE_MS);
        }
        Thread.Sleep(100);
        return typed;
    }

    // MARK: - Tray menus

    // MSAA roles and states of menus: UI Automation shows a WinForms ContextMenuStrip as an empty
    // pane, while MSAA lists its items, and submenus' items, the same way as a Win32 menu's.
    const int ROLE_MENUPOPUP = 11, ROLE_MENUITEM = 12;
    const int STATE_UNAVAILABLE = 0x1, STATE_CHECKED = 0x10, STATE_INVISIBLE = 0x8000, STATE_HASPOPUP = 0x40000000;
    const uint OBJID_CLIENT = 0xFFFFFFFC;
    const string NOTIFY_ICONS = @"Control Panel\NotifyIconSettings";

    /// An item of a Tray menu: the object that answers for it (child 0: itself), its node, its submenu.
    public class TrayItem {
        public IAccessible Host;
        public object Child;
        public Dictionary<string, object> Node;
        public List<TrayItem> Submenu;  // null: none, or not listed yet
        public bool ListedWhenOpen;     // its submenu is a menu window of its own, listed only while it is open
        public IntPtr Window;           // that window, once opened
    }

    /// Every window of pid, hidden and message-only ones included: a Tray icon belongs to one of them.
    static List<IntPtr> ProcessWindows(int pid) {
        List<IntPtr> found = new List<IntPtr>();
        Native.EnumWindows(delegate(IntPtr hwnd, IntPtr param) {
            int owner;
            Native.GetWindowThreadProcessId(hwnd, out owner);
            if (owner == pid) found.Add(hwnd);
            return true;
        }, IntPtr.Zero);
        IntPtr message = IntPtr.Zero;
        while ((message = Native.FindWindowEx(new IntPtr(-3), message, null, null)) != IntPtr.Zero) {  // HWND_MESSAGE
            int owner;
            Native.GetWindowThreadProcessId(message, out owner);
            if (owner == pid) found.Add(message);
        }
        return found;
    }

    /// The Tray icon of one of the processes: (pid, its rectangle on screen), or null. Windows 11 keeps
    /// every icon an app showed under NotifyIconSettings, with the app's path and the icon's uID; the
    /// shell then tells where it is. An icon waiting among the hidden ones is promoted to the taskbar.
    static object[] TrayIcon(Process[] processes) {
        using (RegistryKey icons = Registry.CurrentUser.OpenSubKey(NOTIFY_ICONS, true)) {
            if (icons == null) return null;
            foreach (Process process in processes) {
                string path;
                try { path = process.MainModule.FileName; } catch (Exception) { continue; }
                foreach (string name in icons.GetSubKeyNames()) {
                    using (RegistryKey icon = icons.OpenSubKey(name, true)) {
                        if (icon == null || !string.Equals(icon.GetValue("ExecutablePath") as string, path, StringComparison.OrdinalIgnoreCase)) continue;
                        object uid = icon.GetValue("UID");
                        if (uid == null) continue;  // an icon known by a GUID only
                        object promoted = icon.GetValue("IsPromoted");
                        if (!(promoted is int) || (int)promoted != 1) {
                            icon.SetValue("IsPromoted", 1, RegistryValueKind.DWord);  // on the taskbar at once, until the next restore
                            Thread.Sleep(500);  // the shell answers where the icon is even while it moves: nothing to poll for
                        }
                        foreach (IntPtr hwnd in ProcessWindows(process.Id)) {
                            Native.NOTIFYICONIDENTIFIER id = new Native.NOTIFYICONIDENTIFIER();
                            id.cbSize = Marshal.SizeOf(typeof(Native.NOTIFYICONIDENTIFIER));
                            id.hWnd = hwnd;
                            id.uID = (uint)Convert.ToInt64(uid);
                            Native.RECT r;
                            if (Native.Shell_NotifyIconGetRect(ref id, out r) == 0 && r.Right > r.Left)
                                return new object[] { process.Id, r };
                        }
                    }
                }
            }
        }
        return null;
    }

    /// The visible windows of pid that are menus: their MSAA client lists menu items.
    static List<IntPtr> MenuWindows(int pid) {
        return ProcessWindows(pid).FindAll(delegate(IntPtr hwnd) {
            return Native.IsWindowVisible(hwnd) && MenuItems(Accessible(hwnd)).Count > 0;
        });
    }

    static IAccessible Accessible(IntPtr hwnd) {
        Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");  // IAccessible
        object found;
        return Native.AccessibleObjectFromWindow(hwnd, OBJID_CLIENT, ref iid, out found) == 0 ? found as IAccessible : null;
    }

    /// The items' nodes, in the contract's shape.
    static List<object> Nodes(List<TrayItem> items) {
        return items.ConvertAll(delegate(TrayItem i) { return (object)i.Node; });
    }

    static List<TrayItem> MenuItems(IAccessible parent) {
        List<TrayItem> items = new List<TrayItem>();
        if (parent == null) return items;
        object[] kids;
        try {
            int count = parent.accChildCount, got;
            kids = new object[count];
            if (count > 0) Native.AccessibleChildren(parent, 0, count, kids, out got);
        } catch (Exception) { return items; }
        foreach (object kid in kids) {
            IAccessible own = kid as IAccessible;
            IAccessible host = own ?? parent;
            object child = own != null ? (object)0 : kid;
            int role, state;
            string name;
            try {
                role = Convert.ToInt32(host.get_accRole(child));
                state = Convert.ToInt32(host.get_accState(child));
                name = host.get_accName(child) ?? "";
            } catch (Exception) { continue; }
            if (role == ROLE_MENUPOPUP && own != null) { items.AddRange(MenuItems(own)); continue; }  // a Win32 submenu's items
            if (role != ROLE_MENUITEM || (state & STATE_INVISIBLE) != 0) continue;  // separators too
            TrayItem item = new TrayItem();
            item.Host = host;
            item.Child = child;
            List<TrayItem> sub = own != null ? MenuItems(own) : new List<TrayItem>();
            item.ListedWhenOpen = sub.Count == 0 && (state & STATE_HASPOPUP) != 0;
            item.Submenu = sub.Count > 0 ? sub : null;
            item.Node = Obj("name", name, "enabled", (state & STATE_UNAVAILABLE) == 0, "checked", (state & STATE_CHECKED) != 0,
                "children", item.ListedWhenOpen ? null : Nodes(sub));
            items.Add(item);
        }
        return items;
    }

    /// Opens item's submenu window, as pointing at the item does, unless it is open: a WinForms
    /// submenu lists its items only while it is open. Lists them into item.Submenu.
    static void OpenSubmenu(TrayItem item, int pid, DateTime deadline, string wanted) {
        if (item.Window != IntPtr.Zero && Native.IsWindowVisible(item.Window)) return;
        List<IntPtr> before = MenuWindows(pid);
        item.Host.accDoDefaultAction(item.Child);
        List<IntPtr> added = WaitFor<List<IntPtr>>(deadline, delegate() {
            List<IntPtr> m = MenuWindows(pid).FindAll(delegate(IntPtr w) { return !before.Contains(w); });
            return m.Count > 0 ? m : null;
        });
        if (added == null) throw new Fail(item.Node["name"] + " in " + wanted + "'s Tray menu did not open its submenu");
        item.Window = added[0];
        item.Submenu = MenuItems(Accessible(added[0]));
    }

    /// Lists every submenu under level, opening each one that lists its items only while it is open
    /// (a disabled item's too: WinForms opens it all the same).
    static void ListSubmenus(List<TrayItem> level, int pid, DateTime deadline, string wanted) {
        foreach (TrayItem item in level) {
            if (item.ListedWhenOpen) {
                OpenSubmenu(item, pid, deadline, wanted);
                item.Node["children"] = Nodes(item.Submenu);
            }
            if (item.Submenu != null) ListSubmenus(item.Submenu, pid, deadline, wanted);
        }
    }

    /// Read an app's Tray menu, submenus included, and choose p["choose"] (a label per menu level)
    /// from it: the menu opens on a right click on the Tray icon, as a user opens it, and an item is
    /// chosen by its default action. Whatever is still open afterwards is closed with Escape.
    /// p["icon_only"]: answer whether the Tray icon is there, nothing more: finding it only promotes
    /// a hidden one to the taskbar, without input, so the foreground window stays as it was.
    static Dictionary<string, object> Tray(Dictionary<string, object> p) {
        string wanted = Str(p, "app");
        if (string.IsNullOrEmpty(wanted)) throw new Fail("tray needs an app");
        List<string> path = Strings(p, "choose");
        DateTime deadline = Deadline(p);
        object[] icon = TrayIcon(Process.GetProcessesByName(wanted));
        if (icon == null || Flag(p, "icon_only")) return Obj("icon", icon != null);
        int pid = (int)icon[0];
        Native.RECT r = (Native.RECT)icon[1];
        Native.SetCursorPos((r.Left + r.Right) / 2, (r.Top + r.Bottom) / 2);
        Thread.Sleep(50);
        Mouse(0x0008);  // MOUSEEVENTF_RIGHTDOWN
        Thread.Sleep(30);
        Mouse(0x0010);  // MOUSEEVENTF_RIGHTUP
        List<IntPtr> opened = WaitFor<List<IntPtr>>(deadline, delegate() { List<IntPtr> m = MenuWindows(pid); return m.Count > 0 ? m : null; });
        if (opened == null) throw new Fail(wanted + "'s Tray menu did not open on a right click on its Tray icon");
        try {
            List<TrayItem> top = MenuItems(Accessible(opened[0]));
            ListSubmenus(top, pid, deadline, wanted);
            List<object> items = Nodes(top);
            List<TrayItem> level = top;
            List<string> chosen = new List<string>();
            for (int n = 0; n < path.Count; n++) {
                string label = path[n];
                chosen.Add(label);
                if (level == null)
                    return Obj("icon", true, "items", items, "chosen", null, "failed", Obj("at", chosen, "reason", "leaf"));
                TrayItem item = level.Find(delegate(TrayItem i) { return (string)i.Node["name"] == label; });
                if (item == null || !(bool)item.Node["enabled"])
                    return Obj("icon", true, "items", items, "chosen", null, "failed", Obj("at", chosen, "reason", item == null ? "missing" : "disabled"));
                if (n + 1 == path.Count) {
                    if (item.Submenu != null || item.ListedWhenOpen)  // its default action would only open the submenu
                        return Obj("icon", true, "items", items, "chosen", null, "failed", Obj("at", chosen, "reason", "submenu"));
                    item.Host.accDoDefaultAction(item.Child);
                } else if (item.ListedWhenOpen) {
                    // Opened while reading, but opening a sibling's submenu may have closed it since:
                    // open it again, and choose from what is on screen.
                    OpenSubmenu(item, pid, deadline, wanted);
                }
                level = item.Submenu;
            }
            return Obj("icon", true, "items", items, "chosen", path.Count > 0 ? path : null, "failed", null);
        } finally {
            // An item chosen in a submenu leaves the menu open; reading one leaves it open too. Each
            // menu window is told to cancel, as Escape would, but no key goes to whatever is in front.
            DateTime closing = DateTime.UtcNow.AddSeconds(3);
            WaitFor<string>(closing, delegate() {
                List<IntPtr> open = MenuWindows(pid);
                foreach (IntPtr menu in open) {
                    Native.PostMessage(menu, 0x0100, new IntPtr(0x1B), IntPtr.Zero);  // WM_KEYDOWN, VK_ESCAPE
                    Native.PostMessage(menu, 0x0101, new IntPtr(0x1B), IntPtr.Zero);  // WM_KEYUP
                }
                return open.Count == 0 ? "closed" : null;
            });
        }
    }

    // MARK: - Clipboard

    static T Retry<T>(Func<T> what) {
        // The clipboard is busy while another app reads or writes it.
        for (int attempt = 0; ; attempt++) {
            try { return what(); } catch (ExternalException) { if (attempt >= 20) throw; }
            Thread.Sleep(100);
        }
    }

    static string Clipboard() {
        return Retry(delegate() {
            return System.Windows.Forms.Clipboard.ContainsText() ? Lines(System.Windows.Forms.Clipboard.GetText()) : null;
        });
    }

    static void SetClipboard(string text) {
        // Copied out of this process, so it outlives the call. Not SetText, which refuses "".
        Retry(delegate() {
            System.Windows.Forms.Clipboard.SetDataObject(new System.Windows.Forms.DataObject(System.Windows.Forms.DataFormats.UnicodeText, text), true);
            return true;
        });
    }

    // MARK: - Staging

    /// The selected text in the app's focused element, or null.
    static string Selection(int pid) {
        try {
            AutomationElement focused = AutomationElement.FocusedElement;
            if (focused == null || focused.Current.ProcessId != pid) return null;
            object pattern;
            if (!focused.TryGetCurrentPattern(TextPattern.Pattern, out pattern)) return null;
            TextPatternRange[] ranges = ((TextPattern)pattern).GetSelection();
            return ranges.Length == 0 ? null : RangeText(ranges[0]);
        } catch (Exception) {
            return null;
        }
    }

    /// The file name of the Staged document at path; fails unless it is a file stage-text writes.
    static string StagedName(string path) {
        string staging = Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\'), name = null, folder = null;
        try {
            name = Path.GetFileName(path);
            folder = Path.GetFullPath(Path.GetDirectoryName(path) ?? "").TrimEnd('\\');
        } catch (Exception) {
            // not a path at all: refused below
        }
        if (name == null || !STAGED.IsMatch(name) || !string.Equals(folder, staging, StringComparison.OrdinalIgnoreCase))
            throw new Fail(path + " is not a Staged document: stage-text writes them to " + staging + " as " + STAGE + "XXXXXXXX.txt");
        return name;
    }

    /// Does text (a title, a tab's name) name the document name? Its name without .txt, and no more
    /// hex digits after it, so another Staged document's name never counts.
    static bool Names(string text, string name) {
        return Regex.IsMatch(text ?? "", Regex.Escape(Path.GetFileNameWithoutExtension(name)) + "(?![0-9a-fA-F])");
    }

    static AutomationElementCollection Tabs(Win w) {
        return AutomationElement.FromHandle(w.Handle).FindAll(TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.TabItem));
    }

    /// The window (and tab) of app showing the document name, or null. The editor names a document
    /// by its file's name alone; the name's random hex digits are one stage-text's, and its folder is
    /// checked before (StagedName).
    static Staged StagedDocument(string app, string name) {
        foreach (Win w in Windows()) {
            if (!IsApp(w.Pid, app)) continue;
            AutomationElementCollection tabs;
            try {
                tabs = Tabs(w);
            } catch (ElementNotAvailableException) {
                continue;  // the window closed meanwhile
            }
            foreach (AutomationElement tab in tabs) {
                string tabName;
                try { tabName = tab.Current.Name; } catch (ElementNotAvailableException) { continue; }
                if (Names(tabName, name)) { Staged d = new Staged(); d.Window = w; d.Tab = tab; return d; }
            }
            if (tabs.Count == 0 && Names(w.Title, name)) { Staged d = new Staged(); d.Window = w; return d; }
        }
        return null;
    }

    /// The tab in front of a window (the selected one), or null.
    static AutomationElement SelectedTab(Win w) {
        try {
            foreach (AutomationElement tab in Tabs(w)) {
                object pattern;
                if (tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern) && ((SelectionItemPattern)pattern).Current.IsSelected) return tab;
            }
        } catch (ElementNotAvailableException) {
            // the window closed meanwhile
        }
        return null;
    }

    static void SelectTab(AutomationElement tab) {
        object pattern;
        if (tab.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)) ((SelectionItemPattern)pattern).Select();
    }

    /// Keyboard focus into the text of the document in front of window, its selection kept: a tab
    /// selected through UI Automation (WinUI's TabView) keeps the focus on the tab, where the
    /// Scenario's next keys (a copy, typing) do nothing.
    static void FocusDocument(IntPtr window) {
        try {
            AutomationElement root = AutomationElement.FromHandle(window);
            foreach (ControlType type in new ControlType[] { ControlType.Document, ControlType.Edit }) {
                foreach (AutomationElement e in root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, type))) {
                    if (e.Current.IsOffscreen || !e.Current.IsKeyboardFocusable) continue;
                    if (type == ControlType.Edit && !Multiline(e)) continue;
                    e.SetFocus();
                    return;
                }
            }
        } catch (ElementNotAvailableException) {
            // the window closed meanwhile
        } catch (ArgumentException) {
            // no such window any more
        } catch (InvalidOperationException) {
            // it takes no focus: keys go where the app puts them
        }
    }

    /// Save and close the Staged document p["file"] in p["app"]; the app's other documents stay, and
    /// the tab in front of its window before comes back to the front. Saved first (a Scenario may have
    /// typed into it), so the editor does not ask about saving. Keys go to the tab in front, the one
    /// the window's title names.
    static Dictionary<string, object> CloseStaged(Dictionary<string, object> p) {
        string path = Str(p, "file"), app = Str(p, "app");
        if (string.IsNullOrEmpty(path) || string.IsNullOrEmpty(app)) throw new Fail("close-staged needs file and app");
        string name = StagedName(path);
        DateTime deadline = Deadline(p);
        List<string> ctrl = new List<string> { "ctrl" };
        Staged d = StagedDocument(app, name);
        if (d == null) return Obj("file", path, "closed", false);
        AutomationElement before = d.Tab == null ? null : SelectedTab(d.Window);
        string beforeName = null;
        try { if (before != null) beforeName = before.Current.Name; } catch (ElementNotAvailableException) { }
        while (true) {
            BringToFront(d.Window.Handle, app, deadline);
            if (d.Tab == null) break;
            DateTime wait = DateTime.UtcNow.AddSeconds(CLOSE_WAIT), soon = wait < deadline ? wait : deadline;
            try { SelectTab(d.Tab); } catch (ElementNotAvailableException) { }
            IntPtr handle = d.Window.Handle;
            if (WaitFor<string>(soon, delegate() { return Names(Title(handle), name) ? name : null; }) != null) break;
            if (DateTime.UtcNow >= deadline)
                throw new Fail(app + " did not bring " + name + " to the front of its window in time; the window: " + Title(handle));
            d = StagedDocument(app, name);
            if (d == null) return Obj("file", path, "closed", false);  // closed meanwhile
        }
        Press("s", ctrl);
        if (d.Tab != null) Press("w", ctrl);
        else Native.PostMessage(d.Window.Handle, 0x0010, IntPtr.Zero, IntPtr.Zero);  // WM_CLOSE
        if (WaitFor<string>(deadline, delegate() { return StagedDocument(app, name) == null ? "" : null; }) == null)
            throw new Fail(app + " did not close " + name + " in time; its windows: "
                + json.Serialize(Windows().FindAll(delegate(Win w) { return IsApp(w.Pid, app); }).ConvertAll(delegate(Win w) { return w.Title; })));
        if (beforeName != null && !Names(beforeName, name)) {
            try {
                SelectTab(before);
                DateTime wait = DateTime.UtcNow.AddSeconds(CLOSE_WAIT), soon = wait < deadline ? wait : deadline;
                IntPtr handle = d.Window.Handle;
                WaitFor<string>(soon, delegate() { return Title(handle).Contains(beforeName) ? "" : null; });
            } catch (ElementNotAvailableException) {
                // closed meanwhile
            }
        }
        if (d.Tab != null) FocusDocument(d.Window.Handle);
        return Obj("file", path, "closed", true);
    }

    // MARK: - Notifications

    const string ISO = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'";
    const int SQLITE_ROW = 100, SQLITE_OPEN_READONLY = 1;
    const string TOASTS = "SELECT h.PrimaryId, n.ArrivalTime, n.Payload FROM Notification n "
        + "JOIN NotificationHandler h ON n.HandlerId = h.RecordId WHERE n.Type = 'toast'";

    /// Every Notification in the user's notification database (the Action Center's store), each app's
    /// under its AppUserModelID; ToastNotificationManager's history reads one app's only, without times.
    static Dictionary<string, object> Notifications() {
        string file = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Microsoft\Windows\Notifications\wpndatabase.db");
        List<object> found = new List<object>();
        string now = DateTime.UtcNow.ToString(ISO, System.Globalization.CultureInfo.InvariantCulture);
        if (!File.Exists(file)) return Obj("now", now, "notifications", found);
        IntPtr db, statement;
        if (Native.sqlite3_open_v2(Encoding.UTF8.GetBytes(file + "\0"), out db, SQLITE_OPEN_READONLY, IntPtr.Zero) != 0)
            throw new Fail("cannot open " + file + ": " + SqliteError(db));
        try {
            Native.sqlite3_busy_timeout(db, 2000);
            if (Native.sqlite3_prepare_v2(db, Encoding.UTF8.GetBytes(TOASTS + "\0"), -1, out statement, IntPtr.Zero) != 0)
                throw new Fail("cannot read " + file + ": " + SqliteError(db));
            try {
                while (Native.sqlite3_step(statement) == SQLITE_ROW) {
                    List<string> texts = ToastTexts(Encoding.UTF8.GetString(Column(statement, 2)));
                    found.Add(Obj(
                        "app", Encoding.UTF8.GetString(Column(statement, 0)),
                        "title", texts.Count > 0 ? texts[0] : "",
                        "body", string.Join("\n", texts.GetRange(Math.Min(1, texts.Count), Math.Max(0, texts.Count - 1)).ToArray()),
                        "time", DateTime.FromFileTimeUtc(Native.sqlite3_column_int64(statement, 1)).ToString(ISO, System.Globalization.CultureInfo.InvariantCulture)));
                }
            } finally {
                Native.sqlite3_finalize(statement);
            }
        } finally {
            Native.sqlite3_close(db);
        }
        return Obj("now", now, "notifications", found);
    }

    static byte[] Column(IntPtr statement, int column) {
        byte[] bytes = new byte[Native.sqlite3_column_bytes(statement, column)];
        if (bytes.Length > 0) Marshal.Copy(Native.sqlite3_column_blob(statement, column), bytes, 0, bytes.Length);
        return bytes;
    }

    static string SqliteError(IntPtr db) {
        return db == IntPtr.Zero ? "out of memory" : Marshal.PtrToStringAnsi(Native.sqlite3_errmsg(db));
    }

    /// The texts of a Notification's first binding: its title, then its body's lines.
    static List<string> ToastTexts(string payload) {
        List<string> texts = new List<string>();
        System.Xml.XmlDocument xml = new System.Xml.XmlDocument();
        try { xml.LoadXml(payload); } catch (System.Xml.XmlException) { return texts; }
        System.Xml.XmlNode binding = xml.SelectSingleNode("/toast/visual/binding");
        if (binding != null)
            foreach (System.Xml.XmlNode text in binding.SelectNodes("text")) texts.Add(text.InnerText.Trim());
        return texts;
    }

    static Dictionary<string, object> StageText(Dictionary<string, object> p) {
        string text = Str(p, "text"), app = Str(p, "app");
        if (text == null || string.IsNullOrEmpty(app)) throw new Fail("stage-text needs text and app");
        DateTime deadline = Deadline(p);
        string stem = STAGE + Guid.NewGuid().ToString("N").Substring(0, 8);
        string path = Path.Combine(Path.GetTempPath(), stem + ".txt");
        File.WriteAllText(path, text, new UTF8Encoding(false));
        try {
            ProcessStartInfo start = new ProcessStartInfo(app, "\"" + path + "\"");
            start.UseShellExecute = true;
            Process.Start(start);
        } catch (System.ComponentModel.Win32Exception e) {
            throw new Fail("cannot start " + app + ": " + e.Message + "; is it installed?");
        }
        List<Win> seen = new List<Win>();
        Win window = WaitFor<Win>(deadline, delegate() {
            seen = Windows();
            return seen.Find(delegate(Win w) { return w.Title.Contains(stem); });
        });
        if (window == null)
            throw new Fail(app + " showed no window for " + Path.GetFileName(path) + " in time; windows: "
                + json.Serialize(seen.ConvertAll(delegate(Win w) { return w.Title; })));
        string name = AppName(window.Pid);
        BringToFront(window.Handle, name, deadline);
        // A select-all pressed before the editor has loaded the file, or while the focus is on its
        // tab, selects nothing: pressed again, into the document, until the text is selected.
        string selected;
        while (true) {
            FocusDocument(window.Handle);
            Press("a", new List<string> { "ctrl" });
            DateTime wait = DateTime.UtcNow.AddSeconds(SELECT_WAIT), soon = wait < deadline ? wait : deadline;
            selected = WaitFor<string>(soon, delegate() { return Selection(window.Pid) == text ? text : null; });
            if (selected != null || DateTime.UtcNow >= deadline) break;
        }
        selected = selected ?? Selection(window.Pid);
        string frontmost = Frontmost();  // what the trigger lands on, recorded before it is pressed
        object pressed = null;
        object then;
        if (p.TryGetValue("then", out then) && then is Dictionary<string, object>) {
            Dictionary<string, object> chord = (Dictionary<string, object>)then;
            List<string> modifiers = Strings(chord, "modifiers");
            Press(Str(chord, "key"), modifiers);
            pressed = Obj("key", Str(chord, "key"), "modifiers", modifiers);
        }
        return Obj("app", name, "file", path, "frontmost", frontmost, "selected", selected, "pressed", pressed);
    }
}
}
'@

$dir = Join-Path $env:ProgramData 'vmlab\ui'
$sha = [Security.Cryptography.SHA1]::Create()
$hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))[0..5] | ForEach-Object { $_.ToString('x2') })
$dll = Join-Path $dir "vmlab-ui-$hash.dll"
if (-not (Test-Path -LiteralPath $dll)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # Compiled aside and renamed: a concurrent call never loads half a file.
    $part = "$dll.$PID.part"
    Add-Type -TypeDefinition $source -OutputAssembly $part -OutputType Library -ReferencedAssemblies @(
        'UIAutomationClient', 'UIAutomationTypes', 'WindowsBase', 'System.Windows.Forms', 'System.Drawing', 'System.Web.Extensions', 'System.Core', 'System.Xml', 'Accessibility')
    try { Move-Item -LiteralPath $part -Destination $dll -ErrorAction Stop } catch { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $part }
    # Other vmlab versions' copies; one a running call has loaded stays until next time.
    Get-ChildItem -LiteralPath $dir -Filter 'vmlab-ui-*.dll' | Where-Object { $_.FullName -ne $dll } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
Add-Type -Path $dll
exit [VmlabUi.Helper]::Run($Command, $Params)
