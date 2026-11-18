"""Rootless Dictation - Decky backend (runs as the normal user: plugin.json "flags": []).

Starts the push-to-talk engine (engine/ptt_pipeline.py) as a child process in
its own session, reads its "@@ {json}" state lines, and serves a small status
object to the panel. Settings are saved in Decky's settings folder for this
plugin. Python standard library only; nothing here needs or asks for root.
"""
import asyncio
import collections
import hashlib
import json
import os
import re
import signal
import subprocess
import time

try:
    import decky  # provided by Decky Loader at run time
    logger = decky.logger
except ImportError:  # unit tests outside Decky
    import logging
    logger = logging.getLogger("rootless-dictation")

PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR") or os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")
SETTINGS_DIR = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") or os.path.join(HOME, ".config", "rootless-dictation")
RUNTIME_DIR = os.environ.get("DECKY_PLUGIN_RUNTIME_DIR") or os.path.join(HOME, ".local", "share", "rootless-dictation")
LOG_DIR = os.environ.get("DECKY_PLUGIN_LOG_DIR")
DECKY_HOME = os.environ.get("DECKY_HOME")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
ENGINE = os.path.join(PLUGIN_DIR, "engine", "ptt_pipeline.py")
WHISPER = os.path.join(PLUGIN_DIR, "bin", "whisper-cli")
VAD_MODEL = os.path.join(PLUGIN_DIR, "models", "ggml-silero-v6.2.0.bin")  # voice detection, shipped in the ZIP
# The speech model ships in the ZIP too (no download code at all); it must match its pinned SHA-256.
MODEL_CANDIDATES = [os.path.join(PLUGIN_DIR, "models", "ggml-base.bin")]
PINNED_SHA256 = {
    "whisper-cli": "36d865e9bc737838df027a3dc30760d3d6f6bf5dab220a214407e75d905d60bd",
    "ggml-base.bin": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    "ggml-silero-v6.2.0.bin": "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
}
# Text is typed only while a game is in front: any app except Steam's own interface.
EXPECT_APP = "game"
SYSTEM_PYTHON = "/usr/bin/python3"
PACTL = "/usr/bin/pactl"
# The engine writes the name of the output it muted here and removes it after
# unmuting; if it is still there after the engine stops, the backend unmutes.
MUTE_MARKER = os.path.join(RUNTIME_DIR, "muted-output")
SINK_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
LANGS = ("en", "es", "auto")
DEFAULTS = {"enabled": False, "lang": "en", "send": False, "close_chat": True, "mute": True, "indicator": True,
            "combo": ["R1", "R2"]}
BOOL_SETTINGS = ("send", "close_chat", "mute", "indicator")
# Button names the engine understands (engine/hid_ptt_probe.py BUTTON_BITS).
BUTTON_NAMES = ("View", "Menu", "L3", "R3", "D-pad Up", "D-pad Right", "D-pad Left", "D-pad Down",
                "Left Trackpad", "Right Trackpad", "R2", "L2", "R1", "L1", "Y", "B", "X", "A",
                "L5", "R5", "L4", "R4")
BACK_BUTTONS = ("L4", "R4", "L5", "R5")
CAPTURE_SECONDS = 10
OVERLAY = os.path.join(PLUGIN_DIR, "engine", "overlay.py")
LOG_LINES = 200
# Lines always worth keeping in the log file, even with the detailed log off.
IMPORTANT = ("blocker", "error", "starting engine", "engine exited", "could not", "stopped unexpectedly", "not found")


def build_channel():
    """'dev' (test builds) or 'release', written into the ZIP by build_zip.py.
    Missing or unreadable means release: the quiet, minimal-logging behaviour."""
    try:
        with open(os.path.join(PLUGIN_DIR, "build.json"), encoding="utf-8") as f:
            return "dev" if json.load(f).get("channel") == "dev" else "release"
    except (OSError, ValueError, AttributeError):
        return "release"


# Test builds write every engine step, including what was said, to the Decky
# log file (a switch in the panel, on by default). Release builds hide the
# switch and keep only errors and start/stop lines, so logs stay tiny.
RELEASE = build_channel() == "release"
STOP_GRACE = 3.0

