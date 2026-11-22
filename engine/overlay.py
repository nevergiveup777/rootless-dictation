#!/usr/bin/env python3
"""Rootless Dictation - on-screen recording symbol (started by ptt_pipeline.py).

Draws a small microphone symbol at the top of the screen, over the game:

    wait   grey      buttons held, microphone starting: not yet
    talk   red, pulsing   the microphone is listening: talk now
    busy   blue, moving dots   turning speech into text and typing it
    done   green check     the text is in the chat (hides after a moment)
    fail   orange cross    nothing was typed (hides after a moment)
    hide

It reads one state word per line on stdin and exits when stdin closes, so it
never outlives the engine. It also exits as soon as its display closes, even
while hidden; the engine then starts a new one. Usage:

    /usr/bin/python3 -I overlay.py            # normal use, states on stdin
    /usr/bin/python3 -I overlay.py --check    # show the symbol for 1.5 s and report

How: a transparent, input-transparent X11 window on Gamescope's main
Xwayland display, marked with Gamescope's GAMESCOPE_EXTERNAL_OVERLAY window
property, which is how Gaming Mode overlays (such as MangoHud's mangoapp) are
drawn above games. It runs as the normal user, uses only libX11 and libXext
through ctypes, sends no input, reads nothing from the game, and does not
change any setting. The idea comes from Decktation's recording overlay; this
code was written separately.

Showing and hiding, from the Deck tests:
- Hiding never unmaps: "hide" draws the empty picture for CLEAR_TIME while the
  window is still visible, then sets _NET_WM_WINDOW_OPACITY to 0, as
  Decktation's overlay does. The last picture Gamescope has is therefore always
  the empty one. (0.3.0 unmapped to hide, and the symbol stayed on screen for
  10-30 s.)
- While visible, the current picture is drawn again every frame (0.1 s), also
  for still pictures. (0.3.1 drew them once, and some never appeared.)
- Showing unmaps and maps the invisible window again, then sets it opaque.
  With only the opacity change (0.3.1, 0.3.2), the first one or two symbols
  after the window was created did not appear (Deck test of 0.3.2).
"""
import argparse
import ctypes
import glob
import math
import os
import select
import sys
import time

STATES = ("wait", "talk", "busy", "done", "fail", "hide")
AUTO_HIDE = {"done": 1.2, "fail": 1.8}   # seconds
OPAQUE = 0xFFFFFFFF                      # _NET_WM_WINDOW_OPACITY: 0 = invisible
CLEAR_TIME = 0.3                         # seconds the empty picture is shown before the window is made invisible
FRAME = 0.1                              # seconds per animation frame
TALK_FRAMES, BUSY_FRAMES = 8, 6
COLORS = {"wait": (0.42, 0.43, 0.47), "talk": (0.85, 0.13, 0.13), "busy": (0.17, 0.36, 0.66),
          "done": (0.13, 0.60, 0.28), "fail": (0.86, 0.45, 0.08)}

vp, ul = ctypes.c_void_p, ctypes.c_ulong


class XVisualInfo(ctypes.Structure):
    _fields_ = [("visual", vp), ("visualid", ul), ("screen", ctypes.c_int), ("depth", ctypes.c_int),
                ("c_class", ctypes.c_int), ("red_mask", ul), ("green_mask", ul), ("blue_mask", ul),
                ("colormap_size", ctypes.c_int), ("bits_per_rgb", ctypes.c_int)]


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", ul), ("background_pixel", ul), ("border_pixmap", ul),
                ("border_pixel", ul), ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                ("backing_store", ctypes.c_int), ("backing_planes", ul), ("backing_pixel", ul),
                ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
                ("colormap", ul), ("cursor", ul)]


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", vp), ("resourceid", ul), ("serial", ul),
                ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte), ("minor_code", ctypes.c_ubyte)]


ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, vp, ctypes.POINTER(XErrorEvent))
X_ERRORS = []


@ERROR_HANDLER
def on_x_error(_dpy, ev):
    X_ERRORS.append((ev.contents.error_code, ev.contents.request_code))
    return 0


