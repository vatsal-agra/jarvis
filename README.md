# Jarvis — AI Voice Assistant

A personal, voice-controlled desktop assistant for Windows. It listens for the
wake word **"Jarvis"**, understands natural-language commands with an LLM, and
acts on them — opening apps, searching the web, and driving websites end-to-end
(posting, filling forms, multi-step tasks) through a real browser.

## Features

- **Voice in/out** — speech recognition (Google STT) + neural TTS (edge-tts).
- **LLM brain** — Google Gemini 2.5 Flash (free tier) with automatic multi-key
  and multi-model rotation, falling back to a **local Ollama** model
  (`qwen2.5:7b`) when offline or rate-limited.
- **General browser automation** — a DOM snapshot tags every interactive
  element with a number; the model clicks/types by number, so it can operate
  *any* website without site-specific code.
- **Native app & system control** — via the Windows accessibility API
  (pywinauto) and shell commands, not pixel clicking.
- **Interrupt anytime** — press **ESC** to stop the current task mid-action.

## Setup

```bash
pip install SpeechRecognition pyaudio edge-tts pygame wikipedia \
            deep-translator pyshorteners ollama requests ddgs \
            playwright pywinauto keyboard
python -m playwright install chromium
```

### API keys

Keys are **not** stored in the source. Provide Google Gemini keys one of two ways:

1. Copy `jarvis_keys.txt.example` to `jarvis_keys.txt` and paste your keys
   (one per line). This file is git-ignored.
2. Or set the `JARVIS_GEMINI_KEYS` environment variable (comma-separated).

Get free keys at <https://aistudio.google.com/apikey> (one per Google account).
The free tier allows ~20 requests/day per key **per model**, and the app rotates
across keys and across `gemini-2.5-flash` → `gemini-2.5-flash-lite` to stretch
that budget before falling back to local Ollama.

Optional env overrides: `JARVIS_GEMINI_MODELS` (comma-separated model fallback
chain), `JARVIS_GEMINI_MODEL` (single model).

### Run

```bash
python "virtual assistant advanced4.py"
```

On first run a Chrome window opens (no debug port) so you can log into the sites
you want Jarvis to use; the session is saved to `~/.jarvis_chrome_profile`
(git-ignored). Say **"Jarvis stop"** to exit.

## Notes

- Local browser login data lives outside the repo in `~/.jarvis_chrome_profile`
  and is never committed.
- Built for Windows.