# Children get a minimal, explicit environment. Decky's frozen Python sets
# LD_LIBRARY_PATH to its own bundle, which must not leak into /usr/bin/python3.
CHILD_ENV = {"PATH": "/usr/bin:/bin", "HOME": HOME, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
# pactl needs the user's runtime folder to find their own sound session.
SOUND_ENV = dict(CHILD_ENV, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS, detailed_log=not RELEASE)
    if data.get("lang") in LANGS:
        out["lang"] = data["lang"]
    if not check_combo(data.get("combo")):
        out["combo"] = list(data["combo"])
    for key in ("enabled", "detailed_log") + BOOL_SETTINGS:
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if RELEASE:
        out["detailed_log"] = False
    return out


def check_combo(buttons):
    """'' if these buttons can be used for push-to-talk, otherwise why not."""
    if not isinstance(buttons, list) or not buttons or not all(isinstance(b, str) for b in buttons):
        return "no buttons were chosen"
    if any(b not in BUTTON_NAMES for b in buttons) or len(set(buttons)) != len(buttons):
        return "unknown button"
    if len(buttons) > 3:
        return "use at most 3 buttons"
    if "Menu" in buttons or "View" in buttons:
        return "the Menu and View buttons open menus in games; choose other buttons"
    if len(buttons) == 1 and buttons[0] not in BACK_BUTTONS:
        return (f"{buttons[0]} alone is used all the time while playing; use a back button "
                "(L4, R4, L5, R5) or two buttons together")
    return ""


def new_capture(state, message=""):
    """Panel state for choosing buttons. While waiting: held = the buttons that
    would be saved, ready = held long enough to save on release, why = why they
    cannot be used ('' if they can)."""
    return {"state": state, "message": message, "held": [], "ready": False, "why": ""}


def save_settings(settings):
    os.makedirs(SETTINGS_DIR, mode=0o700, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def remove_own_logs():
    """Delete this plugin's Decky log files on uninstall. Touches only *.log files
    directly inside <DECKY_HOME>/logs/<this plugin's folder>, then that folder if
    empty. Settings and data are kept: Decky also runs uninstall before every
    update, so deleting them would reset the user's settings on each update."""
    if not (DECKY_HOME and LOG_DIR):
        return 0
    expected = os.path.join(os.path.realpath(DECKY_HOME), "logs", os.path.basename(os.path.realpath(PLUGIN_DIR)))
    if os.path.islink(LOG_DIR) or os.path.realpath(LOG_DIR) != expected:
        logger.warning("not removing logs: unexpected log folder %s", LOG_DIR)
        return 0
    removed = 0
    for entry in os.scandir(LOG_DIR):
        if entry.name.endswith(".log") and entry.is_file(follow_symlinks=False):
            os.unlink(entry.path)
            removed += 1
    try:
        os.rmdir(LOG_DIR)
    except OSError:
        pass
    return removed


def pactl(*args):
    """Run pactl in the user's own sound session. Returns (stdout or None, error)."""
    try:
        r = subprocess.run([PACTL, *args], env=SOUND_ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}")[-200:]
    return r.stdout.decode("utf-8", "replace").strip(), ""


def restore_sound():
    """Unmute the output the engine muted if the engine could not do it itself
    (crash, forced stop, power loss). Returns a message, or "" if nothing to do."""
    try:
        with open(MUTE_MARKER, encoding="utf-8") as f:
            sink = f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        return f"could not read {MUTE_MARKER}: {e}"
    ok = bool(SINK_NAME.match(sink))
    if ok:
        out, err = pactl("set-sink-mute", sink, "0")
        if out is None:
            return f"could not restore game sound: {err}"
    try:
        os.unlink(MUTE_MARKER)
    except OSError:
        pass
    return "game sound was left muted; unmuted it" if ok else "removed an unreadable mute marker"


def find_model():
    return next((p for p in MODEL_CANDIDATES if os.path.isfile(p)), None)


def deck_hidraw_nodes():
    found = []
    try:
        names = sorted(os.listdir("/sys/class/hidraw"))
    except OSError:
        return found
    for name in names:
        try:
            with open(f"/sys/class/hidraw/{name}/device/uevent") as f:
                props = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
        except OSError:
            continue
        if props.get("HID_ID", "").upper() == "0003:000028DE:00001205" and props.get("HID_PHYS", "").endswith("/input2"):
            found.append(f"/dev/{name}")
    return found


def proc_status():
    out = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in ("Groups", "CapEff", "NoNewPrivs"):
                    out[key] = " ".join(value.split())
    except OSError:
        pass
    return out


class Plugin:
    def __init__(self):
        self.settings = load_settings()
        self.proc = None
        self.reader = None
        self.phase = "off"
        self.error = ""
        self.last = None
        self.log = collections.deque(maxlen=LOG_LINES)
        self.lock = asyncio.Lock()
        self.capture = new_capture("idle")
        self.capture_until = 0.0  # time.monotonic() when choosing buttons ends
        self.capture_task = None
        self.capture_proc = None
        self.unloading = False

    # --- Decky lifecycle ----------------------------------------------------
    async def _main(self):
        logger.info("Rootless Dictation backend started as uid=%s", os.getuid())
        await self._restore_sound()
        if self.settings["enabled"]:
            await self._start()

    async def _unload(self):
        self.unloading = True
        if self.capture_proc is not None and self.capture_proc.returncode is None:
            self._signal(self.capture_proc, signal.SIGKILL)
        if self.capture_task is not None:
            try:
                await asyncio.wait_for(self.capture_task, STOP_GRACE)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.capture_task.cancel()
        await self._stop()

    async def _uninstall(self):
        await self._stop()
        try:
            remove_own_logs()
        except OSError as e:
            logger.warning("could not remove logs: %s", e)

    # --- panel API ------------------------------------------------------------
    async def status(self):
        return {
            "phase": self.phase,
            "enabled": self.settings["enabled"],
            "settings": {"lang": self.settings["lang"], "send": self.settings["send"],
                         "close_chat": self.settings["close_chat"],
                         "mute": self.settings["mute"], "indicator": self.settings["indicator"],
                         "combo": list(self.settings["combo"]), "detailed_log": self.settings["detailed_log"]},
            "capture": dict(self.capture, left=max(0, round(self.capture_until - time.monotonic()))
                            if self.capture["state"] == "waiting" else 0),
            "channel": "release" if RELEASE else "dev",
            "error": self.error,
            "last": self.last,
            "log": list(self.log)[-10:],
        }

    async def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return "bad value"
        async with self.lock:
            self.settings["enabled"] = enabled
            self._save()
            if enabled:
                await self._start()
            else:
                await self._stop()
        return "ok"

    async def set_setting(self, name, value):
        if name == "lang" and value in LANGS:
            pass
        elif name in BOOL_SETTINGS and isinstance(value, bool):
            pass
        elif name == "combo":
            why = check_combo(value)
            if why:
                return f"Cannot use these buttons: {why}"
            value = list(value)
        elif name == "detailed_log" and isinstance(value, bool) and not RELEASE:
            pass
        else:
            return f"bad setting {name}={value!r}"
        async with self.lock:
            self.settings[name] = value
            self._save()
            if name == "detailed_log":  # only changes what is written to the log file
                return "ok"
            if self.settings["enabled"]:  # restart the engine with the new setting
                await self._stop()
                await self._start()
        return "ok"

    async def capture_buttons(self):
        """Choose new push-to-talk buttons: the next buttons the user holds."""
        if self.capture_task is not None and not self.capture_task.done():
            return "busy"
        self.capture = new_capture("waiting")
        self.capture_until = time.monotonic() + CAPTURE_SECONDS
        self.capture_task = asyncio.get_event_loop().create_task(self._capture())
        return "ok"

    async def _capture(self):
        async with self.lock:
            await self._stop()  # nothing is recorded while choosing
            self.phase = "choosing"
            try:
                result = await self._run_capture()
            except Exception as e:  # report, never crash the plugin
                result = f"could not read the buttons: {e!r}"
            if isinstance(result, list):
                why = check_combo(result)
                if why:
                    self.capture = new_capture("failed", f"{'+'.join(result)}: {why}.")
                else:
                    self.settings["combo"] = result
                    self._save()
                    self.capture = new_capture("done", f"Push-to-talk is now {'+'.join(result)}.")
            else:
                self.capture = new_capture("failed", result)
            self._log(f"choosing buttons: {self.capture['message']}")
            self.phase = "off"
            if self.settings["enabled"] and not self.unloading:
                await self._start()

    async def _run_capture(self):
        proc = await asyncio.create_subprocess_exec(
            SYSTEM_PYTHON, "-I", ENGINE, "--capture", str(CAPTURE_SECONDS),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=CHILD_ENV, cwd="/", start_new_session=True)
        self.capture_proc = proc
        result = "No buttons were held in time."
        try:
            async def read():
                nonlocal result
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("@@ "):
                        self._log(line)
                        continue
                    try:
                        ev = json.loads(line[3:])
                    except ValueError:
                        continue
                    if ev.get("event") == "capture_waiting":  # the engine's own 10 s start now
                        self.capture_until = time.monotonic() + CAPTURE_SECONDS
                    elif ev.get("event") == "capture_held" and isinstance(ev.get("buttons"), list):
                        held = [b for b in ev["buttons"] if b in BUTTON_NAMES]
                        self.capture.update(held=held, ready=bool(ev.get("ready")) and bool(held),
                                            why=check_combo(held) if held else "")
                    elif ev.get("event") == "captured" and isinstance(ev.get("buttons"), list):
                        result = [str(b) for b in ev["buttons"]]
                    elif ev.get("event") == "capture_failed":
                        result = str(ev.get("reason", "failed")) + "."
            await asyncio.wait_for(read(), CAPTURE_SECONDS + 5)
        except asyncio.TimeoutError:
            pass
        finally:
            if proc.returncode is None:
                self._signal(proc, signal.SIGKILL)
            await proc.wait()
            self.capture_proc = None
        return result

    async def self_test(self):
        """Checks a new user can read: each one is [label, ok, detail]."""
        return await asyncio.to_thread(self._self_test)

    # --- engine process -------------------------------------------------------
    def _save(self):
        try:
            save_settings(self.settings)
        except OSError as e:
            self._log(f"could not save settings: {e}")

    def _log(self, line, routine=False):
        """Always shown in the panel (memory only). Written to the Decky log file
        when the detailed log is on, or when the line reports a problem.
        `routine` lines (what was said, each step) are never kept otherwise."""
        self.log.append(line)
        if self.settings["detailed_log"] or (not routine and any(k in line.lower() for k in IMPORTANT)):
            logger.info("engine: %s", line)

    def _running(self):
        return self.proc is not None and self.proc.returncode is None

    async def _start(self):
        if self._running():
            return
        model = find_model()
        if model is None:
            self.phase, self.error = "error", "Speech model not found (expected ggml-base.bin; see self-test)"
            self._log(self.error)
            return
        args = [SYSTEM_PYTHON, "-I", ENGINE, "--whisper", WHISPER, "--model", model, "--vad-model", VAD_MODEL,
                "--lang", self.settings["lang"], "--expect-app", EXPECT_APP, "--seconds", "0",
                "--state-dir", os.path.dirname(MUTE_MARKER), "--combo", "+".join(self.settings["combo"])]
        if self.settings["send"]:
            args.append("--send")
            if self.settings["close_chat"]:
                args.append("--close-chat")
        if self.settings["mute"]:
            args.append("--mute")
        if self.settings["indicator"]:
            args.append("--indicator")
        self.phase, self.error = "starting", ""
        self._log(f"starting engine: {'+'.join(self.settings['combo'])}, language {self.settings['lang']}, "
                  f"{'leave unsent' if not self.settings['send'] else 'send automatically, then close chat' if self.settings['close_chat'] else 'send automatically'}, "
                  f"mute while talking {'on' if self.settings['mute'] else 'off'}, "
                  f"recording symbol {'on' if self.settings['indicator'] else 'off'}")
        try:
            # Own session = own process group, so stopping also ends pw-record,
            # whisper-cli and xtest_type.py started by the engine.
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=CHILD_ENV, cwd="/", start_new_session=True)
        except OSError as e:
            self.phase, self.error = "error", f"could not start the engine: {e}"
            self._log(self.error)
            return
        self.reader = asyncio.get_event_loop().create_task(self._read(self.proc))

    async def _stop(self):
        proc = self.proc
        if proc is None:
            self.phase = "off"
            return
        if proc.returncode is None:
            self._signal(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
            except asyncio.TimeoutError:
                self._signal(proc, signal.SIGKILL)
                await proc.wait()
        if self.reader is not None:
            try:
                await asyncio.wait_for(self.reader, STOP_GRACE)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.reader.cancel()
        self.proc, self.reader = None, None
        self.phase, self.error = "off", ""
        await self._restore_sound()

    async def _restore_sound(self):
        try:
            msg = await asyncio.to_thread(restore_sound)
        except Exception as e:  # never let this break stopping or loading
            msg = f"could not restore game sound: {e!r}"
        if msg:
            self._log(msg)

    @staticmethod
    def _signal(proc, sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _read(self, proc):
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("@@ "):
                    try:
                        self._on_event(json.loads(line[3:]))
                    except ValueError:
                        self._log(line)
                elif line.startswith("%% "):  # contains what the user said
                    public, _, private = line[3:].partition("\t")
                    self._log(f"{public}: {private}", routine=True)
                else:
                    self._log(line)
            code = await proc.wait()
        except Exception as e:  # report, never crash the plugin
            self._log(f"engine reader error: {e!r}")
            code = None
        if proc is self.proc:  # not replaced by a restart
            stopped_by_us = code in (-signal.SIGTERM, -signal.SIGKILL, 143)
            if self.phase != "error":
                self.phase = "off" if stopped_by_us else "error"
            if not stopped_by_us and not self.error:
                self.error = f"Engine stopped unexpectedly (exit {code})"
            self._log(f"engine exited ({code})")
            if not stopped_by_us:  # a crash may have left the game sound muted
                await self._restore_sound()

    def _on_event(self, ev):
        kind = ev.get("event")
        if kind == "ready":
            self.phase = "listening"
        elif kind == "recording":
            self.phase = "recording"
        elif kind == "transcribing":
            self.phase = "transcribing"
        elif kind == "typing":
            self.phase = "typing"
        elif kind == "result":
            self.last = {"time": time.time(), "ok": bool(ev.get("ok")), "text": str(ev.get("text", "")),
                         "sent": bool(ev.get("sent")), "reason": str(ev.get("reason", "")),
                         "seconds": ev.get("seconds")}
        elif kind == "skipped":
            if not ev.get("quiet"):
                self.last = {"time": time.time(), "ok": False, "text": "", "sent": False,
                             "reason": str(ev.get("reason", "")), "seconds": None}
        elif kind == "error":
            self.phase, self.error = "error", str(ev.get("message", "error"))

    # --- self-test --------------------------------------------------------------
    def _self_test(self):
        checks = []

        def add(label, ok, detail):
            checks.append([label, bool(ok), detail])

        st = proc_status()
        add("Runs without root", os.geteuid() != 0,
            f"uid {os.getuid()}, groups {st.get('Groups') or 'none'}, CapEff {st.get('CapEff', '?')}")
        ok = os.path.isfile(WHISPER) and sha256(WHISPER) == PINNED_SHA256["whisper-cli"]
        add("Speech engine", ok and os.access(WHISPER, os.X_OK),
            "whisper-cli matches its pinned SHA-256" if ok else f"missing or changed: {WHISPER}")
        model = find_model()
        ok = model is not None and sha256(model) == PINNED_SHA256["ggml-base.bin"]
        add("Speech model", ok, f"{model} matches its pinned SHA-256" if ok
            else ("not found (it should be in the plugin's models folder; reinstall the plugin)" if model is None
                  else f"{model} does not match its pinned SHA-256"))
        ok = os.path.isfile(VAD_MODEL) and sha256(VAD_MODEL) == PINNED_SHA256["ggml-silero-v6.2.0.bin"]
        add("Voice detection model", ok, "ggml-silero-v6.2.0.bin matches its pinned SHA-256" if ok
            else f"missing or changed: {VAD_MODEL}")
        nodes = deck_hidraw_nodes()
        add("Controller", bool(nodes) and os.access(nodes[0], os.R_OK),
            f"{nodes[0]} readable" if nodes and os.access(nodes[0], os.R_OK)
            else (f"{nodes[0]} not readable; permissions are not changed" if nodes else "Steam Deck controller not found"))
        runtime = f"/run/user/{os.getuid()}"
        add("Audio session", os.path.isdir(runtime) and os.path.exists(os.path.join(runtime, "pipewire-0")),
            f"{runtime}/pipewire-0")
        add("Recorder", os.access("/usr/bin/pw-record", os.X_OK), "/usr/bin/pw-record")
        sink, err = pactl("get-default-sink")
        add("Sound control (for mute while talking)", bool(sink),
            f"default output: {sink}" if sink else f"pactl did not answer: {err}")
        try:
            r = subprocess.run([SYSTEM_PYTHON, "-I", OVERLAY, "--check"], env=CHILD_ENV, cwd="/",
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            out = r.stdout.decode("utf-8", "replace").strip().splitlines()
            ok, detail = r.returncode == 0, (out[0] if out else f"exit {r.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, detail = False, str(e)
        add("Recording symbol (shown at the top for a moment)", ok, detail[:200])
        add("Game display", os.path.exists("/tmp/.X11-unix/X1"), "/tmp/.X11-unix/X1")
        return checks
