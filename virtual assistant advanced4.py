#!/usr/bin/env python
"""
Jarvis — Local AI Voice Assistant
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Brain      : Gemini 2.5 Flash (free tier, multi-key rotation)
             with local qwen2.5:7b via Ollama as offline fallback
Web        : Playwright   persistent Chrome (DOM-based, 99% reliable)
Search     : DuckDuckGo   (free, no key)
Native apps: pywinauto    (Windows accessibility API, not pixels)
TTS        : edge-tts     Microsoft Neural voices
STT        : SpeechRecognition + Google API

Install:
    pip install SpeechRecognition pyaudio edge-tts pygame wikipedia
              deep-translator pyshorteners ollama requests ddgs
              playwright pywinauto
    python -m playwright install chromium
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import datetime
import json
import math
import os
import random as _random
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from datetime import date

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INTERRUPT / BARGE-IN
#  Press ESC at any time to abort the task Jarvis is currently doing (mid tool
#  loop and mid-speech) and return immediately to listening. This is what lets
#  you stop a wrong task the moment you notice it misheard you.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_ABORT = threading.Event()


def _install_abort_hotkey() -> str:
    """Make ESC abort the current task. We arm TWO independent listeners so at
    least one works in any environment:

      1. A global low-level hotkey via the `keyboard` lib. This fires even when
         Jarvis isn't the focused window — essential, because during browser
         automation Chrome is in front, not the terminal. (May need admin on
         some systems / not fire over RDP, hence the second listener.)
      2. A console watcher: on Windows, msvcrt reads single keystrokes, so a
         bare ESC press in the terminal works with no admin and no Enter. On
         other OSes (or if msvcrt is missing) it degrades to reading a line,
         so pressing Enter aborts.

    Both just call _ABORT.set(), which is idempotent — double-firing is fine."""
    arms = []

    # 1) global hotkey (cross-window)
    try:
        import keyboard
        keyboard.add_hotkey("esc", _ABORT.set)
        arms.append("global")
    except Exception:
        pass

    # 2) console watcher (no admin needed)
    try:
        import msvcrt

        def _console_watch():
            while True:
                try:
                    ch = msvcrt.getwch()          # blocks for one keystroke
                except Exception:
                    break
                if ch == "\x1b":                  # ESC
                    _ABORT.set()
        threading.Thread(target=_console_watch, daemon=True).start()
        arms.append("console-esc")
    except Exception:
        def _stdin_watch():
            while True:
                try:
                    input()
                except (EOFError, OSError):
                    break
                _ABORT.set()
        threading.Thread(target=_stdin_watch, daemon=True).start()
        arms.append("stdin-enter")

    if "stdin-enter" in arms and "global" not in arms:
        return "Press Enter in this window any time to stop the current task."
    return "Press ESC any time to stop the current task."

import ollama
import requests
import speech_recognition as sr
import wikipedia


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HUD  ─  live web control center
#  A tiny stdlib HTTP server streams the assistant's state (listening,
#  thinking, which brain/key, every tool call, the reply, Gemini quota) to a
#  browser dashboard (jarvis_hud.html) over Server-Sent Events. Purely
#  observational — if anything here fails, the assistant runs exactly as before.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import http.server
import queue as _queue
import socketserver

_HUD_PORT          = int(os.environ.get("JARVIS_HUD_PORT", "8765"))
_HUD_DIR           = os.path.dirname(os.path.abspath(__file__))
_HUD_CLIENTS       = []                 # one queue.Queue per connected browser
_HUD_LOCK          = threading.Lock()
_HUD_LAST          = {}                 # last value of replayable events (for late joiners)
_GEMINI_USAGE      = {}                 # (key_index, model) -> requests made this session


def _hud_emit(kind: str, **data) -> None:
    """Broadcast one event to every connected HUD browser. Never raises."""
    try:
        data["kind"] = kind
        if kind in ("state", "brain", "quota", "online"):
            _HUD_LAST[kind] = data          # remember so a fresh page isn't blank
        msg = json.dumps(data)
        with _HUD_LOCK:
            for q in list(_HUD_CLIENTS):
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass
    except Exception:
        pass


def _hud_quota() -> None:
    """Emit the per-key/per-model request meter (session counts; 429 marks a key full)."""
    rows = []
    for (idx, model), used in sorted(_GEMINI_USAGE.items()):
        rows.append({"key": idx + 1, "model": model, "used": min(used, 20), "limit": 20})
    if rows:
        _hud_emit("quota", usage=rows)


def _tool_summary(name: str, args: dict) -> str:
    """One-line human summary of a tool call for the action feed."""
    a = args or {}
    for k in ("query", "url", "text", "name", "command", "element", "content", "key"):
        if a.get(k):
            v = str(a[k]).replace("\n", " ")
            if k in ("element", "content"):
                v = f"[{a.get('element','')}] {str(a.get('content',''))}".strip()
            return v[:60]
    return ", ".join(f"{k}={v}" for k, v in list(a.items())[:2])[:60]


def _tool_outcome(result) -> tuple:
    """Map a tool result string to (status, short-detail) for the HUD."""
    s = str(result)
    low = s.lower()
    bad = any(t in low for t in ("error", "could not", "no element", "couldn't", "unavailable", "failed"))
    first = s.strip().splitlines()[0] if s.strip() else ""
    return ("err" if bad else "ok"), first[:54]


class _HudHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(_HUD_DIR, "jarvis_hud.html"), "rb") as f:
                    body = f.read()
            except Exception:
                body = b"<h1 style='color:#fff;background:#000'>jarvis_hud.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q = _queue.Queue(maxsize=256)
            with _HUD_LOCK:
                _HUD_CLIENTS.append(q)
            try:
                self.wfile.write(b": connected\n\n")
                for k in ("online", "brain", "quota", "state"):   # replay current state
                    if k in _HUD_LAST:
                        self.wfile.write(f"data: {json.dumps(_HUD_LAST[k])}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except _queue.Empty:
                        self.wfile.write(b": ping\n\n")    # keep-alive heartbeat
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _HUD_LOCK:
                    try:
                        _HUD_CLIENTS.remove(q)
                    except ValueError:
                        pass
            return

        self.send_response(404)
        self.end_headers()


def _start_hud() -> str:
    """Launch the HUD server in a background thread. Returns its URL, or '' on failure."""
    try:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", _HUD_PORT), _HudHandler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{_HUD_PORT}"
    except Exception:
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _clean_for_speech(text: str) -> str:
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"-{2,}", "—", text)
    return text.strip()


try:
    import edge_tts
    import pygame

    VOICE = "en-US-GuyNeural"
    pygame.mixer.pre_init(44100, -16, 2, 512)
    pygame.mixer.init()

    async def _tts_async(text: str) -> str:
        communicate = edge_tts.Communicate(text, voice=VOICE)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.close()
        await communicate.save(tmp.name)
        return tmp.name

    def speak(text: str) -> None:
        text = _clean_for_speech(str(text))
        print(f"\n🤖 Jarvis: {text}\n")
        _hud_emit("jarvis", text=text)
        _hud_emit("state", state="speaking", sub="responding")
        try:
            import concurrent.futures
            # Run in a fresh thread so asyncio.run() never conflicts
            # with Playwright's internal event loop on the main thread.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fname = pool.submit(lambda: asyncio.run(_tts_async(text))).result(timeout=30)
            pygame.mixer.music.load(fname)
            pygame.mixer.music.play()
            _spk = 0
            while pygame.mixer.music.get_busy():
                if _ABORT.is_set():           # ESC pressed → cut speech short
                    pygame.mixer.music.stop()
                    break
                # Drive the reactor with a lively "talking" envelope while audio plays.
                _spk += 1
                env = 0.45 + 0.35 * abs(math.sin(_spk * 0.6)) + 0.15 * abs(math.sin(_spk * 1.9))
                _hud_emit("level", v=min(1.0, env))
                pygame.time.wait(60)
            pygame.mixer.music.unload()
            os.unlink(fname)
        except Exception as e:
            print(f"[TTS Error] {e}")
        _hud_emit("level", v=0.0)
        _hud_emit("state", state="standby", sub="ready")

except ImportError:
    import pyttsx3

    _engine = pyttsx3.init("sapi5")
    _voices = _engine.getProperty("voices")
    _engine.setProperty("voice", _voices[0].id)
    _engine.setProperty("rate", 170)

    def speak(text: str) -> None:
        text = _clean_for_speech(str(text))
        print(f"\n🤖 Jarvis: {text}\n")
        _hud_emit("jarvis", text=text)
        _hud_emit("state", state="speaking", sub="responding")
        _engine.say(text)
        _engine.runAndWait()
        _hud_emit("state", state="standby", sub="ready")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STT  —  Silero VAD + Google Speech Recognition
#
#  How it works:
#   • Records in 32 ms chunks, feeding each to Silero VAD (neural model)
#   • Starts accumulating only when VAD confirms speech has begun
#   • Ends recording only after 1.5 s of *consistently* low speech probability
#   • Mid-sentence pauses (thinking, breathing) reset the silence counter
#     the moment any speech resumes — so it never cuts you off
#   • Finished audio is sent to Google STT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import numpy as np
import torch

_recognizer = sr.Recognizer()   # kept for Google STT only
_vad_model   = None             # loaded once at first use


def _get_vad():
    global _vad_model
    if _vad_model is None:
        from silero_vad import load_silero_vad
        _vad_model = load_silero_vad()
    return _vad_model


def takeCommand() -> str:
    import pyaudio as _pa

    RATE             = 16000   # Hz  — Silero requires 16 kHz
    CHUNK            = 512     # samples per chunk = 32 ms
    START_THRESH     = 0.5     # VAD prob to consider speech started
    END_THRESH       = 0.3     # VAD prob below which silence counter ticks
    END_CHUNKS_NEED  = 47      # 47 × 32 ms ≈ 1.5 s of silence → done
    MIN_SPEECH_START = 2       # consecutive speech chunks needed to begin recording
    PRE_ROLL         = 16      # chunks of audio kept before speech starts (~0.5 s)
    MAX_CHUNKS       = 625     # hard cap: 20 s
    WAIT_TIMEOUT     = 312     # give up waiting for speech after ~10 s

    model = _get_vad()
    model.reset_states()       # clear RNN state from last call

    pa     = _pa.PyAudio()
    stream = pa.open(
        rate=RATE, channels=1,
        format=_pa.paInt16,
        input=True,
        frames_per_buffer=CHUNK,
    )

    print("🎙  Listening...")
    _hud_emit("state", state="listening", sub="listening for “Jarvis…”")

    pre_roll       = []        # ring buffer before speech starts
    recording      = []        # accumulated speech frames
    speech_started = False
    consec_speech  = 0         # consecutive chunks above START_THRESH
    silence_count  = 0         # consecutive chunks below END_THRESH
    total_waited   = 0
    lvl_tick       = 0         # throttles HUD audio-level emits

    try:
        while True:
            raw     = stream.read(CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            prob    = model(torch.from_numpy(samples), RATE).item()

            # Stream the live mic amplitude to the HUD reactor (every ~64 ms).
            lvl_tick += 1
            if lvl_tick % 2 == 0:
                rms = float(np.sqrt(np.mean(samples * samples)))
                _hud_emit("level", v=min(1.0, rms * 7.0))

            if not speech_started:
                # Keep a short pre-roll buffer so we don't clip the first syllable
                pre_roll.append(raw)
                if len(pre_roll) > PRE_ROLL:
                    pre_roll.pop(0)

                if prob >= START_THRESH:
                    consec_speech += 1
                else:
                    consec_speech = 0

                if consec_speech >= MIN_SPEECH_START:
                    speech_started = True
                    recording      = list(pre_roll)   # include pre-roll
                    silence_count  = 0
                    print("   Recording...")
                    _hud_emit("state", state="listening", sub="hearing you…")

                total_waited += 1
                if total_waited >= WAIT_TIMEOUT:
                    break   # nothing heard for 10 s

            else:
                recording.append(raw)

                if prob >= END_THRESH:
                    silence_count = 0   # speech resumed — reset counter
                else:
                    silence_count += 1

                if silence_count >= END_CHUNKS_NEED:
                    print("   End of speech detected.")
                    break

                if len(recording) >= MAX_CHUNKS:
                    break

    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    if not recording or not speech_started:
        return "none"

    # Hand raw PCM bytes to Google STT via SpeechRecognition
    audio_obj = sr.AudioData(b"".join(recording), RATE, 2)
    try:
        print("🔍 Recognizing...")
        query = _recognizer.recognize_google(audio_obj, language="en-in")
        print(f"👤 You: {query}")
        return query
    except sr.UnknownValueError:
        return "none"
    except sr.RequestError:
        speak("Speech service unavailable. Check your internet.")
        return "none"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BROWSER STATE
#  Priority:
#  Strategy:
#    1. If Jarvis Chrome is already running (port 9223) → reconnect via CDP
#    2. Otherwise → launch Chrome normally via subprocess (zero automation flags)
#       then connect to it via CDP — gives a fully normal, interactable window
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_pw_instance  = None
_pw_browser   = None   # CDP-connected browser object
_pw_page      = None
_jarvis_proc  = None   # subprocess handle for the Jarvis Chrome process

JARVIS_CHROME_PROFILE = os.path.join(os.path.expanduser("~"), ".jarvis_chrome_profile")
JARVIS_CDP_PORT       = 9223   # separate port so we never clash with user's Chrome

_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.path.expanduser("~"),
                 r"AppData\Local\Google\Chrome\Application\chrome.exe"),
]


def _find_chrome() -> str | None:
    for p in _CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def _ensure_playwright():
    global _pw_instance
    if _pw_instance is None:
        from playwright.sync_api import sync_playwright
        _pw_instance = sync_playwright().start()


def _cdp_page(port: int):
    """Connect to Chrome at *port* via CDP and return its first page."""
    try:
        requests.get(f"http://localhost:{port}/json/version", timeout=1)
        browser = _pw_instance.chromium.connect_over_cdp(f"http://localhost:{port}")
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx:
            return browser, (ctx.pages[0] if ctx.pages else ctx.new_page())
    except Exception:
        pass
    return None, None


def _launch_jarvis_chrome():
    """
    Two-phase Chrome launch:

    Phase 1 (first run only) — login setup
      Launch Chrome with NO remote-debugging-port and NO CDP connection at all.
      The window is 100% normal: Google OAuth, LinkedIn login, everything works.
      User logs in, then closes Chrome.  We wait for it to exit.

    Phase 2 — automation
      Relaunch Chrome with --remote-debugging-port=JARVIS_CDP_PORT.
      Playwright connects via CDP.  User is already logged in so no OAuth flows
      happen and the debugger-pause trap is never triggered.
    """
    global _pw_browser, _jarvis_proc

    os.makedirs(JARVIS_CHROME_PROFILE, exist_ok=True)
    first_launch = not os.path.exists(
        os.path.join(JARVIS_CHROME_PROFILE, "Default", "Preferences")
    )

    chrome_exe = _find_chrome()
    if not chrome_exe:
        print("[Browser] Chrome not found — falling back to Playwright Chromium.")
        _pw_browser = _pw_instance.chromium.launch_persistent_context(
            JARVIS_CHROME_PROFILE, headless=False, slow_mo=80,
        )
        page = _pw_browser.pages[0] if _pw_browser.pages else _pw_browser.new_page()
        return page

    # ── Phase 1: login setup (first run only) ─────────────────────────────────
    if first_launch:
        print("\n" + "="*60)
        print("  FIRST-TIME BROWSER SETUP")
        print("  A normal Chrome window will open.")
        print("  Log into Google, LinkedIn, YouTube — any site Jarvis will use.")
        print("  When you are done, CLOSE that Chrome window.")
        print("  Jarvis will continue automatically.")
        print("="*60 + "\n")
        speak(
            "First time setup. A Chrome window is opening with no automation. "
            "Please log into your accounts, then close Chrome when you are done. "
            "Jarvis will continue automatically."
        )
        # Launch completely plain Chrome — zero debug port, zero CDP
        setup_proc = subprocess.Popen([
            chrome_exe,
            f"--user-data-dir={JARVIS_CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
        ])
        print("  ⏳ Waiting for you to finish logging in and close Chrome...")
        setup_proc.wait()   # blocks until the user closes Chrome
        print("  ✅ Login saved. Launching automation browser...\n")
        speak("Logins saved. Starting up now.")

    # ── Phase 2: automation launch ─────────────────────────────────────────────
    _jarvis_proc = subprocess.Popen([
        chrome_exe,
        f"--user-data-dir={JARVIS_CHROME_PROFILE}",
        f"--remote-debugging-port={JARVIS_CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ])
    # Wait up to 10 s for the CDP endpoint to become available
    for _ in range(20):
        time.sleep(0.5)
        browser, page = _cdp_page(JARVIS_CDP_PORT)
        if page:
            _pw_browser = browser
            return page

    print("[Browser] Timed out waiting for Chrome CDP — is Chrome installed?")
    return None


def _get_page():
    """Return active Playwright page, launching Jarvis Chrome if needed."""
    global _pw_page, _pw_browser
    _ensure_playwright()

    # Check if existing page is still alive
    if _pw_page is not None:
        try:
            closed = _pw_page.is_closed()
        except Exception:
            closed = True   # CDP connection broken = Chrome was closed
        if closed:
            _pw_page = None  # discard stale reference so relaunch triggers below
        else:
            # Follow to the most recent tab if a popup opened
            if _pw_browser:
                try:
                    for ctx in _pw_browser.contexts:
                        pages = [p for p in ctx.pages if not p.is_closed()]
                        if pages and pages[-1] != _pw_page:
                            _pw_page = pages[-1]
                except Exception:
                    pass
            return _pw_page

    # Try reconnecting to Jarvis Chrome if it's still running
    browser, page = _cdp_page(JARVIS_CDP_PORT)
    if page:
        _pw_browser = browser
        _pw_page = page
        print("[Browser] Reconnected to Jarvis Chrome via CDP.")
        return _pw_page

    # Chrome not running — launch it fresh
    _pw_page = _launch_jarvis_chrome()
    return _pw_page


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INDEXED-ELEMENT SNAPSHOT
#
#  The reliable way to drive an LLM browser agent: instead of asking the model
#  to fuzzy-match element text (which breaks constantly), we tag EVERY visible
#  interactive element with a number (data-jarvis-id) and hand the model a clean
#  numbered list. The model just says "click 5" or "type into 3" — no guessing.
#  This is site-agnostic: it works on any page because it reads the live DOM.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_INDEX_JS = r"""
() => {
  const txt = (el) => (el && el.innerText || '').replace(/\s+/g, ' ').trim();

  // Resolve the best human-readable name for an element. Crucially this follows
  // <label for=id>, wrapping <label>, and aria-labelledby — so standard form
  // fields (GitHub's "Repository name", etc.) get a real name instead of blank.
  const nameOf = (el, isInput) => {
    let t = (el.getAttribute('aria-label') || '').trim();
    if (t) return t;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const s = lb.split(/\s+/).map(id => {
        const e = document.getElementById(id); return e ? txt(e) : '';
      }).join(' ').trim();
      if (s) return s;
    }
    if (isInput) {
      if (el.id) {
        try {
          const la = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
          if (la) { const s = txt(la); if (s) return s; }
        } catch (e) {}
      }
      const w = el.closest('label');
      if (w) { const s = txt(w); if (s) return s; }
      let p = (el.getAttribute('placeholder') || el.getAttribute('aria-placeholder')
               || el.getAttribute('data-placeholder') || '').trim();
      if (p) return p;
      if (el.value) { const v = String(el.value).trim(); if (v) return v; }
      return (el.getAttribute('name') || el.getAttribute('title') || '').trim();
    }
    let s = txt(el); if (s) return s;
    if (el.value) { const v = String(el.value).trim(); if (v) return v; }
    let p = (el.getAttribute('placeholder') || '').trim(); if (p) return p;
    return (el.getAttribute('title') || el.getAttribute('name')
            || el.getAttribute('alt') || '').trim();
  };

  const sel = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role=button]', '[role=link]', '[role=textbox]', '[role=checkbox]',
    '[role=radio]', '[role=tab]', '[role=menuitem]', '[role=combobox]',
    '[role=searchbox]', '[role=switch]', '[contenteditable=true]',
    '[contenteditable=""]', '[onclick]'
  ].join(',');
  const seen = new Set();
  const out = [];
  let id = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    if (el.disabled) continue;

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || '';
    const editable = el.isContentEditable;
    const isInput = tag === 'input' || tag === 'textarea' || tag === 'select'
                    || editable || role === 'textbox' || role === 'searchbox'
                    || role === 'combobox';

    let label = nameOf(el, isInput).replace(/\s+/g, ' ').slice(0, 90);

    // Skip noise: non-input elements with no readable label.
    if (!label && !isInput) continue;

    let kind;
    if (isInput) kind = (type && tag === 'input') ? ('input:' + type) : (editable ? 'editor' : tag);
    else kind = role || tag;

    el.setAttribute('data-jarvis-id', String(id));
    out.push({ id, kind, label, input: isInput });
    id++;
  }
  return out;
}
"""


def _index_interactive(page):
    """Tag every visible interactive element with data-jarvis-id and return a
    formatted numbered list plus the raw element data."""
    try:
        items = page.evaluate(_INDEX_JS)
    except Exception:
        items = []
    inputs, clicks = [], []
    for it in items:
        label = it.get("label", "") or "(no label)"
        line = f"  [{it['id']}] {it['kind']}: \"{label}\""
        (inputs if it.get("input") else clicks).append(line)
    parts = []
    if inputs:
        parts.append("FIELDS you can type into (browser_type element=number):\n" + "\n".join(inputs))
    if clicks:
        parts.append("BUTTONS/LINKS you can click (browser_click text=number):\n" + "\n".join(clicks))
    return "\n\n".join(parts), items


def _as_index(value):
    """Return an int element-id if *value* is a bare number (e.g. '5', '[5]'),
    else None. Lets the model address elements by their snapshot number."""
    s = str(value).strip().lstrip("[").rstrip("]").strip()
    if s.isdigit():
        return int(s)
    return None


def _settle(page, idle_ms: int = 4000, pause: float = 0.7) -> None:
    """Wait for a page to stop changing after an action. Clicks that open a
    modal/composer or load content need a beat before the DOM reflects the new
    state — snapshotting too early is why an action's result looks 'unchanged'.
    Generic: no per-site logic, just wait for network to go idle then pause for
    any open/close animation to finish."""
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:
        pass  # not all actions trigger network — the pause below still helps
    time.sleep(pause)


def _fill_locator(page, loc, content) -> bool:
    """Type *content* into a located element, handling both plain inputs
    (fill) and rich-text / contenteditable editors (click + keyboard type)."""
    try:
        loc.wait_for(state="visible", timeout=4000)
    except Exception:
        return False
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    # Plain inputs/textarea/select accept fill() directly.
    try:
        tag = loc.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = ""
    if tag in ("input", "textarea", "select"):
        try:
            loc.fill(content, timeout=3000)
            return True
        except Exception:
            pass
    # contenteditable / role=textbox — click to focus, then type.
    try:
        loc.click(timeout=3000)
        time.sleep(0.3)
        try:
            loc.fill("", timeout=1500)   # clear if it's fillable
        except Exception:
            pass
        page.keyboard.type(str(content), delay=12)
        return True
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOOL DEFINITIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOLS = [
    # ── quick URL open (no interaction needed) ────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a URL in the system default browser. Use this ONLY when you just "
                "want to show the user a page — no further interaction needed. "
                "For tasks that require typing, clicking, or reading the page, "
                "use browser_navigate + browser_* tools instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL with https://"}
                },
                "required": ["url"],
            },
        },
    },
    # ── OS / shell ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": (
                "Run a Windows cmd or PowerShell command. Use for opening native apps, "
                "file operations, system tasks. Do NOT pass raw URLs here — use open_url. "
                "Examples: 'start notepad.exe', 'start calc.exe', 'start spotify:', "
                "'start microsoft.windows.camera:', 'start ms-clock:Stopwatch', "
                "'shutdown /s /t 1', 'shutdown /r /t 1'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command":    {"type": "string",  "description": "Command to run"},
                    "powershell": {"type": "boolean", "description": "True = PowerShell, False = cmd"},
                },
                "required": ["command"],
            },
        },
    },
    # ── real-time web search ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web with DuckDuckGo and return current results. "
                "Use for: news, weather, prices, current events, anything that needs "
                "up-to-date information the model may not know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string",  "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    # ── browser automation ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Navigate the Playwright browser to a URL. Use this (not open_url) "
                "when you need to interact with the page afterwards. "
                "Logins persist between sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL with https://"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": (
                "Read the current browser page. Returns a NUMBERED list of every "
                "interactive element (buttons, links, inputs, editors) plus a text preview. "
                "Each element has a [number] — use that number with browser_click and "
                "browser_type. Call this after navigating or after any action that changes the page."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element on the current page. STRONGLY PREFER passing the "
                "element's [number] from the latest browser_snapshot (e.g. '5'). "
                "You may pass visible text as a fallback, but numbers are far more reliable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The element [number] from the snapshot (preferred), or visible text"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": (
                "Type text into an input field or editor on the current page. "
                "STRONGLY PREFER passing the field's [number] from the latest "
                "browser_snapshot as 'element'. Set submit=true to press Enter after "
                "typing (useful for search boxes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {"type": "string", "description": "The field's [number] from the snapshot (preferred), or its placeholder/label"},
                    "content": {"type": "string", "description": "Text to type"},
                    "submit":  {"type": "boolean", "description": "Press Enter after typing (default false)"},
                },
                "required": ["element", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_key",
            "description": "Press a keyboard key in the browser. E.g. 'Enter', 'Tab', 'Escape', 'Control+a'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key name (Playwright key format)"}
                },
                "required": ["key"],
            },
        },
    },
    # ── native Windows app control ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "windows_control",
            "description": (
                "Control a native Windows application using the Windows Accessibility API. "
                "Actions: 'focus' (bring window to front), 'click' (click a button by text), "
                "'type' (type text into focused app or specific element), 'get_text' (read window content). "
                "Use for apps like Notepad, Calculator, Spotify desktop, File Explorer, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "Partial window title to match"},
                    "action":   {"type": "string", "description": "focus | click | type | get_text"},
                    "element":  {"type": "string", "description": "Button/element text (for click/type)"},
                    "text":     {"type": "string", "description": "Text to type (for type action)"},
                },
                "required": ["app_name", "action"],
            },
        },
    },
    # ── knowledge / utility ───────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "wikipedia_search",
            "description": "Search Wikipedia and return a 3-sentence summary. Use for facts, people, places, concepts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic to look up"}
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_date",
            "description": "Get today's date.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate_text",
            "description": "Translate text to another language.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text":            {"type": "string", "description": "Text to translate"},
                    "target_language": {"type": "string", "description": "Target language (name or ISO code)"},
                },
                "required": ["text", "target_language"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shorten_url",
            "description": "Shorten a long URL via TinyURL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to shorten"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_game",
            "description": (
                "ONLY use this tool when the user explicitly asks to play or open one of these specific games: "
                "breakout, car arcade, obstacle course, space invaders, football champs, path finder, tic tac toe. "
                "Do NOT use this for any other task — not for posting, searching, browsing, or anything else."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "game_name": {
                        "type": "string",
                        "description": "One of: breakout, car arcade, obstacle course, space invaders, football champs, path finder, tic tac toe, random",
                    }
                },
                "required": ["game_name"],
            },
        },
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOOL EXECUTOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GAME_FILES = {
    "breakout":        "breakout arcade gaming.py",
    "car arcade":      "car arcade gaming.py",
    "obstacle":        "complete obby game! - ursina.py",
    "obstacle course": "complete obby game! - ursina.py",
    "space invaders":  "space-invaders final game.py",
    "football":        "football champs.py",
    "football champs": "football champs.py",
    "path finder":     "path finder.py",
    "tic tac toe":     "tic tac toe2options.py",
}


def execute_tool(name: str, args: dict) -> str:
    print(f"⚙  Tool → {name}({args})")

    # ── open_url ──────────────────────────────────────────────────────────────
    if name == "open_url":
        url = args.get("url", "").strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened {url}"

    # ── run_shell_command ─────────────────────────────────────────────────────
    elif name == "run_shell_command":
        cmd = args.get("command", "")
        use_ps = args.get("powershell", False)
        if cmd.startswith("http://") or cmd.startswith("https://"):
            webbrowser.open(cmd)
            return f"Opened {cmd}"
        try:
            if use_ps:
                proc = subprocess.run(
                    ["powershell", "-Command", cmd],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=15,
                )
            out = (proc.stdout or proc.stderr or "Done.").strip()
            return out[:500]
        except subprocess.TimeoutExpired:
            return "Started (running in background)."
        except Exception as e:
            return f"Error: {e}"

    # ── web_search ────────────────────────────────────────────────────────────
    elif name == "web_search":
        query = args.get("query", "")
        max_r = int(args.get("max_results", 5))
        try:
            from ddgs import DDGS
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_r):
                    results.append(
                        f"• {r['title']}\n  {r['href']}\n  {r['body'][:200]}"
                    )
            return "\n\n".join(results) if results else "No results found."
        except Exception as e:
            return f"Search error: {e}"

    # ── browser_navigate ──────────────────────────────────────────────────────
    elif name == "browser_navigate":
        url = args.get("url", "")
        if not url.startswith("http"):
            url = "https://" + url
        try:
            page = _get_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            # Give JS-heavy pages (LinkedIn, GitHub, etc.) time to render
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # timeout is fine — page is usable, just still loading extras
            time.sleep(0.5)
            snap = execute_tool("browser_snapshot", {})
            return f"Navigated to: {page.title()} ({page.url})\n\n{snap}"
        except Exception as e:
            return f"Navigation error: {e}"

    # ── browser_snapshot ──────────────────────────────────────────────────────
    elif name == "browser_snapshot":
        try:
            page = _get_page()
            listing, items = _index_interactive(page)
            title   = page.title()
            url_now = page.url
            try:
                preview = page.inner_text("body")[:700].replace("\n", " ").strip()
            except Exception:
                preview = ""

            result  = f"Page: {title}\nURL:  {url_now}\n\n"
            if items:
                result += listing
            else:
                result += "No interactive elements detected (page may still be loading)."
            result += f"\n\nPage text preview:\n{preview}"
            return result
        except Exception as e:
            return f"Snapshot error: {e}"

    # ── browser_click ─────────────────────────────────────────────────────────
    elif name == "browser_click":
        text = str(args.get("text", "")).strip()
        try:
            page = _get_page()

            # Preferred path: the model gave us an element NUMBER from the snapshot.
            idx = _as_index(text)
            if idx is not None:
                loc = page.locator(f"[data-jarvis-id='{idx}']")
                if loc.count() == 0:
                    return (
                        f"No element [{idx}] on this page (numbers change when the page "
                        f"updates). Call browser_snapshot to get fresh numbers."
                    )
                try:
                    loc.first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                loc.first.click(timeout=5000)
                _settle(page)
                snap = execute_tool("browser_snapshot", {})
                return f"Clicked element [{idx}]. Page now:\n\n{snap}"

            # Fallback: text-based matching (kept for resilience).
            strategies = [
                lambda: page.get_by_role("button", name=text).first.click(timeout=3500),
                lambda: page.get_by_role("link",   name=text).first.click(timeout=3500),
                lambda: page.get_by_text(text, exact=False).first.click(timeout=3500),
                lambda: page.get_by_label(text).first.click(timeout=3500),
                lambda: page.get_by_placeholder(text).first.click(timeout=3500),
                lambda: page.locator(f"[aria-label*='{text}']").first.click(timeout=3500),
            ]
            for strategy in strategies:
                try:
                    strategy()
                    _settle(page)
                    snap = execute_tool("browser_snapshot", {})
                    return f"Clicked '{text}'. Page now:\n\n{snap}"
                except Exception:
                    continue
            return (
                f"Could not find '{text}' to click. Call browser_snapshot and "
                f"click using the element [number] instead."
            )
        except Exception as e:
            return f"Click error: {e}"

    # ── browser_type ──────────────────────────────────────────────────────────
    elif name == "browser_type":
        element = str(args.get("element", "")).strip()
        content = args.get("content", "")
        submit  = bool(args.get("submit", False))

        if not element:
            return (
                "ERROR: 'element' is empty. Call browser_snapshot, then pass the "
                "input field's [number] as 'element'."
            )

        try:
            page = _get_page()

            # Preferred path: the model gave us an element NUMBER from the snapshot.
            idx = _as_index(element)
            if idx is not None:
                base = page.locator(f"[data-jarvis-id='{idx}']")
                if base.count() == 0:
                    return (
                        f"No element [{idx}] on this page (numbers change when the page "
                        f"updates). Call browser_snapshot to get fresh numbers."
                    )
                loc = base.first
                ok = _fill_locator(page, loc, content)
                if not ok:
                    return f"Could not type into element [{idx}]."
                if submit:
                    page.keyboard.press("Enter")
                    time.sleep(0.6)
                _settle(page)
                snap = execute_tool("browser_snapshot", {})
                done = "Typed into element [{}].{}".format(idx, " Submitted." if submit else "")
                return f"{done} Page now:\n\n{snap}"

            # Fallback: text/label-based matching.
            fill_strategies = [
                lambda: page.get_by_placeholder(element).first,
                lambda: page.get_by_label(element).first,
                lambda: page.get_by_role("textbox",   name=element).first,
                lambda: page.get_by_role("searchbox", name=element).first,
            ]
            for make in fill_strategies:
                try:
                    loc = make()
                    if _fill_locator(page, loc, content):
                        if submit:
                            page.keyboard.press("Enter")
                            time.sleep(0.6)
                        _settle(page)
                        snap = execute_tool("browser_snapshot", {})
                        done = "Typed into '{}'.{}".format(element, " Submitted." if submit else "")
                        return f"{done} Page now:\n\n{snap}"
                except Exception:
                    continue
            return (
                f"Could not find field '{element}'. Call browser_snapshot and pass "
                f"the field's [number] as 'element'."
            )
        except Exception as e:
            return f"Type error: {e}"

    # ── browser_key ───────────────────────────────────────────────────────────
    elif name == "browser_key":
        key = args.get("key", "")
        try:
            page = _get_page()
            page.keyboard.press(key)
            time.sleep(0.3)
            return f"Pressed '{key}'"
        except Exception as e:
            return f"Key error: {e}"

    # ── windows_control ───────────────────────────────────────────────────────
    elif name == "windows_control":
        app_name = args.get("app_name", "")
        action   = args.get("action", "")
        element  = args.get("element", "")
        text     = args.get("text", "")
        try:
            from pywinauto import Application, Desktop
            from pywinauto.keyboard import send_keys

            if action == "focus":
                wins = Desktop(backend="uia").windows(title_re=f".*{app_name}.*")
                if wins:
                    wins[0].set_focus()
                    return f"Focused: {wins[0].window_text()}"
                return f"No window found matching '{app_name}'"

            elif action == "click":
                app = Application(backend="uia").connect(
                    title_re=f".*{app_name}.*", timeout=5
                )
                win = app.top_window()
                win.child_window(title=element, control_type="Button").click_input()
                return f"Clicked '{element}' in {app_name}"

            elif action == "type":
                app = Application(backend="uia").connect(
                    title_re=f".*{app_name}.*", timeout=5
                )
                win = app.top_window()
                win.set_focus()
                if element:
                    ctrl = win.child_window(title=element)
                    ctrl.set_edit_text(text)
                else:
                    send_keys(text, with_spaces=True)
                return f"Typed in {app_name}"

            elif action == "get_text":
                app = Application(backend="uia").connect(
                    title_re=f".*{app_name}.*", timeout=5
                )
                return app.top_window().window_text()[:500]

            return f"Unknown action '{action}'"
        except Exception as e:
            return f"Windows control error: {e}"

    # ── wikipedia_search ──────────────────────────────────────────────────────
    elif name == "wikipedia_search":
        topic = args.get("topic", "")
        try:
            summary = wikipedia.summary(topic, sentences=3)
            return summary
        except wikipedia.exceptions.DisambiguationError as e:
            return f"Multiple results for '{topic}'. Did you mean: {', '.join(e.options[:3])}?"
        except wikipedia.exceptions.PageError:
            return f"No Wikipedia page found for '{topic}'."
        except Exception as e:
            return f"Wikipedia error: {e}"

    # ── get_current_time ──────────────────────────────────────────────────────
    elif name == "get_current_time":
        return datetime.datetime.now().strftime("%I:%M %p")

    # ── get_current_date ──────────────────────────────────────────────────────
    elif name == "get_current_date":
        return date.today().strftime("%B %d, %Y")

    # ── translate_text ────────────────────────────────────────────────────────
    elif name == "translate_text":
        text   = args.get("text", "")
        target = args.get("target_language", "english")
        try:
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="auto", target=target).translate(text)
        except Exception as e:
            return f"Translation error: {e}"

    # ── shorten_url ───────────────────────────────────────────────────────────
    elif name == "shorten_url":
        url = args.get("url", "")
        try:
            import pyshorteners
            short = pyshorteners.Shortener().tinyurl.short(url)
            print(f"   Shortened URL: {short}")
            return short
        except Exception as e:
            return f"Could not shorten URL: {e}"

    # ── open_game ─────────────────────────────────────────────────────────────
    elif name == "open_game":
        game_name = args.get("game_name", "").lower()
        if "random" in game_name:
            game_file = _random.choice(list(GAME_FILES.values()))
        else:
            game_file = next(
                (v for k, v in GAME_FILES.items() if k in game_name), None
            )
        if not game_file:
            return f"Game '{game_name}' not found. Available: {', '.join(GAME_FILES)}"
        try:
            os.startfile(game_file)
            return f"Opened {game_file}"
        except FileNotFoundError:
            return f"Game file not found: {game_file}"

    return f"Unknown tool: {name}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SYSTEM PROMPT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = """You are Jarvis, a personal AI voice assistant running on Windows 11.

