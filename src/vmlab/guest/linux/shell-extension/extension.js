// vmlab-ui@vmlab: what vmlab's UI helper (../vmlab-ui.py) needs from GNOME Shell on Wayland.
//
// A Wayland client cannot learn where its windows are, raise another app's
// window, move the pointer to a point or read the clipboard without focus, and
// GNOME Shell's own Introspect interface refuses callers it does not know. This
// extension, installed only in vmlab's throwaway Guests, answers on the session
// bus as org.vmlab.Shell (/org/vmlab/Shell):
//
//   Version() -> u
//   Windows() -> s          JSON {"screen": [w, h], "windows": [...]}, bottom to top
//   Activate(t id) -> b     raise and focus a window
//   Click(d x, d y)         a left click at a point, through a virtual pointer
//   Keys(au codes)          press evdev key codes in order, release them in reverse
//   Type(s text)            text into the focused window, whatever the keyboard layout
//   ClipboardGet() -> s     JSON {"text": s or null}
//   ClipboardSet(s text)
//
// Input goes through Clutter virtual devices, so it lands exactly where asked
// (ydotool's relative pointer moves are scaled by pointer acceleration). Calls
// that send input return only after pacing, so the next call's events or
// clipboard read come after these have been delivered.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const VERSION = 1;
const PACE_MS = 15;  // between input events, and before a call that sent some returns
const KEY_ENTER = 28, KEY_TAB = 15;

const IFACE = `<node><interface name="org.vmlab.Shell">
  <method name="Version"><arg type="u" direction="out"/></method>
  <method name="Windows"><arg type="s" direction="out"/></method>
  <method name="Activate"><arg type="t" direction="in"/><arg type="b" direction="out"/></method>
  <method name="Click"><arg type="d" direction="in"/><arg type="d" direction="in"/></method>
  <method name="Keys"><arg type="au" direction="in"/></method>
  <method name="Type"><arg type="s" direction="in"/></method>
  <method name="ClipboardGet"><arg type="s" direction="out"/></method>
  <method name="ClipboardSet"><arg type="s" direction="in"/></method>
</interface></node>`;

function rect(r) {
    return [r.x, r.y, r.width, r.height];
}

export default class VmlabUiExtension {
    enable() {
        const seat = Clutter.get_default_backend().get_default_seat();
        this._pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/vmlab/Shell');
        this._owner = Gio.bus_own_name_on_connection(Gio.DBus.session, 'org.vmlab.Shell', Gio.BusNameOwnerFlags.NONE, null, null);
        this._timeouts = new Set();
    }

    disable() {
        for (const id of this._timeouts)
            GLib.source_remove(id);
        this._timeouts = null;
        Gio.bus_unown_name(this._owner);
        this._dbus.unexport();
        this._dbus = this._pointer = this._keyboard = null;
    }

    _windows() {
        return global.display.sort_windows_by_stacking(global.get_window_actors().map(a => a.meta_window));
    }

    // Run steps PACE_MS apart, then answer the call PACE_MS after the last one.
    _paced(invocation, steps) {
        const next = () => {
            const step = steps.shift();
            if (step) {
                step();
                return GLib.SOURCE_CONTINUE;
            }
            this._timeouts.delete(id);
            invocation.return_value(null);
            return GLib.SOURCE_REMOVE;
        };
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PACE_MS, next);
        this._timeouts.add(id);
    }

    _now() {
        return GLib.get_monotonic_time();
    }

    Version() {
        return VERSION;
    }

    Windows() {
        const focus = global.display.focus_window;
        const [width, height] = global.display.get_size();
        return JSON.stringify({
            screen: [width, height],
            windows: this._windows().map(w => ({
                id: w.get_id(),
                pid: w.get_pid(),
                title: w.get_title() || '',
                frame: rect(w.get_frame_rect()),
                buffer: rect(w.get_buffer_rect()),
                focused: w === focus,
                hidden: w.minimized || w.is_hidden(),
                type: w.get_window_type(),
            })),
        });
    }

    Activate(id) {
        const window = this._windows().find(w => w.get_id() === id);
        if (!window)
            return false;
        Main.activateWindow(window);
        return true;
    }

    ClickAsync([x, y], invocation) {
        this._paced(invocation, [
            () => this._pointer.notify_absolute_motion(this._now(), x, y),
            () => this._pointer.notify_button(this._now(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED),
            () => this._pointer.notify_button(this._now(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED),
        ]);
    }

    _keySteps(codes) {
        return codes.map(c => () => this._keyboard.notify_key(this._now(), c, Clutter.KeyState.PRESSED))
            .concat([...codes].reverse().map(c => () => this._keyboard.notify_key(this._now(), c, Clutter.KeyState.RELEASED)));
    }

    KeysAsync([codes], invocation) {
        this._paced(invocation, this._keySteps(codes));
    }

    // Wayland clients get text through the input method, as from the on-screen keyboard:
    // any character, on any layout. X11 clients (Xwayland) have no input method there,
    // so they get key events for the characters the current layout has.
    TypeAsync([text], invocation) {
        const focus = global.display.focus_window;
        const steps = [];
        if (focus && focus.get_client_type() === Meta.WindowClientType.X11) {
            for (const ch of text) {
                const keyval = ch === '\n' ? Clutter.KEY_Return : ch === '\t' ? Clutter.KEY_Tab : Clutter.unicode_to_keysym(ch.codePointAt(0));
                steps.push(() => this._keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.PRESSED));
                steps.push(() => this._keyboard.notify_keyval(this._now(), keyval, Clutter.KeyState.RELEASED));
            }
        } else {
            for (const part of text.split(/([\n\t])/)) {
                if (part === '\n' || part === '\t')
                    steps.push(...this._keySteps([part === '\n' ? KEY_ENTER : KEY_TAB]));
                else if (part)
                    steps.push(() => Main.inputMethod.commit(part));
            }
        }
        this._paced(invocation, steps);
    }

    ClipboardGetAsync(params, invocation) {
        St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (clipboard, text) => {
            invocation.return_value(new GLib.Variant('(s)', [JSON.stringify({text: text ?? null})]));
        });
    }

    ClipboardSet(text) {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
    }
}