def load_x():
    x = ctypes.CDLL("libX11.so.6")
    sig = {
        "XOpenDisplay": ([ctypes.c_char_p], vp), "XCloseDisplay": ([vp], ctypes.c_int),
        "XDefaultScreen": ([vp], ctypes.c_int), "XRootWindow": ([vp, ctypes.c_int], ul),
        "XDisplayWidth": ([vp, ctypes.c_int], ctypes.c_int), "XDisplayHeight": ([vp, ctypes.c_int], ctypes.c_int),
        "XInternAtom": ([vp, ctypes.c_char_p, ctypes.c_int], ul),
        "XGetWindowProperty": ([vp, ul, ul, ctypes.c_long, ctypes.c_long, ctypes.c_int, ul, ctypes.POINTER(ul),
                                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ul), ctypes.POINTER(ul),
                                ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
        "XFree": ([vp], ctypes.c_int),
        "XMatchVisualInfo": ([vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(XVisualInfo)], ctypes.c_int),
        "XCreateColormap": ([vp, ul, vp, ctypes.c_int], ul),
        "XCreateWindow": ([vp, ul, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_int, ctypes.c_uint, vp, ul, ctypes.POINTER(XSetWindowAttributes)], ul),
        "XChangeProperty": ([vp, ul, ul, ul, ctypes.c_int, ctypes.c_int, vp, ctypes.c_int], ctypes.c_int),
        "XStoreName": ([vp, ul, ctypes.c_char_p], ctypes.c_int),
        "XCreateGC": ([vp, ul, ul, vp], vp), "XFreeGC": ([vp, vp], ctypes.c_int),
        "XCreateImage": ([vp, vp, ctypes.c_uint, ctypes.c_int, ctypes.c_int, vp, ctypes.c_uint, ctypes.c_uint,
                          ctypes.c_int, ctypes.c_int], vp),
        "XPutImage": ([vp, ul, vp, vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
        "XMapRaised": ([vp, ul], ctypes.c_int), "XUnmapWindow": ([vp, ul], ctypes.c_int),
        "XResizeWindow": ([vp, ul, ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
        "XClearWindow": ([vp, ul], ctypes.c_int),
        "XDestroyWindow": ([vp, ul], ctypes.c_int), "XFlush": ([vp], ctypes.c_int),
        "XSync": ([vp, ctypes.c_int], ctypes.c_int), "XPending": ([vp], ctypes.c_int),
        "XConnectionNumber": ([vp], ctypes.c_int), "XNextEvent": ([vp, vp], ctypes.c_int),
        "XSetErrorHandler": ([ERROR_HANDLER], vp),
    }
    for name, (args, res) in sig.items():
        f = getattr(x, name)
        f.argtypes, f.restype = args, res
    try:
        xext = ctypes.CDLL("libXext.so.6")
        xext.XShapeCombineRectangles.argtypes = [vp, ul, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp,
                                                 ctypes.c_int, ctypes.c_int, ctypes.c_int]
    except OSError:
        xext = None
    return x, xext


def root_cardinal(x, dpy, name):
    """A 32-bit CARDINAL property of the root window, or None."""
    atom = x.XInternAtom(dpy, name, True)
    if not atom:
        return None
    rtype, rfmt, n, after = ul(), ctypes.c_int(), ul(), ul()
    data = ctypes.c_void_p()
    root = x.XRootWindow(dpy, x.XDefaultScreen(dpy))
    if x.XGetWindowProperty(dpy, root, atom, 0, 1, False, 6, ctypes.byref(rtype), ctypes.byref(rfmt),
                            ctypes.byref(n), ctypes.byref(after), ctypes.byref(data)) != 0 or not data.value:
        return None
    try:
        return ctypes.cast(data, ctypes.POINTER(ul))[0] if n.value and rfmt.value == 32 else None
    finally:
        x.XFree(data)


def find_display(x, any_display):
    """Gamescope's main Xwayland display (GAMESCOPE_XWAYLAND_SERVER_ID = 0)."""
    for sock in sorted(glob.glob("/tmp/.X11-unix/X*")):
        name = ":" + os.path.basename(sock)[1:]
        dpy = x.XOpenDisplay(name.encode())
        if not dpy:
            continue
        if root_cardinal(x, dpy, b"GAMESCOPE_XWAYLAND_SERVER_ID") == 0:
            return name, dpy
        x.XCloseDisplay(dpy)
    if any_display:  # testing outside Gaming Mode
        name = os.environ.get("DISPLAY", ":0")
        dpy = x.XOpenDisplay(name.encode())
        if dpy:
            return name, dpy
    return None, None


# --- drawing: anti-aliased shapes as signed distances, premultiplied BGRA -----
def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def coverage_maps(size):
    """Per-pixel coverage (0..1) of each shape, in a [-1, 1] box (y down)."""
    px = 2.0 / size

    def cov(d):
        return max(0.0, min(1.0, 0.5 - d / px))

    names = ["disc", "mic", "check", "cross", "dot0", "dot1", "dot2"] + [f"ring{i}" for i in range(TALK_FRAMES)]
    maps = {n: [0.0] * (size * size) for n in names}
    for j in range(size):
        v = (j + 0.5) * px - 1
        for i in range(size):
            u = (i + 0.5) * px - 1
            k = j * size + i
            r = math.hypot(u, v)
            maps["disc"][k] = cov(r - 0.70)
            for f in range(TALK_FRAMES):  # pulse ring growing from the disc and fading
                rr = 0.70 + 0.27 * f / TALK_FRAMES
                maps[f"ring{f}"][k] = cov(abs(r - rr) - 0.035) * (1 - f / TALK_FRAMES)
            head = seg_dist(u, v, 0, -0.30, 0, -0.04) - 0.14
            if v >= -0.06:
                arc = abs(math.hypot(u, v + 0.06) - 0.26) - 0.04
            else:
                arc = min(math.hypot(u - 0.26, v + 0.06), math.hypot(u + 0.26, v + 0.06)) - 0.04
            stem = seg_dist(u, v, 0, 0.20, 0, 0.36) - 0.04
            base = seg_dist(u, v, -0.15, 0.38, 0.15, 0.38) - 0.04
            maps["mic"][k] = cov(min(head, arc, stem, base))
            maps["check"][k] = cov(min(seg_dist(u, v, -0.30, 0.02, -0.09, 0.24),
                                       seg_dist(u, v, -0.09, 0.24, 0.32, -0.22)) - 0.075)
            maps["cross"][k] = cov(min(seg_dist(u, v, -0.25, -0.25, 0.25, 0.25),
                                       seg_dist(u, v, -0.25, 0.25, 0.25, -0.25)) - 0.075)
            for d, cx in enumerate((-0.30, 0.0, 0.30)):
                maps[f"dot{d}"][k] = cov(math.hypot(u - cx, v) - 0.09)
    return maps


def compose(size, layers):
    """layers: (coverage map, (r, g, b), alpha) painted bottom to top. Returns BGRA bytes."""
    out = bytearray(size * size * 4)
    for k in range(size * size):
        r = g = b = a = 0.0
        for m, (cr, cg, cb), alpha in layers:
            s = m[k] * alpha
            if s:
                r, g, b, a = cr * s + r * (1 - s), cg * s + g * (1 - s), cb * s + b * (1 - s), s + a * (1 - s)
        if a:
            o = k * 4
            out[o], out[o + 1], out[o + 2], out[o + 3] = int(b * 255), int(g * 255), int(r * 255), int(a * 255)
    return bytes(out)


def render_frames(size):
    m = coverage_maps(size)
    white = (1.0, 1.0, 1.0)
    frames = {}
    frames["wait"] = [compose(size, [(m["disc"], COLORS["wait"], 0.92), (m["mic"], white, 1.0)])]
    frames["talk"] = [compose(size, [(m[f"ring{f}"], COLORS["talk"], 0.9), (m["disc"], COLORS["talk"], 0.95),
                                     (m["mic"], white, 1.0)]) for f in range(TALK_FRAMES)]
    frames["busy"] = []
    for f in range(BUSY_FRAMES):
        dots = [(m[f"dot{d}"], white, 1.0 if (f // 2) % 3 == d else 0.4) for d in range(3)]
        frames["busy"].append(compose(size, [(m["disc"], COLORS["busy"], 0.92)] + dots))
    frames["done"] = [compose(size, [(m["disc"], COLORS["done"], 0.95), (m["check"], white, 1.0)])]
    frames["fail"] = [compose(size, [(m["disc"], COLORS["fail"], 0.95), (m["cross"], white, 1.0)])]
    return frames


class Overlay:
    def __init__(self, x, xext, dpy, test_size=None):
        self.x, self.dpy = x, dpy
        x.XSetErrorHandler(on_x_error)
        screen = x.XDefaultScreen(dpy)
        self.screen = screen
        self.root = x.XRootWindow(dpy, screen)
        self.w, self.h = x.XDisplayWidth(dpy, screen), x.XDisplayHeight(dpy, screen)
        if test_size:  # testing on a normal desktop: a small window instead of full screen
            self.w, self.h = test_size
        vinfo = XVisualInfo()
        if not x.XMatchVisualInfo(dpy, screen, 32, 4, ctypes.byref(vinfo)):  # 32-bit TrueColor
            raise RuntimeError("no 32-bit transparent visual on this display")
        self.visual = vinfo.visual
        attrs = XSetWindowAttributes()
        attrs.colormap = x.XCreateColormap(dpy, self.root, self.visual, 0)
        attrs.background_pixel = 0  # fully transparent
        attrs.border_pixel = 0
        mask = (1 << 1) | (1 << 3) | (1 << 13)  # CWBackPixel | CWBorderPixel | CWColormap
        # Full-screen like other Gamescope overlays; only the symbol is ever drawn.
        self.win = x.XCreateWindow(dpy, self.root, 0, 0, self.w, self.h, 0, 32, 1, self.visual, mask,
                                   ctypes.byref(attrs))
        x.XStoreName(dpy, self.win, b"Rootless Dictation recording symbol")
        self.opacity_atom = x.XInternAtom(dpy, b"_NET_WM_WINDOW_OPACITY", False)
        self._cardinal(x.XInternAtom(dpy, b"GAMESCOPE_EXTERNAL_OVERLAY", False), 1)
        self._cardinal(self.opacity_atom, 0)  # invisible until the first state
        self.input_shape = False
        if xext is not None:  # empty input region: every touch and click goes to the game
            xext.XShapeCombineRectangles(dpy, self.win, 2, 0, 0, None, 0, 0, 0)  # ShapeInput, ShapeSet
            self.input_shape = True
        self.gc = x.XCreateGC(dpy, self.win, 0, None)
        self.size = max(48, int(self.h * 0.12))
        self.frames = render_frames(self.size)
        self.buffers = {k: [ctypes.create_string_buffer(f, len(f)) for f in v] for k, v in self.frames.items()}
        self.images = {k: [self._image(b) for b in v] for k, v in self.buffers.items()}
        self.blank_buffer = ctypes.create_string_buffer(self.size * self.size * 4)  # all zero: transparent
        self.blank = self._image(self.blank_buffer)
        self.visible, self.clear_since = False, None
        x.XMapRaised(dpy, self.win)  # stays mapped; hidden by opacity (see show())
        x.XSync(dpy, False)
        if X_ERRORS:
            raise RuntimeError(f"X error {X_ERRORS[0]} while creating the window")

    def _image(self, buf):
        return self.x.XCreateImage(self.dpy, self.visual, 32, 2, 0, ctypes.cast(buf, vp), self.size, self.size, 32, 0)

    def _cardinal(self, atom, value):
        data = (ctypes.c_long * 1)(value)
        self.x.XChangeProperty(self.dpy, self.win, atom, 6, 32, 0, data, 1)  # XA_CARDINAL, 32-bit, PropModeReplace

    def _put(self, image):
        left, top = (self.w - self.size) // 2, int(self.h * 0.05)
        self.x.XPutImage(self.dpy, self.win, self.gc, image, 0, 0, left, top, self.size, self.size)

    def show(self, state, tick):
        """Called every frame with the current state."""
        x = self.x
        if state == "hide":
            if not self.visible:
                return
            now = time.monotonic()
            self.clear_since = self.clear_since or now
            if now - self.clear_since < CLEAR_TIME:
                self._put(self.blank)  # empty picture, while Gamescope still draws the window
            else:
                self._cardinal(self.opacity_atom, 0)
                self.visible, self.clear_since = False, None
            x.XFlush(self.dpy)
            return
        self.clear_since = None
        if not self.visible:
            # Unmap and map again (while still invisible), so Gamescope looks at
            # its windows again and takes this one as the overlay. Changing only
            # the opacity was not enough for the first symbols after the window
            # was created (Deck test of 0.3.2).
            x.XUnmapWindow(self.dpy, self.win)
            self._cardinal(self.opacity_atom, OPAQUE)
            x.XMapRaised(self.dpy, self.win)
            self.visible = True
        imgs = self.images[state]
        self._put(imgs[tick % len(imgs)])  # every frame, also for still pictures
        x.XFlush(self.dpy)

    def hide_now(self):
        """Hide completely before exiting (the check, or the end of input)."""
        end = time.monotonic() + CLEAR_TIME + 0.2
        while self.visible and time.monotonic() < end:
            self.show("hide", 0)
            time.sleep(FRAME)

    def close(self):
        self.x.XDestroyWindow(self.dpy, self.win)
        self.x.XCloseDisplay(self.dpy)


def drain_x(x, dpy, event):
    """Read what the X server sent. If the display has closed (Gaming Mode
    restarted, or the Deck switched to Desktop Mode), Xlib reports a fatal IO
    error here and ends this process, so the engine starts a new one at once,
    not at the next button press (Deck test of 0.4.1)."""
    while x.XPending(dpy):
        x.XNextEvent(dpy, event)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="show the symbol for 1.5 s, report, exit")
    ap.add_argument("--any-display", action="store_true", help="testing: allow a non-Gamescope display")
    ap.add_argument("--test-size", help="testing with --any-display: window WxH instead of full screen")
    args = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("Refusing to run as root.")
    try:
        x, xext = load_x()
    except OSError as e:
        print(f"UNAVAILABLE: cannot load libX11: {e}", flush=True)
        return 2
    name, dpy = find_display(x, args.any_display)
    if not dpy:
        print("UNAVAILABLE: no Gaming Mode (Gamescope) display found", flush=True)
        return 2
    t0 = time.monotonic()
    try:
        size = tuple(int(n) for n in args.test_size.split("x")) if (args.any_display and args.test_size) else None
        ov = Overlay(x, xext, dpy, size)
    except RuntimeError as e:
        print(f"UNAVAILABLE: {e}", flush=True)
        return 2
    print(f"READY: display {name}, screen {ov.w}x{ov.h}, symbol {ov.size} px, "
          f"input pass-through {'on' if ov.input_shape else 'not available'}, "
          f"drawn in {time.monotonic() - t0:.1f} s", flush=True)
    if args.check:
        end = time.monotonic() + 1.5
        tick = 0
        while time.monotonic() < end:
            ov.show("talk", tick)
            tick += 1
            time.sleep(FRAME)
        ov.hide_now()
        x.XSync(ov.dpy, False)
        print("OK" if not X_ERRORS else f"X errors: {X_ERRORS[:3]}", flush=True)
        ov.close()
        return 0 if not X_ERRORS else 1

    state, since, tick, buf = "hide", time.monotonic(), 0, b""
    fd, xfd = sys.stdin.fileno(), x.XConnectionNumber(ov.dpy)
    event = ctypes.create_string_buffer(256)  # an XEvent is 192 bytes on 64-bit
    while True:
        r, _, _ = select.select([fd, xfd], [], [], FRAME)
        if xfd in r:  # also while hidden, when nothing else talks to the display
            drain_x(x, ov.dpy, event)
        if fd in r:
            data = os.read(fd, 256)
            if not data:
                break  # the engine is gone
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                word = line.decode("ascii", "replace").strip()
                if word in STATES and word != state:
                    state, since, tick = word, time.monotonic(), 0
        if state in AUTO_HIDE and time.monotonic() - since > AUTO_HIDE[state]:
            state = "hide"
        ov.show(state, tick)
        tick += 1
        if X_ERRORS:
            print(f"X errors: {X_ERRORS[:3]}", flush=True)
            X_ERRORS.clear()
    ov.hide_now()
    ov.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