━━ CORE RULES ━━
• Your responses are SPOKEN ALOUD — keep final replies SHORT (1-2 sentences).
• NEVER use markdown, bullet points, asterisks, or headers in your spoken replies.
• Always USE TOOLS to complete tasks — never pretend you did something without calling a tool.
• Never invent fake results. If a tool fails, say so honestly.

━━ TOOL SELECTION ━━
Task                                    → Tool
Open a site (no interaction needed)     → open_url
Post / create / fill / click on a site  → browser_navigate then browser_snapshot then browser_click/type
Read or interact with a webpage         → browser_navigate + browser_snapshot + browser_click/type
Real-time info (news/weather/prices)    → web_search
Open a native Windows app               → run_shell_command
Control an open Windows app             → windows_control
Facts / encyclopedia                    → wikipedia_search

━━ BROWSER WORKFLOW (works on ANY website) ━━
Every browser_snapshot returns a NUMBERED list of the page's interactive elements:
    [0] button: "Start a post"
    [1] link: "Home"
    [2] input:text: "Search"
    [12] editor: "What do you want to talk about?"
You act on elements BY THEIR NUMBER — this is reliable; matching by text is not.

  browser_navigate(url)          → Go to a URL. Automatically returns the numbered snapshot.
  browser_snapshot()             → Re-read the page. ALWAYS do this after a click that
                                   opens a dialog, expands a form, or changes the page.
  browser_click(text="5")        → Click element number 5.
  browser_type(element="12", content="...")        → Type into element number 12.
  browser_type(element="2", content="cats", submit=true) → Type, then press Enter.
  browser_key(key)               → Press a key (Enter, Tab, Escape).

