#!/usr/bin/env python3
"""Rootless Dictation push-to-talk engine (started by the Decky backend, main.py).

Hold the combo (default R1+R2), speak, release. The engine records the
microphone with pw-record, transcribes it with whisper-cli (with voice detection, VAD_ARGS, so noise is
not turned into text, and with a short hint of the channel words, HINTS), and
pastes the text into the game's chat through xtest_type.py. It sends only with --send;
with --close-chat it then presses Esc to close a chat box that stays open.
With --mute it mutes the default sound output while the microphone is live,
so game sound does not reach the built-in microphone. With --indicator it
shows a recording symbol on screen (overlay.py). Everything runs as the
normal user:

    /usr/bin/python3 -I ptt_pipeline.py --whisper BIN --model MODEL --lang en --mute --indicator
    /usr/bin/python3 -I ptt_pipeline.py --whisper BIN --model MODEL --from-wav x.wav --dry-run
    /usr/bin/python3 -I ptt_pipeline.py --capture 10     # report the next buttons held, then exit

Output: human-readable log lines, plus one machine-readable line per state
change for the backend: "@@ {json}" (see emit()).

Safety:
- Refuses root. Sets NoNewPrivs first, so nothing it starts (pw-record,
  whisper-cli, xtest_type.py) can gain privileges through setuid or
  file-capability binaries.
- Runs whisper-cli and the models (speech, voice detection) only if their
  SHA-256 matches the pinned values.
- Reads the controller passively (O_RDONLY via hid_ptt_probe.py). Never writes
  to it or grabs it.
- Injects only while a game is in front, not Steam's own menus (or only the
  app given with --expect-app). Never starts a
  slash command from speech: the only slash it adds is the chat command for a
  channel word spoken first (KEYWORDS), such as "party ..." or "grupo ..."
  becoming "/p ...", or "whisper Name ..." becoming "/w Name ...".
- Records into $XDG_RUNTIME_DIR/rootless-dictation/ (RAM, private to this
  user) and deletes the recording as soon as it has been transcribed or
  skipped, and again when the engine stops. Nothing is written to disk.
- Muting uses pactl in the user's own sound session only. Before muting it
  writes the output's name to a marker file in --state-dir, so a mute left
  behind by a crash is undone at the next start (here and by main.py). It
  never unmutes an output that was already muted before it started.
"""
import argparse
import array
import ctypes
import hashlib
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I leaves the script's own directory off sys.path
import hid_ptt_probe as hid  # noqa: E402  (same folder, reviewed, read-only controller access)

PINNED_SHA256 = {
    "whisper-cli": "36d865e9bc737838df027a3dc30760d3d6f6bf5dab220a214407e75d905d60bd",
    "ggml-base.bin": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    "ggml-silero-v6.2.0.bin": "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
}
# First spoken word -> WoW chat command. Each language is separate; "auto" uses the
# detected language (both lists if none was detected). The words follow Decktation's
# English channel list and its Spanish example (LANGUAGE_CONFIG.md); "responder" is ours.
# The extra spellings are mishearings seen on the Deck and in tests: "party", "yell"
# and "hermandad". Only made-up words, never real words or names.
KEYWORDS = {
    "en": {"party": "/p", "parties": "/p", "parti": "/p", "party's": "/p", "raid": "/raid", "guild": "/g",
           "officer": "/o", "say": "/s", "yell": "/y", "yeal": "/y", "jeal": "/y", "geel": "/y",
           "instance": "/i", "whisper": "/w", "reply": "/r"},
    "es": {"grupo": "/p", "banda": "/raid", "hermandad": "/g", "oficial": "/o", "decir": "/s", "di": "/s",
           "gritar": "/y", "grita": "/y", "instancia": "/i", "susurrar": "/w", "susurra": "/w",
           "responder": "/r", "responde": "/r", "dí": "/s", "termandad": "/g"},
}
# Vocabulary hint (whisper-cli --prompt): the channel words, so the engine expects
# them. Tested on Deck recordings of one speaker: channel words 7 -> 13 of
# 14 in English and 7 -> 10 of 10 in Spanish, normal sentences unchanged. On noise
# a hint makes the engine invent text ("¡Adiós!"); voice detection stops that.
# No hint in "auto": a mixed English + Spanish hint made English worse.
HINTS = {"en": "Yell guild raid reply party officer say instance whisper",
         "es": "Grupo, banda, hermandad, oficial, di, decir, grita, gritar, instancia, susurra, susurrar, "
               "responde, responder."}
