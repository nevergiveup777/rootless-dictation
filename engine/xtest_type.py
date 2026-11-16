#!/usr/bin/env python3
"""Decktation Rootless - Phase 1, probes A1/A2: text entry via X11 XTEST as `deck`.

Run on the Steam Deck as the normal `deck` user, in Gaming Mode, with
WoW Forever focused and the Quick Access Menu closed:

    /usr/bin/python3 -I xtest_type.py --dry-run               # check only, inject nothing
    /usr/bin/python3 -I xtest_type.py --open-chat             # A1: type "Hello World"
    /usr/bin/python3 -I xtest_type.py --open-chat --paste     # A2: paste UTF-8 text
    /usr/bin/python3 -I xtest_type.py --open-chat --paste --text "/p rootless test" --send-return

Default behaviour leaves the text as an UNSENT draft. Nothing is submitted
unless --send-return is given (when testing by hand, use it only with a
harmless message, e.g. "/p ..." while not in a party, so the game just answers
that you are not in a party).

--close-after-send presses Esc after sending, for games whose chat box stays
open after sending (WoW Forever on the user's Deck, where B closes it). Do not
use it if the chat box closes by itself: Esc would then reach the game and may
open its menu.

Mechanism: synthetic key events through the XTEST extension of the game's
Xwayland display (default :1), the same thing `xdotool` does. Paste mode
puts the text on that display's CLIPBOARD from a hidden Tk window, then
sends Ctrl+V. It waits until the game has actually read the clipboard (plus
PASTE_MARGIN), or PASTE_MAX_WAIT if no read is seen. No device nodes, no
uinput, no root. Your previous clipboard content on the game display is not
restored.

    /usr/bin/python3 -I xtest_type.py --clipboard-check   # clipboard round trip only, no keys
"""
import argparse
import ctypes
import glob
import os
import sys
import time

XK_RETURN, XK_SHIFT_L, XK_CONTROL_L, XK_V, XK_ESCAPE = 0xFF0D, 0xFFE1, 0xFFE3, 0x0076, 0xFF1B
OPEN_CHAT_WAIT = 0.4   # Return -> chat box open and focused (proven since 0.1)
PASTE_MAX_WAIT = 2.0   # the old fixed wait after Ctrl+V; now only used if no clipboard read is seen
PASTE_MARGIN = 0.25    # after the game has read the clipboard, before Return (upstream waits 0.3 s)
SEND_TO_ESC = 0.3      # Return (send) -> Esc (close the chat box); key events stay in order
CHECK_TEXT = "/p Hello, world! ¿Sí? Ünïcödé ñ á — €"


def load_x():
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    vp, ul = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], vp
    x11.XCloseDisplay.argtypes = [vp]
    x11.XFlush.argtypes = [vp]
    x11.XSync.argtypes = [vp, ctypes.c_int]
    x11.XKeysymToKeycode.argtypes, x11.XKeysymToKeycode.restype = [vp, ul], ctypes.c_ubyte
    x11.XkbKeycodeToKeysym.argtypes = [vp, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int]
    x11.XkbKeycodeToKeysym.restype = ul
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [vp], ul
    x11.XInternAtom.argtypes, x11.XInternAtom.restype = [vp, ctypes.c_char_p, ctypes.c_int], ul
    x11.XGetWindowProperty.argtypes = [
        vp, ul, ul, ctypes.c_long, ctypes.c_long, ctypes.c_int, ul,
        ctypes.POINTER(ul), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ul), ctypes.POINTER(ul),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
    x11.XFree.argtypes = [vp]
    xtst.XTestQueryExtension.argtypes = [vp] + [ctypes.POINTER(ctypes.c_int)] * 4
    xtst.XTestFakeKeyEvent.argtypes = [vp, ctypes.c_uint, ctypes.c_int, ul]
    return x11, xtst


def focused_apps(x11):
    """GAMESCOPE_FOCUSED_APP from every reachable display (for the record)."""
    out = {}
    for sock in sorted(glob.glob("/tmp/.X11-unix/X*")):
        disp = ":" + os.path.basename(sock)[1:]
        dpy = x11.XOpenDisplay(disp.encode())
        if not dpy:
            continue
        try:
            atom = x11.XInternAtom(dpy, b"GAMESCOPE_FOCUSED_APP", True)
            if not atom:
                continue
            t, f, n, r = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
            data = ctypes.POINTER(ctypes.c_ubyte)()
            ok = x11.XGetWindowProperty(dpy, x11.XDefaultRootWindow(dpy), atom, 0, 1, False, 0,
                                        ctypes.byref(t), ctypes.byref(f), ctypes.byref(n),
                                        ctypes.byref(r), ctypes.byref(data)) == 0
            if ok and data:
                if f.value == 32 and n.value >= 1:
                    # Xlib sign-extends format-32 items into a C long; keep 32 bits.
                    out[disp] = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0] & 0xFFFFFFFF
                x11.XFree(data)
        finally:
            x11.XCloseDisplay(dpy)
    return out