THE LOOP — repeat until the task is done:
  1. browser_navigate to the right URL.
  2. Look at the numbered elements. The snapshot is split into FIELDS (type into) and
     BUTTONS/LINKS (click). MATCH each field by its label — e.g. to set a repository
     name, type into the field labelled "Repository name", NOT just element [1].
  3. Type into the right field(s) by number; click buttons by number.
  4. After any click that changes the page (opens a dialog/composer, submits a form),
     call browser_snapshot AGAIN — old numbers are invalid once the page changes, and a
     composer dialog adds NEW fields (the editor) that weren't there before.
  5. Finish by clicking the real submit button (e.g. "Create repository", "Post") by
     its number. Then snapshot once more to CONFIRM it worked before you reply.
You have up to 10 tool calls per command — use them to actually finish the job.

WRITING A POST / COMMENT (general pattern for any site):
  click the compose button → browser_snapshot (the editor is a NEW field now) →
  browser_type your full content into the editor → click the Post/Share button →
  browser_snapshot to confirm it appears. Do NOT stop right after opening the composer.

⚠ NEVER NARRATE INSTEAD OF ACTING. This is the most important rule.
  A spoken reply ENDS your turn. So if the task is not finished, DO NOT reply with
  words — call the next tool RIGHT NOW in the same turn. Phrases like "I'm taking a
  look", "let me gather", "I'm on the page and ready", "I'll now write it" are FORBIDDEN
  as a turn-ending reply while work remains. Either call a tool, or — only when truly
  done — give the short final reply. There is no in-between.