# Voice detection (Silero VAD, run by whisper-cli itself): audio without speech is
# not transcribed at all. 1 s of padding around the speech, and no splitting at
# short pauses, keep the audio the engine hears close to the full recording.
VAD_ARGS = ["-vp", "1000", "-vsd", "2000"]
VAD_FOUND = re.compile(r"whisper_vad: detected (\d+) speech segments")
# A transcript that is only one of these is the engine's output for noise, not speech.
NOISE_WORDS = {"the", "you"}
WHISPER_CMD = "/w"                # needs a player name: "whisper Name message"
WHISPER_FILLER = {"to", "a"}      # "whisper to Name ...", "susurra a Name ..."
WHISPER_INCOMPLETE = "but no player name and message after it"
EDGE_PUNCT = ".,:;!?¡¿\"'"
NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+")  # [BLANK_AUDIO], (música), *risas*
MAX_CHAT_BYTES = 255
MIN_HOLD, MAX_HOLD, TAIL = 0.35, 30.0, 0.50   # seconds; TAIL keeps recording after release
SPEECH_DBFS = -45.0                            # loudest 0.5 s window must exceed this
PW_RECORD = "/usr/bin/pw-record"
PACTL = "/usr/bin/pactl"
WAV_HEADER = 44      # bytes pw-record writes before the first sample (16-bit mono WAV)
MIC_WAIT = 1.0       # if no samples arrive by then, mute anyway and report it
MUTE_MARKER = "muted-output"
CAPTURE_MIN_HOLD = 0.5   # seconds the new buttons must be held when choosing them
# Order used to name a chosen combination ("L1+R1", not "R1+L1").
BUTTON_ORDER = ("L1", "R1", "L2", "R2", "L4", "R4", "L5", "R5", "L3", "R3", "A", "B", "X", "Y",
                "D-pad Up", "D-pad Down", "D-pad Left", "D-pad Right", "Left Trackpad", "Right Trackpad",
                "View", "Menu")
SINK_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")

T0 = time.monotonic()


def log(msg, private=None):
    """One human-readable line. `private` is what the user said or typed: the
    backend shows it in the panel (memory only) but never writes it to a file."""
    if private is None:
        print(f"{time.monotonic() - T0:7.2f}s  {msg}", flush=True)
    else:
        print(f"%% {time.monotonic() - T0:7.2f}s  {msg}\t{private}", flush=True)


def emit(kind, **data):
    """One machine-readable state line for the backend."""
    print("@@ " + json.dumps(dict(data, event=kind), ensure_ascii=False), flush=True)


