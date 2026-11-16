#!/usr/bin/env python3
"""Decktation Rootless - Phase 1, probe B1: read Deck buttons as `deck`.

Run on the Steam Deck as the normal `deck` user, in Gaming Mode, with
WoW Forever focused:

    /usr/bin/python3 -I hid_ptt_probe.py                 # combo R1+R2, 60 s
    /usr/bin/python3 -I hid_ptt_probe.py --combo L4      # a back grip
    /usr/bin/python3 -I hid_ptt_probe.py --combo L1+R1   # upstream default

It finds the Deck's built-in controller hidraw node, opens it for reading
only, and prints every button change plus COMBO DOWN / COMBO UP. It never
writes to the device, never grabs it, and never changes permissions. If
the node is not readable by this user it reports a blocker and exits.

Reading is passive: Steam and the game still receive every button. A combo
of buttons the game uses (L1/R1/R2...) will also trigger those actions.

The button bit table is copied from Decktation's backend/src/deck_hid.py
(Copyright (c) 2026 silverfoxy, MIT License), which mirrors the
ID_CONTROLLER_DECK_STATE layout in Linux drivers/hid/hid-steam.c.
"""
import argparse
import errno
import os
import select
import sys
import time

DECK_HID_ID = "0003:000028DE:00001205"
REPORT_SIZE = 64
DECK_REPORT_TYPE = 9

# button -> (byte offset, bit) ; from upstream deck_hid.STEAM_DECK_BUTTON_BITS
BUTTON_BITS = {
    "View": (9, 4), "Menu": (9, 6), "L3": (10, 6), "R3": (11, 2),
    "D-pad Up": (9, 0), "D-pad Right": (9, 1), "D-pad Left": (9, 2), "D-pad Down": (9, 3),
    "Left Trackpad": (10, 1), "Right Trackpad": (10, 2),
    "R2": (8, 0), "L2": (8, 1), "R1": (8, 2), "L1": (8, 3),
    "Y": (8, 4), "B": (8, 5), "X": (8, 6), "A": (8, 7),
    "L5": (9, 7), "R5": (10, 0), "L4": (13, 1), "R4": (13, 2),
}
# Half-pull counts as pressed, as upstream does (16-bit analog trigger values).
TRIGGER_OFFSETS = {"L2": 44, "R2": 46}
TRIGGER_THRESHOLD = 16384


def decode(report):
    if len(report) != REPORT_SIZE or report[0] != 1 or report[1] != 0 or report[2] != DECK_REPORT_TYPE:
        return None
    states = {name: bool(report[b] & (1 << bit)) for name, (b, bit) in BUTTON_BITS.items()}
    for name, off in TRIGGER_OFFSETS.items():
        states[name] |= int.from_bytes(report[off:off + 2], "little") >= TRIGGER_THRESHOLD
    return states


def find_deck_nodes():
    found = []
    try:
        names = sorted(os.listdir("/sys/class/hidraw"))
    except OSError:
        names = []
    for name in names:
        try:
            with open(f"/sys/class/hidraw/{name}/device/uevent") as f:
                props = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        except OSError:
            continue
        if props.get("HID_ID", "").upper() == DECK_HID_ID and props.get("HID_PHYS", "").endswith("/input2"):
            found.append((f"/dev/{name}", props.get("HID_NAME", "?"), props["HID_PHYS"]))
    return found


def wait_for_report(fd, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, REPORT_SIZE)
            except BlockingIOError:
                continue
            if decode(data) is not None:
                return True
    return False


def open_node(path):
    """Read-only first; hid-steam may only deliver reports to read/write openers."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return None, None, e
    if wait_for_report(fd, 3.0):
        return fd, "O_RDONLY", None
    os.close(fd)
    print("  no reports with O_RDONLY within 3 s; retrying O_RDWR (still never writes)")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        return None, None, e
    if wait_for_report(fd, 3.0):
        return fd, "O_RDWR", None
    os.close(fd)
    return None, None, OSError(errno.ENODATA, "opened, but no controller reports arrived")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--combo", default="R1+R2",
                    help="buttons joined by '+', e.g. R1+R2, L1+R1, L4, L4+R4 (default R1+R2)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--all", action="store_true", help="print every button change, not just combo buttons")
    args = ap.parse_args()

    if os.geteuid() == 0:
        sys.exit("Refusing to run as root. Run this as the normal deck user.")
    combo = [b.strip() for b in args.combo.split("+") if b.strip()]
    bad = [b for b in combo if b not in BUTTON_BITS]
    if bad:
        sys.exit(f"Unknown button(s) {bad}. Choose from: {', '.join(BUTTON_BITS)}")

    print(f"uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()}")
    nodes = find_deck_nodes()
    if not nodes:
        print("BLOCKER: no Steam Deck controller hidraw node (28DE:1205 .../input2) found.")
        return 2
    for path, name, phys in nodes:
        print(f"candidate {path}: {name} ({phys}) read={os.access(path, os.R_OK)} write={os.access(path, os.W_OK)}")

    path = nodes[0][0]
    fd, mode, err = open_node(path)
    if fd is None:
        kind = "permission denied" if isinstance(err, PermissionError) else "open/read failed"
        print(f"BLOCKER: {kind} on {path}: {err}. Not changing any permissions.")
        return 2
    print(f"OK: reading {path} as uid {os.getuid()} with {mode}")
    print(f"Watching combo {'+'.join(combo)} for {args.seconds:.0f} s. Press it a few times; "
          f"also try it while WoW Forever's chat box and menus are open.")

    prev, combo_down, combo_count, reports = {}, False, 0, 0
    t0 = time.monotonic()
    end = t0 + args.seconds
    try:
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                continue
            try:
                data = os.read(fd, REPORT_SIZE)
            except BlockingIOError:
                continue
            if not data:
                print("controller disconnected"); break
            states = decode(data)
            if states is None:
                continue
            reports += 1
            now = time.monotonic() - t0
            for name, down in states.items():
                if prev.get(name, False) != down and (args.all or name in combo):
                    print(f"{now:8.3f}s  {name:14s} {'down' if down else 'up'}")
            prev = states
            held = all(states[b] for b in combo)
            if held and not combo_down:
                combo_count += 1
                print(f"{now:8.3f}s  >>> COMBO DOWN #{combo_count} ({'+'.join(combo)})")
            elif combo_down and not held:
                print(f"{now:8.3f}s  <<< COMBO UP")
            combo_down = held
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)
    elapsed = max(time.monotonic() - t0, 0.001)
    print(f"SUMMARY: mode={mode} reports={reports} (~{reports / elapsed:.0f}/s) combo_presses={combo_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