CONTINUING EARLIER WORK:
  You do NOT remember the previous page. If the user follows up about something you were
  doing ("I don't see the text", "now post it", "it didn't work", "try again"), your
  FIRST action MUST be browser_snapshot to read the LIVE page, then continue from there.

RULES:
  • Always write REAL, complete content — never placeholders like "content goes here".
    If web_search gives thin results, write from your own knowledge instead of stalling.
  • Pick the field whose LABEL matches what you're entering. Never type into a random
    field or a search box by mistake.
  • Use submit=true ONLY for a single search box. For multi-field forms / posts, type the
    content then CLICK the submit button — do not press Enter.
  • Element numbers change every time the page changes. Re-snapshot before acting again.
  • Never ask the user for a URL or what to type — figure it out. Only ask ONE short
    question if a required value (like a repo name) was genuinely not provided.
  • A task is NOT done after typing one field. Only say it's done after you clicked the
    final submit button AND a fresh snapshot confirms it. Never claim success otherwise.

━━ LOGIN PAGES ━━
If a page snapshot shows a sign-in or login form:
  STOP. Do NOT fill in any username or password.
  Say: "Please log into [site] in the Jarvis browser window, then ask me again."

━━ NATIVE WINDOWS APP SHORTCUTS ━━
Spotify    → run_shell_command("start spotify:")          ← always app, never spotify.com
Camera     → run_shell_command("start microsoft.windows.camera:")
Stopwatch  → run_shell_command("start ms-clock:Stopwatch")
Calculator → run_shell_command("start calc.exe")
Notepad    → run_shell_command("start notepad.exe")
Explorer   → run_shell_command("start explorer")
Task Mgr   → run_shell_command("taskmgr")
Shutdown   → run_shell_command("shutdown /s /t 1")
Restart    → run_shell_command("shutdown /r /t 1")

