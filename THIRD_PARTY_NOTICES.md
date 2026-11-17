# Third-party notices

Rootless Dictation is an independent project. It is not affiliated with or endorsed by Decktation, Valve, Blizzard or the Decky Loader team.

## Decktation (MIT)

- Source: <https://github.com/silverfoxy/decktation>, Copyright (c) 2026 silverfoxy. The licence notice is included in `LICENSE`.
- **Copied:** the Steam Deck button bit table and trigger offsets in `engine/hid_ptt_probe.py`, from `backend/src/deck_hid.py`.
- **Ideas learned and re-implemented:**
  - finding the controller by HID ID and `/input2`
  - pasting with the clipboard and Ctrl+V
  - checking `GAMESCOPE_FOCUSED_APP` before typing
  - first-word channel keywords; the channel words and their chat commands follow Decktation's English list (`defaults/channel_languages.json`) and its Spanish example (`LANGUAGE_CONFIG.md`)
  - the whisper.cpp version pin
  - muting the sound output while recording (Decktation's "Mute output while recording"); written from scratch here with `pactl`, plus a crash marker
  - an on-screen recording indicator drawn as a Gamescope external overlay (Decktation's recording overlay uses GTK and Cairo), kept on screen and hidden by setting its opacity to 0 as Decktation does; `engine/overlay.py` was written from scratch with plain libX11 calls and its own symbols
  - choosing the push-to-talk buttons by holding them (Decktation's binding capture); written from scratch

## whisper.cpp (MIT)

- `bin/whisper-cli`, built from <https://github.com/ggml-org/whisper.cpp> at commit `f24588a272ae8e23280d9c220536437164e6ed28`.
- CPU only, statically linked libstdc++/libgcc; the build recipe is in the project repository.
- Licence: `licenses/whisper.cpp-MIT.txt`, Copyright (c) 2023-2026 The ggml authors.

## OpenAI Whisper model weights (MIT)

- `models/ggml-base.bin`: OpenAI's Whisper `base` model (<https://github.com/openai/whisper>; "Whisper's code and model weights are released under the MIT License"), converted to ggml format by the whisper.cpp project and downloaded from <https://huggingface.co/ggerganov/whisper.cpp>. It is used only after its SHA-256 is verified.
- Licence: `licenses/openai-whisper-MIT.txt`, Copyright (c) 2022 OpenAI.

## Silero VAD model (MIT)

- `models/ggml-silero-v6.2.0.bin`: the Silero voice activity detection model v6.2.0 (<https://github.com/snakers4/silero-vad>), converted to ggml format by the whisper.cpp project. The file comes from the pinned whisper.cpp source tree (`models/for-tests-silero-v6.2.0-ggml.bin`). It is identical (same SHA-256) to `ggml-silero-v6.2.0.bin` on <https://huggingface.co/ggml-org/whisper-vad>, whose model card says MIT.
- Licence: `licenses/silero-vad-MIT.txt`, Copyright (c) 2020-present Silero Team.

## @decky/api (LGPL-2.1)

- Bundled in `dist/index.js`: the small connector between this plugin's panel and Decky Loader. Source: <https://github.com/SteamDeckHomebrew/decky-api>.
- Licence: `licenses/decky-api-LGPL-2.1.txt`.
- The panel's source and build configuration are published with the project, so the bundled library can be replaced and rebuilt.

## @decky/ui (LGPL-2.1)

Used at build time for component types only. At run time the panel uses the copy that Decky Loader already provides; it is not bundled.

## decky-plugin-template (BSD-3-Clause)

The build configuration (`rollup.config.js`, `tsconfig.json`, `package.json` layout) comes from <https://github.com/SteamDeckHomebrew/decky-plugin-template>. Licence: `licenses/decky-plugin-template-BSD-3-Clause.txt`.