class Keyboard:
    def __init__(self, x11, xtst, dpy, key_delay):
        self.x11, self.xtst, self.dpy, self.key_delay = x11, xtst, dpy, key_delay

    def resolve(self, keysym):
        """Return (keycode, needs_shift) or None if the keysym is not in the current keymap."""
        kc = self.x11.XKeysymToKeycode(self.dpy, keysym)
        if not kc:
            return None
        if self.x11.XkbKeycodeToKeysym(self.dpy, kc, 0, 0) == keysym:
            return kc, False
        if self.x11.XkbKeycodeToKeysym(self.dpy, kc, 0, 1) == keysym:
            return kc, True
        return None

    def _event(self, kc, down):
        self.xtst.XTestFakeKeyEvent(self.dpy, kc, 1 if down else 0, 0)
        self.x11.XFlush(self.dpy)

    def tap(self, kc, modifiers=()):
        for m in modifiers:
            self._event(m, True)
        self._event(kc, True)
        time.sleep(0.012)
        self._event(kc, False)
        for m in reversed(modifiers):
            self._event(m, False)
        self.x11.XSync(self.dpy, 0)
        time.sleep(self.key_delay)


def pump(tk, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if tk is not None:
            tk.update()
        time.sleep(0.02)


def own_clipboard(display, text, reads):
    """Own CLIPBOARD on `display` from a hidden Tk window and serve `text`.
    Each time another client (the game) reads it, the time is added to `reads`.
    Tk serves both STRING and UTF8_STRING from this handler, as for `clipboard append`."""
    import tkinter
    tk = tkinter.Tk(screenName=display)
    tk.withdraw()

    def serve(offset, length):
        reads.append(time.monotonic())
        start = int(offset)
        return text[start:start + int(length)]

    tk.selection_handle(serve, selection="CLIPBOARD")
    tk.selection_own(selection="CLIPBOARD")
    tk.update()
    return tk


def wait_for_read(tk, reads, since, seconds):
    """Pump Tk until a clipboard read after `since`; return its time, or None after `seconds`."""
    end = since + seconds
    while time.monotonic() < end:
        tk.update()
        after = [t for t in reads if t >= since]
        if after:
            return after[0]
        time.sleep(0.01)
    return None


def clipboard_check(display):
    """Own the clipboard and read it back from a second process (UTF8_STRING and STRING). No keys."""
    reads = []
    try:
        tk = own_clipboard(display, CHECK_TEXT, reads)
    except Exception as e:  # tkinter.TclError, ImportError
        print(f"BLOCKER: cannot own the CLIPBOARD on {display} via Tk: {e}")
        return 2
    import subprocess
    reader = ("import sys, tkinter\nt = tkinter.Tk(screenName=sys.argv[1]); t.withdraw()\n"
              "for kind in ('UTF8_STRING', 'STRING'):\n"
              "    print(ascii(t.selection_get(selection='CLIPBOARD', type=kind)))\n")
    child = subprocess.Popen([sys.executable, "-I", "-c", reader, display], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    end = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < end:
        tk.update()
        time.sleep(0.01)
    if child.poll() is None:
        child.kill()
    out, err = child.communicate()
    tk.destroy()
    got = out.decode("ascii", "replace").splitlines()
    want_utf8, want_string = ascii(CHECK_TEXT), ascii(CHECK_TEXT.encode("latin-1", "replace").decode("latin-1"))
    print(f"reader got UTF8_STRING {got[0] if got else '-'}; STRING {got[1] if len(got) > 1 else '-'}; "
          f"owner saw {len(reads)} read(s)")
    if got[:1] == [want_utf8] and reads:
        print(f"CLIPBOARD CHECK OK: the text came back exactly as UTF8_STRING, and each read was seen"
              f"{'; STRING is the Latin-1 form' if got[1:2] == [want_string] else '; STRING differs'}")
        return 0
    print(f"CLIPBOARD CHECK FAILED: expected {want_utf8}. Reader error: {err.decode('utf-8', 'replace')[-300:]}")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--display", default=":1", help="game Xwayland display (default :1)")
    ap.add_argument("--paste", action="store_true", help="A2: clipboard + Ctrl+V instead of typing")
    ap.add_argument("--text", help="text to enter (default depends on mode)")
    ap.add_argument("--open-chat", action="store_true", help="press Return first to open chat")
    ap.add_argument("--send-return", action="store_true", help="press Return afterwards to try to SEND")
    ap.add_argument("--close-after-send", action="store_true",
                    help="with --send-return: press Esc afterwards to close a chat box that stays open")
    ap.add_argument("--countdown", type=int, default=10, help="seconds to focus the game (default 10)")
    ap.add_argument("--key-delay", type=float, default=0.03, help="seconds between keys (default 0.03)")
    ap.add_argument("--dry-run", action="store_true", help="check everything, inject nothing")
    ap.add_argument("--expect-app", type=int,
                    help="abort without injecting unless gamescope reports this app id as focused")
    ap.add_argument("--clipboard-check", action="store_true",
                    help="only own the clipboard and read it back from a second process; no keys")
    args = ap.parse_args()

    if os.geteuid() == 0:
        sys.exit("Refusing to run as root. Run this as the normal deck user.")
    if args.clipboard_check:
        return clipboard_check(args.display)
    text = args.text if args.text is not None else (
        "/p Hello, world! ¿Sí? Ünïcödé" if args.paste else "Hello World")
    print(f"uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()}")

    try:
        x11, xtst = load_x()
    except OSError as e:
        print(f"BLOCKER: cannot load libX11/libXtst: {e}")
        return 2
    dpy = x11.XOpenDisplay(args.display.encode())
    if not dpy:
        print(f"BLOCKER: cannot open X display {args.display} as this user.")
        return 2
    ints = [ctypes.c_int() for _ in range(4)]
    if not xtst.XTestQueryExtension(dpy, *(ctypes.byref(i) for i in ints)):
        print(f"BLOCKER: XTEST extension not available on {args.display}.")
        return 2
    print(f"OK: {args.display} open, XTEST v{ints[2].value}.{ints[3].value}")
    print(f"GAMESCOPE_FOCUSED_APP per display: {focused_apps(x11)}")

    kb = Keyboard(x11, xtst, dpy, args.key_delay)
    close = args.close_after_send and args.send_return
    keys = [("Return", XK_RETURN), ("Shift_L", XK_SHIFT_L), ("Control_L", XK_CONTROL_L), ("v", XK_V)]
    if close:
        keys.append(("Escape", XK_ESCAPE))
    special = {name: kb.resolve(ks) for name, ks in keys}
    missing = [n for n, v in special.items() if v is None]
    if missing:
        print(f"BLOCKER: keys missing from the {args.display} keymap: {missing}")
        return 2

    plan = []
    if not args.paste:
        for ch in text:
            if not (0x20 <= ord(ch) <= 0x7E):
                print(f"Typing mode is ASCII-only; {ch!r} is not. Use --paste for this text.")
                return 1
            res = kb.resolve(ord(ch))
            if res is None:
                print(f"BLOCKER: {ch!r} has no key in the current {args.display} keymap.")
                return 2
            plan.append((ch, res))
    print(f"mode={'paste' if args.paste else 'type'} text={text!r} open_chat={args.open_chat} "
          f"send_return={args.send_return} close_after_send={close}")
    if args.dry_run:
        print("DRY RUN: all checks passed; nothing was injected.")
        x11.XCloseDisplay(dpy)
        return 0

    tk = None
    reads = []
    if args.paste:
        try:
            tk = own_clipboard(args.display, text, reads)
        except Exception as e:  # tkinter.TclError, ImportError
            print(f"BLOCKER: cannot own the CLIPBOARD on {args.display} via Tk: {e}")
            return 2
        print("clipboard set on the game display")

    if args.send_return:
        print("WARNING: --send-return will try to SUBMIT the message. Ctrl+C now to abort.")
    for left in range(args.countdown, 0, -1):
        print(f"  injecting in {left} s - focus WoW Forever, close the Quick Access Menu", flush=True)
        pump(tk, 1.0)
    focus = focused_apps(x11)
    print(f"GAMESCOPE_FOCUSED_APP per display: {focus}")
    if args.expect_app is not None and args.expect_app not in focus.values():
        print(f"ABORT: app {args.expect_app} is not focused; nothing was injected.")
        if tk is not None:
            tk.destroy()
        x11.XCloseDisplay(dpy)
        return 3

    ret_kc = special["Return"][0]
    if args.open_chat:
        kb.tap(ret_kc)
        pump(tk, OPEN_CHAT_WAIT)
    if args.paste:
        before = len(reads)
        pasted_at = time.monotonic()
        kb.tap(special["v"][0], modifiers=(special["Control_L"][0],))
        read_at = wait_for_read(tk, reads, pasted_at, PASTE_MAX_WAIT)  # Wine reads the clipboard asynchronously
        if read_at is not None:
            pump(tk, PASTE_MARGIN)  # keep serving while the game inserts the text
            print(f"TIMING: the game read the clipboard {read_at - pasted_at:.2f} s after Ctrl+V")
        else:
            pump(tk, 0.3)  # the old wait before Return
            print(f"TIMING: no clipboard read seen within {PASTE_MAX_WAIT:.1f} s after Ctrl+V"
                  f"{f' ({before} read(s) before Ctrl+V)' if before else ''}; used the old waits")
    else:
        shift_kc = special["Shift_L"][0]
        for _, (kc, shift) in plan:
            kb.tap(kc, modifiers=(shift_kc,) if shift else ())
        if args.send_return:
            pump(tk, 0.3)
    if args.send_return:
        kb.tap(ret_kc)
        if close:
            pump(tk, SEND_TO_ESC)
            kb.tap(special["Escape"][0])  # after the game has handled the send
        pump(tk, 0.05)

    if tk is not None:
        tk.destroy()
    x11.XCloseDisplay(dpy)
    print("DONE. Check the chat box: is the text exactly", repr(text),
          "and was it", ("sent and the chat closed?" if close else "sent?") if args.send_return else "left unsent?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