━━ URL PATTERNS ━━
YouTube search  : https://www.youtube.com/results?search_query=QUERY
Google search   : https://www.google.com/search?q=QUERY
Amazon India    : https://www.amazon.in/s?k=QUERY&sort=review-rank
Google Maps     : https://www.google.com/maps/dir/FROM/TO
Google News     : https://news.google.com/search?q=QUERY&hl=en-IN&gl=IN
Gmail inbox     : https://mail.google.com/mail/u/0/#inbox
Gmail compose   : https://mail.google.com/mail/?view=cm&to=EMAIL&su=SUBJECT&body=BODY
Google Meet     : https://meet.google.com/new
LinkedIn feed   : https://www.linkedin.com/feed/
Dominos India   : https://www.dominos.co.in/
Microsoft Store : https://apps.microsoft.com/search?query=QUERY

━━ USER'S PERSONAL INFO ━━
YouTube channel "Dietichen" : https://www.youtube.com/channel/UCNcVMyq5JyZ5V_TXN6KUgbA
YouTube channel "20xAI"     : https://www.youtube.com/channel/UCjz0PNtjFD_1haoyZbYaX-g
YouTube Studio "Dietichen"  : https://studio.youtube.com/channel/UCNcVMyq5JyZ5V_TXN6KUgbA
YouTube Studio "20xAI"      : https://studio.youtube.com/channel/UCjz0PNtjFD_1haoyZbYaX-g
Games available: breakout, car arcade, obstacle course, space invaders, football champs, path finder, tic tac toe"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BRAIN  ─  Ollama agentic loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ── Brain configuration ──────────────────────────────────────────────────────
# Primary brain: Google Gemini (free tier) via its OpenAI-compatible endpoint —
# our TOOLS are already in OpenAI format, so they work as-is. Multiple API keys
# are rotated automatically when one hits its rate limit (HTTP 429). If every key
# is exhausted or the network is down, Jarvis falls back to the local Ollama
# model so it keeps working offline.
#
# Keys are loaded from (in order of priority):
#   1. the JARVIS_GEMINI_KEYS env var (comma-separated), or
#   2. a local, GIT-IGNORED file `jarvis_keys.txt` next to this script — one key
#      per line, blank lines and #-comments ignored.
# Keys are NEVER hardcoded here, so this file is safe to commit/share publicly.
# See jarvis_keys.txt.example for the format.
def _load_gemini_keys() -> list:
    env = [k.strip() for k in os.environ.get("JARVIS_GEMINI_KEYS", "").split(",") if k.strip()]
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_keys.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")]
    except FileNotFoundError:
        return []


