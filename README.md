# Rootless Dictation (development version)

Hold a button combo on your Steam Deck, speak, release: your words are typed into the game's chat.

- **No root.** `plugin.json` has `"flags": []`, so Decky runs the plugin as your normal user. It never uses sudo and never changes device permissions.
- **Offline speech recognition** with whisper.cpp. Your voice never leaves the Deck.
- **No downloads, no self-updater, no telemetry.** Everything, including the speech models, is inside the ZIP (about 150 MB). The plugin never uses the internet.
- English and Español. Small and readable on purpose.

Independent project, MIT licensed. Not affiliated with or endorsed by Decktation, Valve, Blizzard or the Decky Loader team. See `THIRD_PARTY_NOTICES.md`.

## How to use

1. Open the plugin and switch on **Listen for R1+R2**. Close the menu and go back to the game.
2. Hold **R1+R2**. A microphone symbol appears at the top of the screen: **grey** means wait, **red** means talk now. The game sound also goes quiet.
3. After your last word, keep holding for about a second, then release. Moving dots mean it is working; a green check means your text is in the chat.
4. Start with a channel word to choose the chat, for example **"party …"** or **"grupo …"** becomes `/p …`. Without one, the text goes to the chat channel that is open. Each language has its own words:

| Chat | English | Español | Typed |
| --- | --- | --- | --- |
| Party | party | grupo | `/p` |
| Raid | raid | banda | `/raid` |
| Guild | guild | hermandad | `/g` |
| Officers | officer | oficial | `/o` |
| Say (nearby) | say | di, decir | `/s` |
| Yell | yell | grita, gritar | `/y` |
| Instance | instance | instancia | `/i` |
| Reply to the last whisper | reply | responde, responder | `/r` |
| Whisper a player | whisper *Name* … | susurra *Nombre* … | `/w Name …` |

**Whisper** takes the next word as the player's name ("whisper to *Name*" and "susurra a *Nombre*" also work). The speech engine often gets fantasy names wrong; then the game answers that no such player is online. This is a limit of speech recognition. **Reply** needs no name, so it is the reliable way to answer a whisper.

The channel word only counts as the first word, so a normal sentence that starts with one of them goes to that chat: "Say that again" becomes `/s that again`, and "Di el oro…" ("I gave the gold…") becomes `/s el oro…`.

**Choose your language** instead of Auto. In English and Español, the speech engine is told the channel words in advance, so it recognises them more often. Auto can't do this because it doesn't know the language in advance. It is also slower, and it sometimes guesses the wrong language.

**Background noise is ignored.** A small voice detection model (Silero VAD, included) checks for speech first. If you hold the buttons without speaking, nothing is typed.

**Push-to-talk buttons:** choose **Change buttons** and hold the buttons you want. The panel shows the buttons you are holding and tells you when to let go. A back button (L4, R4, L5, R5) works on its own; other buttons need a combination of two or three.

**Send automatically** presses Enter for you after typing. When it is off, check the text and send it yourself. **Close chat after sending** (on by default) then presses Esc, because in WoW Forever the chat box stays open after sending. Turn it off if your chat box closes by itself, because Esc would then open the game menu.

**Mute game sound while talking** (on by default) silences the Deck's sound output only while the microphone is listening, because game sound makes speech recognition much worse. The sound comes back about half a second after you release, and also after a crash.

## Status

Development step 1 of M1 (test version 0.3.5). It is tested only with World of Warcraft (WoW Forever) on one Steam Deck OLED.

## Building

On Linux (tested on Ubuntu under WSL2), as a normal user:

```
# 1. The panel
corepack pnpm@9.15.9 install --frozen-lockfile --ignore-scripts
corepack pnpm@9.15.9 run build

# 2. The speech engine, from whisper.cpp at the pinned commit (needs cmake and a C++ compiler)
git clone https://github.com/ggml-org/whisper.cpp stt/src/whisper.cpp
git -C stt/src/whisper.cpp checkout f24588a272ae8e23280d9c220536437164e6ed28
bash stt/build_whisper.sh

# The speech model (148 MB), the same file whisper.cpp's models/download-ggml-model.sh fetches
mkdir -p stt/models
curl -L -o stt/models/ggml-base.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
echo "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe  stt/models/ggml-base.bin" | sha256sum -c

# 3. The ZIP for Decky's "Install Plugin from ZIP"
python3 -I build_zip.py
```

`build_zip.py` stops if `whisper-cli`, the speech model or the voice detection model (taken from the whisper.cpp source tree) doesn't match its pinned SHA-256. The two models always match. Your own `whisper-cli` build will probably not match byte for byte, because compilers differ. If you trust your build, put its SHA-256 in `build_zip.py`, `main.py` and `engine/ptt_pipeline.py` (all three check it).

Never build as root. The template's `.vscode/` helper scripts use sudo and are not part of this project.
