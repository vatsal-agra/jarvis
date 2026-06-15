# Jarvis — AI Voice Assistant

A personal, voice-controlled desktop assistant for Windows. It listens for the
wake word **"Jarvis"**, understands natural-language commands with an LLM, and
acts on them — opening apps, searching the web, and driving websites end-to-end
(posting, filling forms, multi-step tasks) through a real browser.

![Jarvis HUD](docs/hud.png)

> The **live HUD** above opens automatically in your browser when Jarvis starts.
> It streams everything in real time — what you said, which brain/key is active,
> every tool call as it runs, the Gemini daily-quota meter, and the reactor core
> that shifts colour with Jarvis's state (listening · thinking · acting · speaking).

## Features

- **Voice in/out** — speech recognition (Google STT) + neural TTS (edge-tts).
- **LLM brain** — Google Gemini 2.5 Flash (free tier) with automatic multi-key
  and multi-model rotation, falling back to a **local Ollama** model
  (`qwen2.5:7b`) when offline or rate-limited.
- **Vision (multimodal)** — Jarvis can *see*. It can look at your **screen**
  (`look_at_screen`) to read an error or describe what's open, and look at the
  **live browser page** (`look_at_page`) to *visually verify* an action
  actually worked (e.g. confirm a post published) instead of trusting the DOM.
  Powered by Gemini's free multimodal vision.
- **Long-term memory (free RAG)** — Jarvis remembers durable facts about you
  (preferences, accounts, how you like things done) using free Gemini
  embeddings + a local vector store, and **automatically recalls the relevant
  ones before every command** by semantic similarity. Stored privately in
  `jarvis_memory.json` (git-ignored); embeddings use a separate quota pool, so
  they never touch the 20/day generate budget.
- **General browser automation** — a DOM snapshot tags every interactive
  element with a number; the model clicks/types by number, so it can operate
  *any* website without site-specific code — now backed by **vision-based
  verification** for the steps the DOM can't confirm.
- **Native app & system control** — via the Windows accessibility API
  (pywinauto) and shell commands, not pixel clicking.
- **Interrupt anytime** — press **ESC** to stop the current task mid-action.
- **Live HUD** — a cinematic web dashboard (zero extra dependencies, served from
  Python over Server-Sent Events) that visualises the assistant in real time:
  - a hand-written **canvas reactor** — a particle-field energy core with a
    rotating arc assembly and a frequency-spectrum ring that **reacts to your
    actual microphone amplitude** while it listens, and to the speech envelope
    while it talks (the core also shifts colour with state);
  - a cinematic **boot sequence**, depth **parallax** on mouse-move, live
    transcript, streaming action feed, active brain/key pill, and a Gemini
    daily-quota meter.

  Opens automatically on startup; set `JARVIS_HUD_PORT` to change the port
  (default `8765`).

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