GEMINI_KEYS = _load_gemini_keys()
# Free tier is ~20 generate-content requests/DAY *per key, per MODEL*. An
# agentic task spends many requests (one per tool-loop round), so the daily
# quota — not the per-minute rate — is the real ceiling. Because the quota is
# per model, gemini-2.5-flash and gemini-2.5-flash-lite each have their OWN
# 20/day pool: we try flash first, then fall through to flash-lite (free extra
# budget) before dropping to local Ollama. Override the whole list with
# JARVIS_GEMINI_MODELS (comma-separated) or a single model with
# JARVIS_GEMINI_MODEL.
GEMINI_MODELS = (
    [m.strip() for m in os.environ.get("JARVIS_GEMINI_MODELS", "").split(",") if m.strip()]
    or ([os.environ["JARVIS_GEMINI_MODEL"]] if os.environ.get("JARVIS_GEMINI_MODEL") else [])
    or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
)
GEMINI_MODEL = GEMINI_MODELS[0]   # primary (used for the startup reachability probe)
GEMINI_URL   = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OLLAMA_MODEL = "qwen2.5:7b"

_gemini_key_idx = 0          # index of the key that last worked — rotation starts here
_gemini_last_model = GEMINI_MODEL   # model that actually served the last request


def _sanitize_assistant(msg: dict) -> dict:
    """Keep only the fields the chat APIs accept, so a message returned by one
    provider can be safely re-sent to either provider on the next round."""
    clean = {"role": "assistant", "content": msg.get("content")}
    tcs = msg.get("tool_calls")
    if tcs:
        clean["tool_calls"] = [
            {
                "id":   tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name":      tc["function"]["name"],
                    "arguments": tc["function"].get("arguments") or "{}",
                },
            }
            for i, tc in enumerate(tcs)
        ]
    return clean