def set_no_new_privs():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    libc.prctl(1, ctypes.c_ulong(signal.SIGTERM), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))  # PR_SET_PDEATHSIG
    with open("/proc/self/status") as f:
        return next((l.split(":", 1)[1].strip() for l in f if l.startswith("NoNewPrivs:")), "?")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def speech_level(path):
    """Loudest 0.5 s RMS window in dBFS (16-bit mono WAV)."""
    with wave.open(path) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            return -120.0, 0.0
        rate, n = w.getframerate(), w.getnframes()
        a = array.array("h", w.readframes(n))
    step, best = max(rate // 2, 1), 0.0
    for i in range(0, len(a), step):
        chunk = a[i:i + step]
        if chunk:
            best = max(best, math.sqrt(sum(x * x for x in chunk) / len(chunk)))
    return 20 * math.log10(max(best, 1.0) / 32768), n / max(rate, 1)


def clean(text):
    text = NON_SPEECH.sub(" ", text).replace("|", " ")  # "|" starts WoW escape codes
    text = " ".join(text.split())                        # one line, single spaces
    return text.lstrip("/ ").strip()                     # never a slash command from speech


def route(text, lang):
    """Apply a channel keyword spoken as the first word.
    Returns (final_text, channel_note, keyword_found). final_text is "" when
    the keyword has nothing usable after it; channel_note then says why.
    A whisper takes the next word as the player's name: letters only, with
    the punctuation the speech engine adds removed ("Whisper, Thrall, hi").
    """
    tables = [KEYWORDS[lang]] if lang in KEYWORDS else list(KEYWORDS.values())
    parts = text.split(None, 1)
    if not parts:
        return "", "", False
    first = parts[0].strip(EDGE_PUNCT).lower()
    for table in tables:
        if first not in table:
            continue
        command = table[first]
        note = f"keyword '{first}' -> {command}"
        rest = parts[1].lstrip("/ ").strip() if len(parts) > 1 else ""
        if command == WHISPER_CMD:
            words = rest.split(None, 1)
            if words and words[0].strip(EDGE_PUNCT).lower() in WHISPER_FILLER:
                words = words[1].split(None, 1) if len(words) > 1 else []
            name = words[0].strip(EDGE_PUNCT) if words else ""
            rest = words[1].lstrip("/ ").strip() if len(words) > 1 else ""
            if not (name.isalpha() and rest):
                return "", f"{note}, {WHISPER_INCOMPLETE}", True
            return f"{command} {name} {rest}", f"{note}, player name '{name}'", True
        if not rest:
            return "", f"{note}, but no message after it", True
        return f"{command} {rest}", note, True
    return text, "no keyword (default chat channel)", False


def fit_chat(text):
    data = text.encode("utf-8")
    if len(data) <= MAX_CHAT_BYTES:
        return text, False
    return data[:MAX_CHAT_BYTES].decode("utf-8", "ignore").rstrip(), True


def drain(fd):
    while True:
        try:
            if not os.read(fd, hid.REPORT_SIZE):
                return
        except BlockingIOError:
            return


class SoundMute:
    """Mutes the default sound output (speakers or headphones) while the
    microphone is live and restores it afterwards, through pactl in the
    user's own sound session. The output's name goes into a marker file
    before muting, so a crash can be undone later by recover()."""

    def __init__(self, env, marker):
        self.env, self.marker = env, marker
        self.sink = None      # output we muted and must restore
        self.pending = None   # output looked up at press time, muted once the mic is live

    def _pactl(self, *args):
        try:
            r = subprocess.run([PACTL, *args], env=self.env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, str(e)
        if r.returncode != 0:
            return None, (r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}")[-200:]
        return r.stdout.decode("utf-8", "replace").strip(), ""

    def prepare(self):
        """Find the default output and whether it is already muted (at press time)."""
        self.pending = None
        sink, err = self._pactl("get-default-sink")
        if not sink or not SINK_NAME.match(sink):
            log(f"could not mute game sound: no default output ({err or sink!r})")
            return
        state, err = self._pactl("get-sink-mute", sink)
        if state is None:
            log(f"could not mute game sound: {err}")
        elif state.lower().endswith("yes"):
            log("game sound was already muted; leaving it as it is")
        else:
            self.pending = sink

    def mute(self):
        sink, self.pending = self.pending, None
        if sink is None:
            return False
        try:
            with open(self.marker, "w", encoding="utf-8") as f:
                f.write(sink + "\n")
        except OSError as e:
            log(f"could not mute game sound: cannot write {self.marker}: {e}")
            return False
        out, err = self._pactl("set-sink-mute", sink, "1")
        if out is None:
            log(f"could not mute game sound: {err}")
            self._forget()
            return False
        self.sink = sink
        return True

    def restore(self):
        sink, self.sink, self.pending = self.sink, None, None
        if sink is None:
            return
        out, err = self._pactl("set-sink-mute", sink, "0")
        if out is None:
            log(f"could not restore game sound: {err}; it will be retried at the next start")
            return  # keep the marker so the next start retries
        self._forget()

    def recover(self):
        """Undo a mute left behind by a crash or a forced stop."""
        try:
            with open(self.marker, encoding="utf-8") as f:
                sink = f.read().strip()
        except FileNotFoundError:
            return
        except OSError as e:
            log(f"could not read {self.marker}: {e}")
            return
        if SINK_NAME.match(sink):
            self.sink = sink
            log("game sound was left muted by an earlier run; unmuting it")
            self.restore()
        else:
            self._forget()

    def _forget(self):
        try:
            os.unlink(self.marker)
        except FileNotFoundError:
            pass


class Indicator:
    """The on-screen recording symbol: a child process (overlay.py) that gets
    one state word per line. Dictation works without it.

    It is started again whenever it ends: at boot Decky starts the engine before
    Gaming Mode's display exists (Deck logs of 0.3.6 and 0.4.0: "no Gaming Mode
    (Gamescope) display found" 0.3 s after start), and a Steam restart ends its
    display connection (overlay.py then exits at once, also while hidden). Tries
    back off up to RETRY_DELAYS[-1]; each new state of a press (wait, talk, busy)
    while it is not running tries at once and replays the current state."""

    RETRY_DELAYS = (2, 2, 4, 8, 15, 30)  # seconds before each new try
    STEADY = 30          # seconds: a run at least this long counts as working (back-off starts over)
    REPLAY = ("wait", "talk", "busy")    # states still going on when a new overlay.py starts

    def __init__(self, env):
        self.env, self.proc, self.lock = env, None, threading.Lock()
        self.state, self.last_note, self.thread = "hide", None, None
        self.stopped, self.wake = threading.Event(), threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._keep_running, daemon=True)
        self.thread.start()

    def _note(self, line):
        """Log a problem once, not again on every try."""
        if line != self.last_note:
            self.last_note = line
            log(line)

    def _keep_running(self):
        tries = 0
        while not self.stopped.is_set():
            self.wake.clear()
            proc, began = self._spawn(), time.monotonic()
            if proc is not None and self._report(proc) and time.monotonic() - began >= self.STEADY:
                tries = 0
            if self.stopped.is_set():
                break
            self.wake.wait(self.RETRY_DELAYS[min(tries, len(self.RETRY_DELAYS) - 1)])
            tries += 1

    def _spawn(self):
        with self.lock:
            if self.stopped.is_set():
                return None
            try:
                proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "overlay.py")], env=self.env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as e:
                self._note(f"could not start the recording symbol: {e}")
                return None
            self.proc = proc
            if self.state in self.REPLAY:  # the buttons are already held: show it now
                self._send(self.state)
            return proc

    def _report(self, proc):
        """Log what overlay.py says until it ends. True if it was working."""
        ready = False
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("READY"):
                ready, self.last_note = True, None
                log("recording symbol: " + line[:200])
            elif line.startswith("UNAVAILABLE"):
                self._note("could not show the recording symbol: " + line[:200] + "; trying again in the background")
            elif line:
                log("recording symbol: " + line[:200])
        code = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
        if not self.stopped.is_set() and code != 2:  # 2: UNAVAILABLE, reported above
            self._note(f"recording symbol stopped unexpectedly (exit {code}); starting it again")
        return ready

    def _send(self, state):
        """Caller holds the lock."""
        try:
            self.proc.stdin.write(state.encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.proc = None
            self.wake.set()

    def show(self, state):
        with self.lock:
            self.state = state
            if self.proc is not None:
                self._send(state)
            elif state in self.REPLAY:  # a press while it is not running: try now, not after the back-off
                self.wake.set()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        with self.lock:
            proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.close()  # overlay.py exits when its input ends
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


class Pipeline:
    def __init__(self, args):
        self.a = args
        self.whisper = args.whisper
        self.model = args.model
        self.vad_model = getattr(args, "vad_model", None)
        self.runtime = f"/run/user/{os.getuid()}"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8", "XDG_RUNTIME_DIR": self.runtime}
        self.wav = os.path.join(self.runtime, "rootless-dictation", "ptt-last.wav")
        self.rec = None
        state_dir = args.state_dir or os.path.dirname(self.wav)
        self.muter = SoundMute(self.env, os.path.join(state_dir, MUTE_MARKER))
        self.watcher, self.watch_stop = None, threading.Event()
        self.indicator = Indicator(self.env) if getattr(args, "indicator", False) else None

    def cue(self, state):
        if self.indicator is not None:
            self.indicator.show(state)

    def skip(self, reason, quiet=False, **data):
        self.cue("hide" if quiet else "fail")
        emit("skipped", reason=reason, quiet=quiet, **data)

    def fail(self, message):
        log(f"BLOCKER: {message}")
        emit("error", message=message)
        return False

    def preflight(self):
        files = [(self.whisper, "whisper-cli"), (self.model, "ggml-base.bin")]
        if self.vad_model:
            files.append((self.vad_model, "ggml-silero-v6.2.0.bin"))
        for path, name in files:
            if not os.path.isfile(path):
                return self.fail(f"{name} not found at {path}")
            digest = sha256(path)
            if digest != PINNED_SHA256[name]:
                return self.fail(f"{name} does not match its pinned SHA-256 ({digest[:12]}...); not running it")
            log(f"OK: {name} sha256 matches the pinned value")
        if not os.access(self.whisper, os.X_OK):
            return self.fail(f"{self.whisper} is not executable")
        st = os.stat(self.runtime) if os.path.isdir(self.runtime) else None
        if st is None or st.st_uid != os.getuid():
            return self.fail(f"{self.runtime} is missing or not owned by this user")
        os.makedirs(os.path.dirname(self.wav), mode=0o700, exist_ok=True)
        os.makedirs(os.path.dirname(self.muter.marker), mode=0o700, exist_ok=True)
        return True

    # --- recording -------------------------------------------------------
    def start_recording(self):
        try:
            os.unlink(self.wav)
        except FileNotFoundError:
            pass
        started = time.monotonic()
        self.cue("wait")
        self.rec = subprocess.Popen(
            [PW_RECORD, "--rate", "16000", "--channels", "1", "--format", "s16", self.wav],
            env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.watch_stop.clear()
        self.watcher = threading.Thread(target=self._watch_mic, args=(started,), daemon=True)
        self.watcher.start()

    def _watch_mic(self, started):
        """Log when the first samples arrive (the microphone is live) and, with
        --mute, mute the game sound at that moment: the silence tells the user
        to start talking."""
        if self.a.mute:
            self.muter.prepare()
        live = None
        while not self.watch_stop.is_set():
            try:
                if os.path.getsize(self.wav) > WAV_HEADER:
                    live = time.monotonic() - started
                    break
            except OSError:
                pass
            if time.monotonic() - started > MIC_WAIT:
                break
            self.watch_stop.wait(0.01)
        if self.watch_stop.is_set():
            return  # released before the microphone was live: nothing to mute
        self.cue("talk")
        muted = self.a.mute and self.muter.mute()
        what = f"mic live after {live:.2f} s" if live is not None else f"mic start not seen within {MIC_WAIT:.1f} s"
        log(what + ("; game sound muted" if muted else ""))

    def _end_watch(self):
        self.watch_stop.set()
        if self.watcher is not None:
            self.watcher.join(timeout=8)  # at most a few short pactl calls
            self.watcher = None

    def stop_recording(self, quick=False):
        """quick: released too soon to be used; hide the symbol, skip the tail."""
        rec, self.rec = self.rec, None
        if rec is None:
            return False
        if quick:
            self.cue("hide")
        else:
            self.cue("busy")
            time.sleep(TAIL)  # keep the last syllable; the sound stays muted during it
        self._end_watch()
        rec.send_signal(signal.SIGINT)  # pw-record finalises the WAV header on SIGINT
        try:
            _, err = rec.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            rec.kill()
            _, err = rec.communicate()
        self.muter.restore()
        if err and err.strip():
            log("pw-record: " + err.decode("utf-8", "replace").strip()[-300:])
        return os.path.isfile(self.wav)

    def kill_children(self):
        self._end_watch()
        if self.rec is not None and self.rec.poll() is None:
            self.rec.kill()
            self.rec.wait()
        self.muter.restore()
        if self.indicator is not None:
            self.indicator.stop()

    def discard(self):
        """Delete the live recording (never a --from-wav input file)."""
        try:
            os.unlink(self.wav)
        except FileNotFoundError:
            pass

    # --- transcribe, route, inject ----------------------------------------
    def process(self, wav, released_at=None):
        t = time.monotonic()
        level, secs = speech_level(wav)
        log(f"audio {secs:.1f} s, loudest window {level:.0f} dBFS")
        if level < SPEECH_DBFS:
            log("no speech detected (too quiet); nothing typed")
            self.skip("No speech heard (too quiet)")
            return
        emit("transcribing")
        cmd = [self.whisper, "-m", self.model, "-f", wav, "-l", self.a.lang, "-t", str(self.a.threads), "-nt"]
        if self.vad_model:
            cmd += ["--vad", "-vm", self.vad_model] + VAD_ARGS
        if self.a.lang in HINTS:
            cmd += ["--prompt", HINTS[self.a.lang]]
        try:
            res = subprocess.run(cmd, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            log("ERROR: whisper-cli timed out")
            self.skip("Speech-to-text timed out")
            return
        t_stt = time.monotonic() - t
        if res.returncode != 0:
            log(f"ERROR: whisper-cli exit {res.returncode}: {res.stderr.decode('utf-8', 'replace')[-400:]}")
            self.skip(f"Speech-to-text failed (exit {res.returncode})")
            return
        err = res.stderr.decode("utf-8", "replace")
        detected = re.search(r"auto-detected language: (\w+) \(p = ([0-9.]+)\)", err)
        found = VAD_FOUND.search(err)
        if self.vad_model and not (found and int(found.group(1)) > 0):
            log(f"voice detection found no speech ({t_stt:.1f} s); nothing typed")
            self.skip("No speech heard (voice detection)")
            return
        raw = " ".join(res.stdout.decode("utf-8", "replace").split())
        log(f"heard ({t_stt:.1f} s{', detected ' + detected.group(1) + ' p=' + detected.group(2) if detected else ''})", repr(raw))
        text = clean(raw)
        if not text or text.strip(EDGE_PUNCT).lower() in NOISE_WORDS:
            log("transcript empty after cleaning; nothing typed" if not text
                else "transcript is a single word the engine produces on noise; nothing typed")
            self.skip("Nothing recognisable was heard", heard=raw)
            return
        final, note, _found = route(text, detected.group(1) if (detected and self.a.lang == "auto") else self.a.lang)
        if not final:
            log(f"{note}; nothing typed")
            self.skip("Whisper needs a player name, then the message" if note.endswith(WHISPER_INCOMPLETE)
                      else "Channel word without a message", heard=raw)
            return
        final, cut = fit_chat(final)
        send = bool(self.a.send)
        log(f"{note}; will {'SEND' if send else 'type (unsent)'}{' (trimmed to 255 bytes)' if cut else ''}", repr(final))
        emit("typing", text=final)
        inject = [sys.executable, "-I", os.path.join(HERE, "xtest_type.py"), "--expect-app", self.a.expect_app,
                  "--open-chat", "--paste", "--countdown", "0", f"--text={final}"]
        if send:
            inject.append("--send-return")
            if self.a.close_chat:
                inject.append("--close-after-send")
        if self.a.dry_run:
            inject.append("--dry-run")
        t2 = time.monotonic()
        res = subprocess.run(inject, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        out = res.stdout.decode("utf-8", "replace")
        verdict = next((l for l in out.splitlines() if l.startswith(("DONE", "ABORT", "BLOCKER", "DRY RUN"))), out[-200:])
        log(f"inject exit {res.returncode} in {time.monotonic() - t2:.1f} s", verdict)
        timing = next((l for l in out.splitlines() if l.startswith("TIMING: ")), None)
        if timing:
            log(timing[8:])
        total = time.monotonic() - released_at if released_at is not None else None
        if total is not None:
            log(f"total from release to text in chat: {total:.1f} s (speech-to-text {t_stt:.1f} s)")
        if res.returncode == 0:
            self.cue("done")
            emit("result", ok=True, text=final, sent=bool(send and not self.a.dry_run),
                 seconds=round(total if total is not None else t_stt, 1))
        else:
            self.cue("fail")
            reason = "Game was not in front; nothing typed" if verdict.startswith("ABORT") else verdict.strip()[:160]
            emit("result", ok=False, text=final, sent=False, reason=reason)

    def run_live(self):
        nodes = hid.find_deck_nodes()
        if not nodes:
            self.fail("No Steam Deck controller found")
            return 2
        fd, mode, err = hid.open_node(nodes[0][0])
        if fd is None:
            self.fail(f"Cannot read the controller ({nodes[0][0]}): {err}. Not changing any permissions.")
            return 2
        combo = self.a.combo_list
        unlimited = self.a.seconds <= 0
        log(f"OK: reading {nodes[0][0]} with {mode}")
        self.muter.recover()
        if self.indicator is not None:
            self.indicator.start()
        log(f"READY{'' if unlimited else f' for {self.a.seconds:.0f} s'}: hold {'+'.join(combo)}, speak, release. "
            f"Language {self.a.lang}{' with word hint' if self.a.lang in HINTS else ''}, "
            f"voice detection {'ON' if self.vad_model else 'off'}, "
            f"{'leave unsent' if not self.a.send else 'auto-send ON, then Esc' if self.a.close_chat else 'auto-send ON'}, "
            f"mute while talking {'ON' if self.a.mute else 'off'}, recording symbol {'ON' if self.indicator else 'off'}.")
        emit("ready", combo="+".join(combo))
        end = None if unlimited else time.monotonic() + self.a.seconds
        down_at, armed, count = None, True, 0
        try:
            while end is None or time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], 0.1)
                held = None
                if r:
                    try:
                        data = os.read(fd, hid.REPORT_SIZE)
                    except BlockingIOError:
                        data = b"?"
                    if not data:
                        self.fail("Controller disconnected")
                        break
                    states = hid.decode(data)
                    if states is not None:
                        held = all(states[b] for b in combo)
                if down_at is not None and time.monotonic() - down_at > MAX_HOLD:
                    log(f"held over {MAX_HOLD:.0f} s; stopping this recording")
                    held = False
                if held is None:
                    continue
                if not armed:
                    armed = not held  # wait for a full release after processing
                    continue
                if held and down_at is None:
                    down_at = time.monotonic()
                    self.start_recording()
                    count += 1
                    log(f">>> #{count} recording (release {'+'.join(combo)} when done)")
                    emit("recording", n=count)
                elif not held and down_at is not None:
                    released_at = time.monotonic()
                    hold = released_at - down_at
                    down_at = None
                    ok = self.stop_recording(quick=hold < MIN_HOLD)
                    if hold < MIN_HOLD:
                        log(f"<<< released after {hold:.2f} s: too short, ignored")
                        self.skip("Too short (hold the buttons while speaking)", quiet=True)
                    elif not ok:
                        log("<<< ERROR: no recording was produced")
                        self.skip("No recording was produced (microphone?)")
                    else:
                        log(f"<<< released after {hold:.2f} s; transcribing")
                        try:
                            self.process(self.wav, released_at)
                        finally:
                            self.discard()
                    self.discard()  # also when too short or empty
                    drain(fd)      # drop presses queued while busy; only fresh input counts
                    armed = False  # and require a full release before the next recording
                    emit("ready", combo="+".join(combo))
        finally:
            self.kill_children()
            self.discard()
            os.close(fd)
        log(f"session over: {count} recording(s)")
        return 0


def capture(seconds):
    """Report the buttons the user holds next, for choosing the push-to-talk
    buttons. Buttons already held when it starts (such as the press that
    started it) are ignored until released. It remembers the most buttons held
    together and reports them when all are released, if they were held for at
    least CAPTURE_MIN_HOLD seconds. While the user holds buttons it reports
    them ("capture_held": what would be saved, and whether they were held
    long enough), so the panel can show them. Reads the controller passively,
    as run_live."""
    nodes = hid.find_deck_nodes()
    if not nodes:
        emit("capture_failed", reason="No Steam Deck controller found")
        return 2
    fd, mode, err = hid.open_node(nodes[0][0])
    if fd is None:
        emit("capture_failed", reason=f"Cannot read the controller: {err}")
        return 2
    log(f"choosing buttons: reading {nodes[0][0]} with {mode} for up to {seconds:.0f} s")
    emit("capture_waiting", seconds=seconds)
    order = list(BUTTON_ORDER) + [b for b in hid.BUTTON_BITS if b not in BUTTON_ORDER]
    end = time.monotonic() + seconds
    ignore, best, first = None, set(), None
    shown = ([], False)  # last "capture_held" report
    try:
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            data = None
            if r:
                try:
                    data = os.read(fd, hid.REPORT_SIZE)
                except BlockingIOError:
                    pass
                if data == b"":
                    emit("capture_failed", reason="Controller disconnected")
                    return 2
            states = hid.decode(data) if data else None
            now = time.monotonic()
            if states is not None:
                held = {b for b, down in states.items() if down}
                ignore = held if ignore is None else ignore & held
                held -= ignore
                if held:
                    first = first or now
                    if len(held) > len(best):
                        best = held
                elif first is not None:
                    if now - first >= CAPTURE_MIN_HOLD:
                        names = [b for b in order if b in best]
                        log("chosen buttons: " + "+".join(names))
                        emit("captured", buttons=names)
                        return 0
                    best, first = set(), None  # a quick tap: wait for a real hold
            report = ([b for b in order if b in best], first is not None and now - first >= CAPTURE_MIN_HOLD)
            if report != shown:
                emit("capture_held", buttons=report[0], ready=report[1])
                shown = report
        emit("capture_failed", reason="No buttons were held in time")
        return 1
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--whisper", help="path to whisper-cli (SHA-256 pinned)")
    ap.add_argument("--model", help="path to ggml-base.bin (SHA-256 pinned)")
    ap.add_argument("--vad-model", help="path to ggml-silero-v6.2.0.bin (SHA-256 pinned): voice detection")
    ap.add_argument("--capture", type=float, metavar="SECONDS",
                    help="report the next buttons held (to choose push-to-talk buttons), then exit")
    ap.add_argument("--indicator", action="store_true", help="show the recording symbol on screen")
    ap.add_argument("--combo", default="R1+R2")
    ap.add_argument("--lang", choices=("en", "es", "auto"), default="en")
    ap.add_argument("--send", action="store_true",
                    help="press Return to send (default: leave unsent)")
    ap.add_argument("--close-chat", action="store_true",
                    help="with --send: press Esc after sending, to close a chat box that stays open")
    ap.add_argument("--mute", action="store_true", help="mute the default sound output while the mic is live")
    ap.add_argument("--state-dir", help="folder for the mute marker (default: the private runtime folder)")
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this many seconds (0 = until stopped)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--expect-app", default="game",
                    help="only inject while this app ID is focused ('game': any app except Steam's interface)")
    ap.add_argument("--from-wav", help="skip buttons and microphone; process this WAV once")
    ap.add_argument("--dry-run", action="store_true", help="check injection, but inject nothing")
    args = ap.parse_args()

    if os.geteuid() == 0:
        sys.exit("Refusing to run as root. Run this as the normal deck user.")
    if args.capture is None and not (args.whisper and args.model):
        sys.exit("--whisper and --model are required (except with --capture)")
    args.combo_list = [b.strip() for b in args.combo.split("+") if b.strip()]
    bad = [b for b in args.combo_list if b not in hid.BUTTON_BITS]
    if bad or not (args.expect_app == "game" or args.expect_app.isdigit()):
        sys.exit(f"bad --combo {bad} or --expect-app")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    nnp = set_no_new_privs()
    log(f"uid={os.getuid()} gid={os.getgid()} groups={os.getgroups()} NoNewPrivs={nnp}")
    if args.capture is not None:
        return capture(max(1.0, min(args.capture, 30.0)))
    p = Pipeline(args)
    try:
        if not p.preflight():
            return 2
        if args.from_wav:
            p.process(args.from_wav)
            return 0
        return p.run_live()
    finally:
        p.kill_children()
        p.discard()


if __name__ == "__main__":
    sys.exit(main())
