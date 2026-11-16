#!/usr/bin/env python3
"""Package Rootless Dictation as a ZIP for Decky's "Install Plugin from ZIP".

    python3 -I build_zip.py              test build: "Detailed log" switch shown, on by default
    python3 -I build_zip.py --release    release build: switch hidden, only errors logged
    (after: corepack pnpm@9.15.9 run build)

Standard library only. Refuses to run as root. Before packaging it checks that:
- plugin.json does not ask for root and package.json has no "remote_binary"
  (Decky would download it and make the plugin folder world-writable);
- the built panel is newer than its source and avoids Decky's installer,
  tab-scripting, network and eval APIs;
- the Python code does not call sudo, pkexec, polkit, systemctl, chmod/chown,
  setuid, udev or Decky's local server;
- whisper-cli matches its pinned SHA-256 and is not setuid/setgid;
- the voice detection model matches its pinned SHA-256.
Writes out/RootlessDictation-<version>[-release].zip and its .sha256. The
build channel goes into the ZIP as build.json; it is not a file in the repo.
"""
import hashlib
import json
import os
import re
import stat
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = "RootlessDictation"
WHISPER_SRC = os.path.join(HERE, "stt", "out", "whisper-cli")  # built by stt/build_whisper.sh
WHISPER_SHA256 = "36d865e9bc737838df027a3dc30760d3d6f6bf5dab220a214407e75d905d60bd"
# Silero VAD v6.2.0 in ggml format: the copy in the pinned whisper.cpp source tree
# (models/for-tests-silero-v6.2.0-ggml.bin), identical to ggml-org/whisper-vad on Hugging Face.
VAD_SRC = os.path.join(HERE, "stt", "src", "whisper.cpp", "models", "for-tests-silero-v6.2.0-ggml.bin")
VAD_SHA256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"

FILES = ["plugin.json", "package.json", "main.py", "dist/index.js", "LICENSE", "THIRD_PARTY_NOTICES.md",
         "README.md", "engine/ptt_pipeline.py", "engine/hid_ptt_probe.py", "engine/xtest_type.py", "engine/overlay.py",
         "licenses/whisper.cpp-MIT.txt", "licenses/silero-vad-MIT.txt", "licenses/decky-api-LGPL-2.1.txt",
         "licenses/decky-plugin-template-BSD-3-Clause.txt"]
PY_FILES = [f for f in FILES if f.endswith(".py")]
FORBIDDEN_PY = re.compile(r"\b(sudo|pkexec|polkit|systemctl|chmod|chown|setuid|setgid|seteuid|udevadm|"
                          r"install_plugin|1337)\b|/etc/udev|os\.chmod|os\.chown")
FORBIDDEN_JS = re.compile(r"install_plugin|executeInTab|execute_in_tab|fetchNoCors|injectCssIntoTab|"
                          r"openFilePicker|\beval\(|new Function|XMLHttpRequest|WebSocket\(|\bfetch\(|1337")


def fail(msg):
    sys.exit(f"BUILD STOPPED: {msg}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def code_lines(path):
    """Lines of Python code with comments and docstring-only lines removed (rough but conservative)."""
    out, in_doc = [], False
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#") or (s.startswith('"""') and s.endswith('"""')):
            continue
        out.append((n, line.split(" #")[0]))
    return out


def audit():
    pj = json.load(open(os.path.join(HERE, "plugin.json")))
    if "root" in pj.get("flags", []) or "_root" in pj.get("flags", []):
        fail('plugin.json asks for root ("flags")')
    pkg = json.load(open(os.path.join(HERE, "package.json")))
    if "remote_binary" in pkg:
        fail('package.json has "remote_binary"')
    if pkg.get("version") != pj.get("version", pkg.get("version")):
        fail("plugin.json and package.json versions differ")
    dist, src = os.path.join(HERE, "dist", "index.js"), os.path.join(HERE, "src", "index.tsx")
    if not os.path.isfile(dist) or os.path.getmtime(dist) < os.path.getmtime(src):
        fail("dist/index.js is missing or older than src/index.tsx: run the build first")
    js = open(dist, encoding="utf-8").read()
    if FORBIDDEN_JS.search(js):
        fail(f"dist/index.js uses a forbidden API: {FORBIDDEN_JS.search(js).group(0)}")
    for rel in PY_FILES:
        for n, line in code_lines(os.path.join(HERE, rel)):
            m = FORBIDDEN_PY.search(line)
            if m:
                fail(f"{rel}:{n} uses a forbidden call: {m.group(0)}")
    if not os.path.isfile(WHISPER_SRC):
        fail(f"whisper-cli not found at {WHISPER_SRC}")
    if sha256(WHISPER_SRC) != WHISPER_SHA256:
        fail("whisper-cli does not match its pinned SHA-256")
    if os.stat(WHISPER_SRC).st_mode & (stat.S_ISUID | stat.S_ISGID):
        fail("whisper-cli has a setuid/setgid bit")
    if not os.path.isfile(VAD_SRC) or sha256(VAD_SRC) != VAD_SHA256:
        fail(f"voice detection model missing or not matching its pinned SHA-256: {VAD_SRC}")
    print(f"audit OK: flags {pj.get('flags')}, no remote_binary, panel and {len(PY_FILES)} Python files clean, "
          "whisper-cli and voice detection model pinned, whisper-cli not setuid")
    return pkg["version"]


def add_bytes(z, data, arc, mode):
    info = zipfile.ZipInfo(f"{FOLDER}/{arc}", date_time=(2026, 1, 1, 0, 0, 0))
    info.create_system = 3  # Unix, so the mode bits below are kept
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def add(z, src, arc, mode):
    with open(src, "rb") as f:
        add_bytes(z, f.read(), arc, mode)


def main():
    if os.geteuid() == 0:
        sys.exit("Refusing to run as root.")
    args = sys.argv[1:]
    if args not in ([], ["--release"]):
        sys.exit("usage: python3 -I build_zip.py [--release]")
    channel = "release" if args else "dev"
    version = audit()
    out = os.path.join(HERE, "out", f"{FOLDER}-{version}{'-release' if channel == 'release' else ''}.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        for rel in FILES:
            add(z, os.path.join(HERE, rel), rel, 0o755 if rel.startswith("engine/") else 0o644)
        add(z, WHISPER_SRC, "bin/whisper-cli", 0o755)
        add(z, VAD_SRC, "models/ggml-silero-v6.2.0.bin", 0o644)
        add_bytes(z, json.dumps({"channel": channel}).encode() + b"\n", "build.json", 0o644)
    digest = sha256(out)
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(out)}\n")
    with zipfile.ZipFile(out) as z:
        for info in z.infolist():
            print(f"  {info.file_size:8d}  {stat.filemode(info.external_attr >> 16)}  {info.filename}")
    print(f"wrote {out} ({channel} build)\nsha256 {digest}")


if __name__ == "__main__":
    main()