def _gemini_request(messages, tools, temperature) -> dict:
    """Call Gemini, rotating through keys on rate-limit (429) and retrying
    transient busy errors (503). When every key is rate-limited on the primary
    model, fall through to the next model in GEMINI_MODELS — each model has its
    own separate daily free quota, so this is genuinely extra free budget.
    Returns the assistant message dict (OpenAI format). Raises RuntimeError if
    no key/model combination could serve it (caller then falls back to Ollama)."""
    global _gemini_key_idx, _gemini_last_model
    if not GEMINI_KEYS:
        raise RuntimeError("no Gemini keys configured")
    n = len(GEMINI_KEYS)
    last_err = "unknown"
    for model in GEMINI_MODELS:
        for offset in range(n):
            idx = (_gemini_key_idx + offset) % n
            key = GEMINI_KEYS[idx]
            body = {"model": model, "messages": messages,
                    "tools": tools, "temperature": temperature}
            for _attempt in range(3):
                try:
                    r = requests.post(
                        GEMINI_URL,
                        headers={"Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json"},
                        json=body, timeout=60,
                    )
                except Exception as e:
                    raise RuntimeError(f"network error: {e}")  # offline → fall back now
                if r.status_code == 200:
                    _gemini_key_idx = idx          # remember the working key
                    _gemini_last_model = model     # …and the model that served it
                    _GEMINI_USAGE[(idx, model)] = _GEMINI_USAGE.get((idx, model), 0) + 1
                    _hud_quota()
                    return r.json()["choices"][0]["message"]
                if r.status_code == 503:
                    time.sleep(2)                  # model busy — retry same key
                    last_err = "503 busy"
                    continue
                if r.status_code == 429:
                    last_err = f"429 daily-limit (key {idx + 1}, {model})"
                    _GEMINI_USAGE[(idx, model)] = 20   # tapped out for the day → bar goes full
                    _hud_quota()
                    break                          # key tapped out — try the next key
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
                break                              # 401/403/400 — try the next key
        # every key exhausted for this model → fall through to the next model
    raise RuntimeError(f"all Gemini keys+models unavailable ({last_err})")


def _to_ollama_messages(messages):
    """Convert OpenAI-format messages to what the Ollama client expects. The key
    difference: OpenAI tool_calls store `arguments` as a JSON STRING, but Ollama's
    pydantic model requires a dict. Without this, falling back to Ollama after
    Gemini fails crashes with a validation error."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            fixed = []
            for tc in m["tool_calls"]:
                args = tc["function"].get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except Exception:
                        args = {}
                fixed.append({"function": {"name": tc["function"]["name"],
                                           "arguments": args}})
            m["tool_calls"] = fixed
        out.append(m)
    return out


def _ollama_request(messages, tools, temperature) -> dict:
    """Call the local Ollama model and return an OpenAI-format assistant dict."""
    resp = ollama.chat(
        model=OLLAMA_MODEL, messages=_to_ollama_messages(messages), tools=tools,
        options={"temperature": temperature},
    )
    m = resp.message
    out = {"role": "assistant", "content": m.content or ""}
    tcs = []
    for i, tc in enumerate(m.tool_calls or []):
        tcs.append({
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": tc.function.name,
                         "arguments": json.dumps(dict(tc.function.arguments))},
        })
    if tcs:
        out["tool_calls"] = tcs
    return out


def brain_chat(messages, tools, temperature):
    """Return (assistant_message_dict, brain_label). Tries Gemini first, then
    falls back to local Ollama."""
    try:
        msg = _gemini_request(messages, tools, temperature)
        _hud_emit("brain", provider="gemini", model=_gemini_last_model, key=_gemini_key_idx + 1)
        return _sanitize_assistant(msg), f"Gemini ({_gemini_last_model}, key {_gemini_key_idx + 1})"
    except Exception as e:
        print(f"   ⚠ Gemini unavailable ({e}) → using local Ollama")
        _hud_emit("toast", text="Gemini unavailable — switching to local Ollama", level="warn")
        _hud_emit("brain", provider="ollama", model=OLLAMA_MODEL)
        msg = _ollama_request(messages, tools, temperature)
        return _sanitize_assistant(msg), f"Ollama ({OLLAMA_MODEL})"


def process_command(user_input: str, history: list) -> tuple[str, list]:
    history.append({"role": "user", "content": user_input})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-20:]

    final = "Done."
    label_shown = False
    _hud_emit("state", state="thinking", sub="reasoning")
    for _ in range(10):  # max 10 tool rounds per command
        if _ABORT.is_set():
            history.append({"role": "assistant", "content": "Okay, stopped."})
            return "__ABORTED__", history

        try:
            _hud_emit("state", state="thinking", sub="reasoning")
            assistant, label = brain_chat(messages, TOOLS, 0.15)
        except Exception as e:
            err = f"AI error: {e}"
            history.append({"role": "assistant", "content": err})
            return err, history

        if _ABORT.is_set():           # user hit ESC while the model was thinking
            history.append({"role": "assistant", "content": "Okay, stopped."})
            return "__ABORTED__", history

        if not label_shown:
            print(f"   🧠 {label}")
            label_shown = True

        messages.append(assistant)

        content = (assistant.get("content") or "").strip()
        if content:
            final = content

        tool_calls = assistant.get("tool_calls")
        if not tool_calls:
            break

        for tc in tool_calls:
            if _ABORT.is_set():       # stop before running any further tools
                history.append({"role": "assistant", "content": "Okay, stopped."})
                return "__ABORTED__", history
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            _hud_emit("state", state="acting", sub=name.replace("_", " "))
            _hud_emit("tool", name=name, summary=_tool_summary(name, args), status="run")
            result = execute_tool(name, args)
            _status, _detail = _tool_outcome(result)
            _hud_emit("tool", name=name, status=_status, detail=_detail)
            preview = str(result)
            if len(preview) > 900:
                preview = preview[:900] + " …(truncated)"
            print(f"   ↳ {preview}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", "call_0"),
                "content": str(result),
            })

    history.append({"role": "assistant", "content": final})
    return final, history


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STARTUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _gemini_available() -> bool:
    """True if Gemini is reachable and at least one key authenticates. A 429/503
    still counts as available (working, just busy/limited — runtime rotation
    handles that). Only a network failure or all-keys-rejected means False."""
    if not GEMINI_KEYS:
        return False
    body = {"model": GEMINI_MODEL,
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    for key in GEMINI_KEYS:
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body, timeout=15,
            )
        except Exception:
            return False   # network down → offline, use Ollama
        if r.status_code in (200, 429, 503):
            return True
    return False


def ensure_ollama_running() -> bool:
    try:
        requests.get("http://localhost:11434", timeout=2)
        return True
    except Exception:
        pass
    print("Starting Ollama server...")
    subprocess.Popen(
        ["ollama", "serve"],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(12):
        time.sleep(1)
        try:
            requests.get("http://localhost:11434", timeout=2)
            return True
        except Exception:
            pass
    return False


def wish() -> None:
    hour = datetime.datetime.now().hour
    if hour < 12:
        speak("Good morning! Jarvis online.")
    elif hour < 18:
        speak("Good afternoon! Jarvis ready.")
    else:
        speak("Good evening! Jarvis at your service.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    # ── Launch the live HUD (web control center) ──────────────────────────────
    _hud_url = _start_hud()
    if _hud_url:
        try:
            webbrowser.open(_hud_url)
        except Exception:
            pass
    _hud_emit("state", state="standby", sub="booting")

    print("=" * 62)
    print("  JARVIS  —  AI Assistant")
    print(f"  Brain  : Gemini {' → '.join(GEMINI_MODELS)}  ({len(GEMINI_KEYS)} keys)  +  Ollama fallback")
    print("  Web    : Playwright Chromium  (DOM-based, reliable)")
    print("  Search : DuckDuckGo  (real-time, free)")
    print("  Apps   : pywinauto   (Windows accessibility API)")
    if _hud_url:
        print(f"  HUD    : {_hud_url}   (live control center — opening in browser)")
    print("  Say 'Jarvis stop' or 'Jarvis goodbye' to exit.")
    print("=" * 62)

    print("Checking AI brain...")
    if _gemini_available():
        _hud_emit("online", up=True)
        _hud_emit("brain", provider="gemini", model=GEMINI_MODEL, key=1)
        print(f"Brain ready: Gemini {GEMINI_MODEL} ({len(GEMINI_KEYS)} key(s)). "
              "Local Ollama armed as fallback.\n")
        # Start the Ollama server in the background so fallback is instant if
        # needed — but DON'T warm the model (saves ~4 GB VRAM while Gemini leads).
        try:
            ensure_ollama_running()
        except Exception:
            pass
    else:
        print("Gemini not reachable — using local Ollama brain.")
        _hud_emit("online", up=False)
        _hud_emit("brain", provider="ollama", model=OLLAMA_MODEL)
        if not ensure_ollama_running():
            speak("Could not reach any AI brain. Check your internet, or start Ollama.")
            exit(1)
        print("Loading model into GPU memory...")
        try:
            ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": "hi"}],
                options={"num_predict": 1},
            )
            print("Model ready.\n")
        except Exception:
            pass

    wish()

    print(_install_abort_hotkey())

    conversation_history: list = []
    WAKE_WORDS = ("jarvis ", "hey jarvis ", "ok jarvis ", "okay jarvis ")

    while True:
        query = takeCommand()
        if not query or query == "none":
            continue

        q = query.lower().strip()

        # ── Wake word gate ────────────────────────────────────────────────────
        # Ignore anything that doesn't contain "jarvis"
        if "jarvis" not in q:
            print("   (no wake word — ignored)")
            continue

        print("   ✅ Wake word detected")

        # ── Hard stop — no LLM round-trip ─────────────────────────────────────
        if any(p in q for p in ["jarvis stop", "jarvis goodbye", "jarvis bye",
                                  "jarvis goodnight", "jarvis exit", "jarvis quit"]):
            speak("Goodbye!")
            _hud_emit("toast", text="Jarvis signing off", level="info")
            _hud_emit("state", state="standby", sub="offline")
            try:
                if _pw_instance:
                    _pw_instance.stop()
            except Exception:
                pass
            break

        # ── Strip wake word so LLM gets clean intent ───────────────────────────
        clean = query.strip()
        for w in WAKE_WORDS:
            if clean.lower().startswith(w):
                clean = clean[len(w):].strip()
                break
        else:
            # "jarvis" appeared mid-sentence or with no space — strip it anyway
            clean = re.sub(r"(?i)\bjarvis\b[,\s]*", "", clean).strip()

        # Nothing left after stripping (e.g. user just said "Jarvis")
        if not clean:
            speak("Yes?")
            continue

        print(f"\n📨 Sending to AI: {clean}")
        _hud_emit("user", text=clean)
        _ABORT.clear()        # fresh task — ignore any stale stop signal
        response, conversation_history = process_command(clean, conversation_history)

        if response == "__ABORTED__":
            _ABORT.clear()    # consume the stop; ready for the next command
            print("⏹  Stopped.")
            _hud_emit("toast", text="Task stopped", level="warn")
            _hud_emit("state", state="standby", sub="stopped")
            speak("Okay, stopped. What should I do instead?")
            time.sleep(0.3)
            continue

        _hud_emit("task")     # one task completed → bump the counter
        speak(response)

        time.sleep(0.3)
