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
import base64
import datetime
import io
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
_TEXT_CMDS         = _queue.Queue()     # commands typed into the HUD → main loop


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

    def do_POST(self):
        if self.path == "/command":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                text = (body.get("text") or "").strip()
                if text:
                    _TEXT_CMDS.put(text)
            except Exception:
                pass
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
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
    {
        "type": "function",
        "function": {
            "name": "look_at_page",
            "description": (
                "SEE the current browser page with real computer vision (you have eyes). "
                "Use this to VISUALLY VERIFY the result of an action when the element list is "
                "not enough — e.g. after submitting, confirm a post actually published; read an "
                "error dialog, a CAPTCHA prompt, a chart, or any visual content the DOM text "
                "doesn't capture. Ask a specific question about what is on screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string",
                        "description": "What you want to know about the page, e.g. 'Did my post publish successfully? What does the confirmation say?'"}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": (
                "SEE the user's entire desktop screen with real computer vision. Use when the "
                "user asks about what is on their screen, to read an on-screen error/message, "
                "describe an image or app, or understand visual context outside the browser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string",
                        "description": "What to look for or answer about the screen."}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a durable fact about the user to long-term memory so you recall it in "
                "future conversations — preferences, names, accounts, recurring tasks, how they "
                "like things done. Use ONLY for lasting facts, never for one-off chit-chat. "
                "Relevant memories are surfaced to you automatically before each command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string",
                        "description": "The fact to remember, written as a standalone sentence, e.g. 'The user prefers concise answers and lives in Vellore.'"}
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Search your long-term memory for things you know about the user that relate to "
                "a topic. Use when you need older context that wasn't already surfaced to you."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up in memory."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_by_vision",
            "description": (
                "Click something on the current browser page by DESCRIBING it visually, when "
                "it is NOT in the numbered element list (e.g. an image, a canvas control, an "
                "icon with no label, a map pin). Jarvis looks at the page and clicks where the "
                "thing is. Prefer browser_click with a number when the element IS listed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string",
                        "description": "What to click, described visually, e.g. 'the blue circular play button in the centre of the video'."}
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a place (free, no key). Use for weather questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City or place name, e.g. 'Vellore' or 'Tokyo'."}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Set a reminder/timer. Jarvis will speak the reminder out loud when it's due. "
                "Convert the user's phrasing into delay_seconds (e.g. 'in 10 minutes' → 600, "
                "'in 2 hours' → 7200)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to remind the user about."},
                    "delay_seconds": {"type": "integer", "description": "Seconds from now until the reminder fires."}
                },
                "required": ["text", "delay_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard",
            "description": (
                "Read whatever text the user currently has copied to their clipboard. Use when "
                "they say things like 'summarise what I copied', 'translate this', 'what does "
                "this mean' referring to copied text."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_file",
            "description": (
                "Read a local document (.txt .md .csv .pdf .docx or code) and answer a question "
                "about its contents. Use when the user asks about a file by path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full path to the file."},
                    "question": {"type": "string", "description": "What the user wants to know about it."}
                },
                "required": ["path", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression (arithmetic, powers, sqrt, sin/cos/log, etc.). Use for any calculation instead of doing mental math.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. '(45*1.18) + sqrt(144)' or '2**10'."}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get current news headlines (free). Optionally about a topic. Use for 'what's the news', 'latest on X'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional topic, e.g. 'AI' or 'cricket'. Omit for top headlines."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_briefing",
            "description": "Give a spoken briefing: greeting, date/time, weather, top headlines and pending reminders. Use for 'brief me' / 'what's my morning look like'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City for the weather part (use what you know about the user if available)."},
                    "topic": {"type": "string", "description": "Optional news topic focus."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_plan",
            "description": (
                "At the START of a multi-step task, declare your plan as a list of short steps. "
                "It shows on the user's HUD as a live checklist. Then call complete_step after "
                "finishing each step. Use for tasks with 3+ steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {"type": "array", "items": {"type": "string"},
                              "description": "Ordered short step descriptions (max 8)."}
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_step",
            "description": "Mark the plan step at the given index (0-based) as complete. Call right after finishing that step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "0-based index of the step just completed."}
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": (
                "Scroll the current page when what you need is below/above the fold. Returns a "
                "fresh snapshot of the now-visible elements. Use if a snapshot didn't show the "
                "element you expected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"],
                                  "description": "Which way to scroll."}
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tab",
            "description": "Manage browser tabs: open a new tab (optionally at a URL), list open tabs, or switch to a tab by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["new", "list", "switch", "close"]},
                    "url": {"type": "string", "description": "URL for action=new (optional)."},
                    "index": {"type": "integer", "description": "Tab index for switch/close."}
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create/overwrite a local file with text content (.txt .md .csv code, or .docx). Use to save notes, drafts, code or documents the user asks you to write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full destination path."},
                    "content": {"type": "string", "description": "The full file contents."}
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount between currencies using live free exchange rates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "from_currency": {"type": "string", "description": "3-letter code, e.g. USD."},
                    "to_currency": {"type": "string", "description": "3-letter code, e.g. INR."}
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": "Control system media playback (Spotify, YouTube, any player) via media keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["play", "pause", "next", "previous", "stop", "volup", "voldown", "mute"]}
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch a specific web page/article by URL and summarise it or answer a question about it. Use when the user gives a link or says 'summarise this article'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "question": {"type": "string", "description": "Optional specific question; omit to summarise."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_price",
            "description": "Get the live price of a cryptocurrency (free).",
            "parameters": {
                "type": "object",
                "properties": {
                    "coin": {"type": "string", "description": "e.g. 'bitcoin', 'eth', 'solana'."},
                    "currency": {"type": "string", "description": "fiat code, default usd."}
                },
                "required": ["coin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "define_word",
            "description": "Get the dictionary definition of a word (free).",
            "parameters": {
                "type": "object",
                "properties": {"word": {"type": "string"}},
                "required": ["word"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_units",
            "description": "Convert between units of length, mass, volume, speed, data, or temperature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number"},
                    "from_unit": {"type": "string", "description": "e.g. 'km', 'lb', 'celsius'."},
                    "to_unit": {"type": "string", "description": "e.g. 'miles', 'kg', 'fahrenheit'."}
                },
                "required": ["value", "from_unit", "to_unit"],
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
            # Self-heal: text matching failed → fall back to vision (Set-of-Marks).
            try:
                _hud_emit("toast", text="👁 click self-healing via vision…", level="info")
                vres = _vision_click(page, text)
                if vres.startswith("Saw and clicked"):
                    return vres
            except Exception:
                pass
            return (
                f"Could not find '{text}' to click, even with vision. Call browser_snapshot and "
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

    # ── look_at_page ── (vision: see the live browser page) ─────────────────────
    elif name == "look_at_page":
        question = args.get("question", "What is on this page?")
        try:
            page = _get_page()
            png = page.screenshot(type="png")
            _hud_emit("toast", text="👁 Looking at the page…", level="info")
            return _gemini_vision(question, base64.b64encode(png).decode(), "image/png")
        except Exception as e:
            return f"Vision error (page): {e}"

    # ── look_at_screen ── (vision: see the whole desktop) ───────────────────────
    elif name == "look_at_screen":
        question = args.get("question", "What is on the screen?")
        try:
            b64, mime = _grab_screen_b64()
            _hud_emit("toast", text="👁 Looking at your screen…", level="info")
            return _gemini_vision(question, b64, mime)
        except Exception as e:
            return f"Vision error (screen): {e}"

    # ── remember ── (write to long-term semantic memory) ────────────────────────
    elif name == "remember":
        fact = (args.get("fact") or "").strip()
        if not fact:
            return "Nothing to remember (empty fact)."
        ok = _mem_add(fact)
        _hud_emit("toast", text="🧠 Remembered", level="ok")
        _hud_emit("memory", count=len(_MEM))
        return "Saved to long-term memory." if ok else "Couldn't reach the memory embedder; not saved."

    # ── recall ── (semantic search of long-term memory) ─────────────────────────
    elif name == "recall":
        query = (args.get("query") or "").strip()
        hits = _mem_search(query, k=5)
        if not hits:
            return "I don't have anything in memory about that yet."
        return "From memory:\n" + "\n".join(f"• {t}" for t, _ in hits)

    # ── click_by_vision ── (Set-of-Marks: model picks a marked element to click) ─
    elif name == "click_by_vision":
        desc = args.get("description", "")
        try:
            _hud_emit("toast", text="👁 locating by vision…", level="info")
            return _vision_click(_get_page(), desc)
        except Exception as e:
            return f"Vision-click error: {e}"

    # ── get_weather ─────────────────────────────────────────────────────────────
    elif name == "get_weather":
        return _get_weather(args.get("location", ""))

    # ── set_reminder ────────────────────────────────────────────────────────────
    elif name == "set_reminder":
        text = (args.get("text") or "").strip() or "your reminder"
        try:
            delay = int(args.get("delay_seconds", 0))
        except Exception:
            delay = 0
        if delay <= 0:
            return "I need a valid time for the reminder."
        return _set_reminder(text, delay)

    # ── read_clipboard ──────────────────────────────────────────────────────────
    elif name == "read_clipboard":
        clip = _read_clipboard()
        if not clip.strip():
            return "The clipboard is empty (or contains no text)."
        return f"Clipboard contents:\n{clip[:4000]}"

    # ── ask_file ──────────────────────────────────────────────────────────────--
    elif name == "ask_file":
        return _ask_file(args.get("path", ""), args.get("question", "Summarise this file."))

    # ── calculate ───────────────────────────────────────────────────────────────
    elif name == "calculate":
        return _calculate(args.get("expression", ""))

    # ── get_news ──────────────────────────────────────────────────────────────--
    elif name == "get_news":
        heads = _get_news(args.get("topic", ""), n=6)
        return ("Headlines:\n" + "\n".join(f"• {h}" for h in heads)) if heads else "Couldn't fetch news right now."

    # ── daily_briefing ──────────────────────────────────────────────────────────
    elif name == "daily_briefing":
        return _compose_briefing(args.get("location", ""), args.get("topic", ""))

    # ── set_plan / complete_step ────────────────────────────────────────────────
    elif name == "set_plan":
        return _set_plan(args.get("steps", []))
    elif name == "complete_step":
        return _complete_step(args.get("index", 0))

    # ── browser_scroll ──────────────────────────────────────────────────────────
    elif name == "browser_scroll":
        d = (args.get("direction") or "down").lower()
        try:
            page = _get_page()
            js = {"down": "window.scrollBy(0, window.innerHeight*0.85)",
                  "up": "window.scrollBy(0, -window.innerHeight*0.85)",
                  "top": "window.scrollTo(0, 0)",
                  "bottom": "window.scrollTo(0, document.body.scrollHeight)"}.get(d,
                  "window.scrollBy(0, window.innerHeight*0.85)")
            page.evaluate(js)
            _settle(page, idle_ms=1500, pause=0.4)
            snap = execute_tool("browser_snapshot", {})
            return f"Scrolled {d}. Page now:\n\n{snap}"
        except Exception as e:
            return f"Scroll error: {e}"

    # ── browser_tab ──────────────────────────────────────────────────────────---
    elif name == "browser_tab":
        global _pw_page
        action = (args.get("action") or "list").lower()
        try:
            page = _get_page()
            ctx = page.context
            if action == "new":
                np = ctx.new_page()
                _pw_page = np
                url = args.get("url", "")
                if url:
                    if not url.startswith("http"):
                        url = "https://" + url
                    np.goto(url, wait_until="domcontentloaded", timeout=25000)
                    _settle(np)
                return f"Opened a new tab.\n\n{execute_tool('browser_snapshot', {})}"
            pages = [p for p in ctx.pages if not p.is_closed()]
            if action == "list":
                return "Open tabs:\n" + "\n".join(
                    f"  [{i}] {p.title()[:50]}  ({p.url[:60]})" for i, p in enumerate(pages))
            idx = int(args.get("index", 0))
            if not (0 <= idx < len(pages)):
                return f"No tab [{idx}]. There are {len(pages)} tabs."
            if action == "switch":
                _pw_page = pages[idx]
                _pw_page.bring_to_front()
                return f"Switched to tab [{idx}].\n\n{execute_tool('browser_snapshot', {})}"
            if action == "close":
                pages[idx].close()
                _pw_page = None
                return f"Closed tab [{idx}]."
            return f"Unknown tab action '{action}'."
        except Exception as e:
            return f"Tab error: {e}"

    # ── write_file ──────────────────────────────────────────────────────────────
    elif name == "write_file":
        return _write_file(args.get("path", ""), args.get("content", ""))

    # ── convert_currency ─────────────────────────────────────────────────────────
    elif name == "convert_currency":
        try:
            amt = float(args.get("amount", 0))
        except Exception:
            amt = 0.0
        return _convert_currency(amt, args.get("from_currency", ""), args.get("to_currency", ""))

    # ── media_control ─────────────────────────────────────────────────────────--
    elif name == "media_control":
        return _media_control(args.get("action", ""))

    # ── read_url ─────────────────────────────────────────────────────────────--
    elif name == "read_url":
        return _read_url(args.get("url", ""), args.get("question", ""))

    # ── get_crypto_price ─────────────────────────────────────────────────────---
    elif name == "get_crypto_price":
        return _get_crypto_price(args.get("coin", ""), args.get("currency", "usd"))

    # ── define_word ──────────────────────────────────────────────────────────---
    elif name == "define_word":
        return _define_word(args.get("word", ""))

    # ── convert_units ────────────────────────────────────────────────────────---
    elif name == "convert_units":
        try:
            val = float(args.get("value", 0))
        except Exception:
            val = 0.0
        return _convert_units(val, args.get("from_unit", ""), args.get("to_unit", ""))

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
See / read the user's screen            → look_at_screen
Visually verify a web action worked     → look_at_page
Click an unlabelled/visual element      → click_by_vision
Save a lasting fact about the user      → remember
Look up older context about the user    → recall
Weather for a place                     → get_weather
News headlines                          → get_news
A spoken briefing of the day            → daily_briefing
Any math / calculation                  → calculate
Set a reminder / timer                  → set_reminder
Use text the user has copied            → read_clipboard
Answer questions about a local file     → ask_file
Save/write a file or document           → write_file
Convert currency                        → convert_currency
Play/pause/next music or volume         → media_control
Reveal off-screen page content          → browser_scroll
Work across multiple tabs               → browser_tab
Summarise an article / read a link      → read_url
Crypto price                            → get_crypto_price
Define a word                           → define_word
Convert units (length/mass/temp/…)      → convert_units

━━ SHOW YOUR PLAN ━━
For any task with 3 or more steps, FIRST call set_plan with the ordered steps — it
appears on the user's HUD as a live checklist. Call complete_step(index) right after
each step finishes so they can watch progress. Keep steps short.

━━ YOU HAVE EYES (vision) ━━
You can SEE. After an important web action whose success isn't obvious from the element
list — submitting a form, publishing a post, a payment/confirmation screen — call
look_at_page to VISUALLY CONFIRM it actually worked before telling the user it's done.
Use look_at_screen when the user asks about anything on their screen. This works on any
site or app; never assume success, verify it.

━━ YOU HAVE MEMORY ━━
Durable facts you should recall later (preferences, names, accounts, how the user likes
things done, recurring tasks) → call remember. Relevant memories are automatically given
to you before each command under "THINGS YOU REMEMBER"; use them naturally. Use recall
for older context that wasn't surfaced. Do NOT remember trivial one-off chit-chat.

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
            body = {"model": model, "messages": messages, "temperature": temperature}
            if tools:
                body["tools"] = tools
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  VISION  ─  Gemini multimodal (Jarvis can see)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _gemini_vision(prompt: str, image_b64: str, mime: str = "image/png") -> str:
    """Ask Gemini a question about an image. Uses the same key/model rotation as
    the text brain (vision works on the free tier). Returns the answer text."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
    ]}]
    msg = _gemini_request(messages, None, 0.2)   # tools omitted for pure vision
    return (msg.get("content") or "").strip() or "(no description returned)"


def _grab_screen_b64(max_w: int = 1280):
    """Capture the desktop, downscale, and return (base64_jpeg, mime)."""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def _vision_click(page, description: str) -> str:
    """Click an element by VISUAL description using Set-of-Marks prompting: every
    interactive element is tagged + drawn with its number on a screenshot, then
    Gemini picks WHICH number matches the description (reliable — it chooses among
    labelled candidates instead of guessing raw pixels). We click that element by
    its data-jarvis-id, so the click itself is exact. Great for icon buttons,
    images and controls whose text label is missing or unhelpful."""
    from PIL import Image, ImageDraw
    _index_interactive(page)   # tag every visible interactive element with data-jarvis-id
    rects = page.evaluate(
        "() => { const d = window.devicePixelRatio || 1;"
        " return [...document.querySelectorAll('[data-jarvis-id]')].map(e=>{"
        "  const r=e.getBoundingClientRect();"
        "  return {id:+e.getAttribute('data-jarvis-id'),"
        "          x:(r.x+r.width/2)*d, y:(r.y+r.height/2)*d,"
        "          on:(r.width>1&&r.height>1&&r.bottom>0&&r.top<innerHeight)};})"
        " .filter(o=>o.on); }"
    )
    if not rects:
        return "Nothing visible to click on this page."
    png = page.screenshot(type="png")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for o in rects:                       # draw numbered markers (Set-of-Marks)
        x, y = o["x"], o["y"]
        draw.ellipse([x - 14, y - 11, x + 14, y + 11], fill=(255, 40, 90))
        draw.text((x - 4 * len(str(o["id"])), y - 6), str(o["id"]), fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, "PNG")
    prompt = (
        "Each clickable element on this page is marked with a pink numbered badge. "
        f"Which badge number is on: \"{description}\"? "
        "Reply with ONLY JSON: {\"id\": <number>, \"found\": true} or {\"found\": false} if none match."
    )
    ans = _gemini_vision(prompt, base64.b64encode(buf.getvalue()).decode(), "image/png")
    mt = re.search(r"\{.*\}", ans, re.S)
    data = json.loads(mt.group(0)) if mt else {}
    if not data.get("found") or "id" not in data:
        return f"I looked but couldn't visually identify '{description}'. Try browser_snapshot for the numbered list."
    idx = int(data["id"])
    loc = page.locator(f"[data-jarvis-id='{idx}']")
    if loc.count() == 0:
        return f"Vision picked element [{idx}] but it's no longer on the page. Re-snapshot and retry."
    loc.first.click(timeout=5000)
    _settle(page)
    snap = execute_tool("browser_snapshot", {})
    return f"Saw and clicked '{description}' (element [{idx}]). Page now:\n\n{snap}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LONG-TERM MEMORY  ─  free Gemini embeddings + local vector store
#  Jarvis embeds and stores durable facts; before each command the most
#  relevant ones are recalled by semantic similarity and fed back to the brain.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GEMINI_EMBED_URL   = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
GEMINI_EMBED_MODEL = os.environ.get("JARVIS_EMBED_MODEL", "gemini-embedding-001")
GEMINI_EMBED_DIM   = 768   # compact, fast cosine; the model supports custom dims
_MEM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_memory.json")
_MEM = []   # [{"text":str, "ts":str, "vec":[float]}]


def _gemini_embed(texts):
    """Embed a list of strings via the free embeddings endpoint (key rotation).
    Returns a list of vectors, or [] on failure."""
    global _gemini_key_idx
    if not GEMINI_KEYS:
        return []
    n = len(GEMINI_KEYS)
    body = {"model": GEMINI_EMBED_MODEL, "input": texts, "dimensions": GEMINI_EMBED_DIM}
    for offset in range(n):
        idx = (_gemini_key_idx + offset) % n
        try:
            r = requests.post(
                GEMINI_EMBED_URL,
                headers={"Authorization": f"Bearer {GEMINI_KEYS[idx]}", "Content-Type": "application/json"},
                json=body, timeout=30,
            )
        except Exception:
            return []
        if r.status_code == 200:
            return [d["embedding"] for d in r.json()["data"]]
        if r.status_code == 429:
            continue          # this key's embed quota is spent — try the next
        return []             # other error — give up quietly
    return []


def _cosine(a, b):
    dot = s1 = s2 = 0.0
    for x, y in zip(a, b):
        dot += x * y; s1 += x * x; s2 += y * y
    return dot / (math.sqrt(s1) * math.sqrt(s2) + 1e-9)


def _mem_load():
    global _MEM
    try:
        with open(_MEM_PATH, encoding="utf-8") as f:
            _MEM = json.load(f)
    except Exception:
        _MEM = []


def _mem_save():
    try:
        with open(_MEM_PATH, "w", encoding="utf-8") as f:
            json.dump(_MEM, f)
    except Exception:
        pass


def _mem_add(fact: str) -> bool:
    """Embed and store a fact. Skips near-duplicates. Returns True on success."""
    vecs = _gemini_embed([fact])
    if not vecs:
        return False
    vec = vecs[0]
    for m in _MEM:                               # de-dupe very similar facts
        if m.get("vec") and _cosine(vec, m["vec"]) > 0.95:
            m["text"], m["vec"] = fact, vec
            _mem_save()
            return True
    _MEM.append({"text": fact, "ts": datetime.datetime.now().isoformat(timespec="seconds"), "vec": vec})
    _mem_save()
    return True


def _mem_search(query: str, k: int = 4, threshold: float = 0.30):
    """Return up to k (text, score) memories most similar to query, above threshold."""
    if not _MEM or not query.strip():
        return []
    qv = _gemini_embed([query])
    if not qv:
        return []
    qv = qv[0]
    scored = [(m["text"], _cosine(qv, m["vec"])) for m in _MEM if m.get("vec")]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(t, s) for t, s in scored[:k] if s >= threshold]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PASSIVE LEARNING  ─  auto-extract durable facts after each exchange
#  Uses the LOCAL Ollama model so it costs ZERO Gemini quota, then stores
#  any facts via the free embeddings memory above. Jarvis learns silently.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_LEARN_PROMPT = (
    "You extract durable, long-term facts about the USER from a conversation turn. "
    "Return ONLY a JSON array of short standalone sentences (max 3) capturing lasting "
    "facts worth remembering: preferences, identity, accounts, location, recurring needs, "
    "how they like things done. IGNORE one-off task details, questions, and chit-chat. "
    "If nothing is durable, return []. No prose, JSON array only.\n\n"
    "USER: {user}\nASSISTANT: {asst}"
)


def _extract_facts(user_text: str, asst_text: str):
    """Ask the local model for durable facts. Returns a list[str] (possibly empty)."""
    prompt = _LEARN_PROMPT.format(user=user_text[:600], asst=asst_text[:600])
    raw = ""
    try:
        resp = ollama.chat(model=OLLAMA_MODEL,
                           messages=[{"role": "user", "content": prompt}],
                           options={"temperature": 0.1})
        raw = (resp.message.content or "").strip()
    except Exception:
        return []
    m = re.search(r"\[.*\]", raw, re.S)        # pull the JSON array out of any wrapper
    if not m:
        return []
    try:
        facts = json.loads(m.group(0))
    except Exception:
        return []
    return [str(f).strip() for f in facts if isinstance(f, (str,)) and str(f).strip()][:3]


def _learn_async(user_text: str, asst_text: str) -> None:
    """Background passive-learning pass — never blocks the main loop."""
    def _run():
        try:
            facts = _extract_facts(user_text, asst_text)
            n = 0
            for f in facts:
                if _mem_add(f):
                    n += 1
                    print(f"   🧠 learned: {f}")
            if n:
                _hud_emit("toast", text=f"🧠 learned {n} new fact(s)", level="ok")
                _hud_emit("memory", count=len(_MEM))
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SYSTEM VITALS  ─  live CPU / RAM / GPU / battery telemetry → HUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        u, mu, mt, t = [x.strip() for x in out.stdout.strip().split("\n")[0].split(",")]
        return {"util": float(u), "mem_used": float(mu), "mem_total": float(mt), "temp": float(t)}
    except Exception:
        return None


def _vitals() -> dict:
    import psutil
    v = {"cpu": round(psutil.cpu_percent()), "ram": round(psutil.virtual_memory().percent)}
    try:
        b = psutil.sensors_battery()
        if b is not None:
            v["batt"] = round(b.percent)
            v["plugged"] = bool(b.power_plugged)
    except Exception:
        pass
    g = _gpu_stats()
    if g:
        v["gpu"] = round(g["util"])
        v["gpu_mem"] = round(g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] else 0
        v["gpu_temp"] = round(g["temp"])
    return v


def _vitals_loop() -> None:
    try:
        import psutil
        psutil.cpu_percent()      # prime the first reading
    except Exception:
        return
    while True:
        try:
            _hud_emit("vitals", **_vitals())
        except Exception:
            pass
        time.sleep(2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WEATHER  ─  free, no API key (Open-Meteo)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_WMO = {0:"clear sky",1:"mainly clear",2:"partly cloudy",3:"overcast",45:"fog",48:"rime fog",
    51:"light drizzle",53:"drizzle",55:"dense drizzle",61:"light rain",63:"rain",65:"heavy rain",
    66:"freezing rain",71:"light snow",73:"snow",75:"heavy snow",77:"snow grains",80:"light showers",
    81:"showers",82:"violent showers",85:"snow showers",86:"heavy snow showers",95:"thunderstorm",
    96:"thunderstorm w/ hail",99:"severe thunderstorm"}


def _get_weather(location: str) -> str:
    try:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": location, "count": 1}, timeout=10).json()
        if not g.get("results"):
            return f"Couldn't find a place called '{location}'."
        r = g["results"][0]
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": r["latitude"], "longitude": r["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "timezone": "auto"}, timeout=10).json()
        c = w["current"]
        desc = _WMO.get(c.get("weather_code"), "unknown conditions")
        place = ", ".join(x for x in (r.get("name"), r.get("country")) if x)
        return (f"{place}: {c['temperature_2m']}°C and {desc}, feels like "
                f"{c['apparent_temperature']}°C, humidity {c['relative_humidity_2m']}%, "
                f"wind {c['wind_speed_10m']} km/h.")
    except Exception as e:
        return f"Weather error: {e}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REMINDERS / TIMERS  ─  a background scheduler that speaks when due
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_REM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_reminders.json")
_REMINDERS = []      # [{"text":str, "at":epoch_float}]
_REM_LOCK = threading.Lock()


def _rem_load():
    global _REMINDERS
    try:
        with open(_REM_PATH, encoding="utf-8") as f:
            _REMINDERS = json.load(f)
    except Exception:
        _REMINDERS = []


def _rem_save():
    try:
        with open(_REM_PATH, "w", encoding="utf-8") as f:
            json.dump(_REMINDERS, f)
    except Exception:
        pass


def _emit_reminders():
    with _REM_LOCK:
        rows = sorted(({"text": r["text"], "at": r["at"]} for r in _REMINDERS), key=lambda r: r["at"])
    _hud_emit("reminders", items=rows)


def _set_reminder(text: str, delay_seconds: int) -> str:
    when = time.time() + max(1, int(delay_seconds))
    with _REM_LOCK:
        _REMINDERS.append({"text": text, "at": when})
        _rem_save()
    _emit_reminders()
    mins = max(1, round(delay_seconds / 60))
    _hud_emit("toast", text=f"⏰ reminder set ({mins} min)", level="ok")
    return f"Reminder set. I'll remind you in about {mins} minute(s)."


def _reminder_loop() -> None:
    while True:
        now = time.time()
        due = []
        with _REM_LOCK:
            keep = []
            for r in _REMINDERS:
                (due if r["at"] <= now else keep).append(r)
            if due:
                _REMINDERS[:] = keep
                _rem_save()
        if due:
            _emit_reminders()
        for r in due:
            _hud_emit("toast", text=f"⏰ {r['text']}", level="warn")
            try:
                speak(f"Reminder: {r['text']}")
            except Exception:
                pass
        time.sleep(3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLIPBOARD + LOCAL FILE Q&A
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _read_clipboard() -> str:
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip()
        except Exception:
            return ""


def _read_file_text(path: str, limit: int = 14000) -> str:
    path = os.path.expanduser(path.strip().strip('"'))
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
    elif ext == ".docx":
        import docx
        txt = "\n".join(p.text for p in docx.Document(path).paragraphs)
    else:                                   # txt / md / csv / code / etc.
        with open(path, encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    return txt[:limit]


def _ask_file(path: str, question: str) -> str:
    try:
        text = _read_file_text(path)
    except FileNotFoundError:
        return f"I can't find the file: {path}"
    except Exception as e:
        return f"Couldn't read that file: {e}"
    if not text.strip():
        return "That file appears to be empty or unreadable (maybe a scanned PDF — try look_at_screen)."
    messages = [
        {"role": "system", "content": "Answer the user's question using ONLY the document below. Be concise and spoken-friendly. If the answer isn't in it, say so.\n\nDOCUMENT:\n" + text},
        {"role": "user", "content": question},
    ]
    try:
        return (_gemini_request(messages, None, 0.2).get("content") or "").strip()
    except Exception as e:
        return f"Couldn't analyse the file: {e}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CALCULATOR  ─  safe arithmetic / math (local, instant, no quota)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import ast as _ast
import operator as _op

_CALC_BIN = {_ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul, _ast.Div: _op.truediv,
             _ast.Pow: _op.pow, _ast.Mod: _op.mod, _ast.FloorDiv: _op.floordiv}
_CALC_UN = {_ast.USub: _op.neg, _ast.UAdd: _op.pos}
_CALC_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}
_CALC_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
               "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
               "round": round, "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
               "radians": math.radians, "degrees": math.degrees, "min": min, "max": max}


def _calc_eval(node):
    if isinstance(node, _ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numbers allowed")
    if isinstance(node, _ast.BinOp) and type(node.op) in _CALC_BIN:
        return _CALC_BIN[type(node.op)](_calc_eval(node.left), _calc_eval(node.right))
    if isinstance(node, _ast.UnaryOp) and type(node.op) in _CALC_UN:
        return _CALC_UN[type(node.op)](_calc_eval(node.operand))
    if isinstance(node, _ast.Name) and node.id in _CALC_NAMES:
        return _CALC_NAMES[node.id]
    if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id in _CALC_FUNCS:
        return _CALC_FUNCS[node.func.id](*[_calc_eval(a) for a in node.args])
    raise ValueError("unsupported expression")


def _calculate(expr: str) -> str:
    try:
        val = _calc_eval(_ast.parse(expr.strip(), mode="eval").body)
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        return f"{expr.strip()} = {val}"
    except Exception:
        return f"I couldn't compute '{expr}'. Try a plain arithmetic expression."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NEWS  ─  free headlines via Google News RSS (no key)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_news(topic: str = "", n: int = 6) -> list:
    import xml.etree.ElementTree as ET
    if topic.strip():
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(topic) + "&hl=en-US&gl=US&ceid=US:en"
    else:
        url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    try:
        xml = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
        root = ET.fromstring(xml)
        items = root.findall(".//item")
        return [it.findtext("title", "").strip() for it in items[:n] if it.findtext("title")]
    except Exception:
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DAILY BRIEFING  ─  greeting + weather + headlines + pending reminders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _compose_briefing(location: str = "", topic: str = "") -> str:
    hour = datetime.datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    parts = [f"{greet}. It's {datetime.datetime.now().strftime('%A, %B %d, %I:%M %p')}."]
    if location:
        w = _get_weather(location)
        if w and not w.lower().startswith(("weather error", "couldn't")):
            parts.append("Weather: " + w)
    heads = _get_news(topic, n=4)
    if heads:
        parts.append("Top headlines: " + "; ".join(heads) + ".")
    with _REM_LOCK:
        pending = len(_REMINDERS)
    if pending:
        parts.append(f"You have {pending} reminder(s) pending.")
    return " ".join(parts)


# Proactive routines: optional jarvis_routines.json like
#   [{"time": "08:00", "location": "Vellore", "topic": "AI"}]
# fires a spoken briefing once when the local clock reaches that HH:MM.
_ROUTINES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_routines.json")


def _routine_loop() -> None:
    fired_today = set()
    last_day = None
    while True:
        try:
            now = datetime.datetime.now()
            if now.day != last_day:
                fired_today.clear(); last_day = now.day      # reset each day
            try:
                with open(_ROUTINES_PATH, encoding="utf-8") as f:
                    routines = json.load(f)
            except Exception:
                routines = []
            hhmm = now.strftime("%H:%M")
            for i, r in enumerate(routines):
                if r.get("time") == hhmm and i not in fired_today:
                    fired_today.add(i)
                    _hud_emit("toast", text="🌅 daily briefing", level="info")
                    speak(_compose_briefing(r.get("location", ""), r.get("topic", "")))
        except Exception:
            pass
        time.sleep(20)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PLAN  ─  the model declares a step plan; the HUD shows live progress
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_PLAN = {"steps": [], "done": 0}


def _set_plan(steps) -> str:
    steps = [str(s).strip() for s in (steps or []) if str(s).strip()][:8]
    _PLAN["steps"], _PLAN["done"] = steps, 0
    _hud_emit("plan", steps=steps, done=0)
    return "Plan set." if steps else "Empty plan."


def _complete_step(index) -> str:
    try:
        i = int(index)
    except Exception:
        i = _PLAN["done"]
    _PLAN["done"] = max(_PLAN["done"], i + 1)
    _hud_emit("plan", steps=_PLAN["steps"], done=_PLAN["done"])
    return "Step marked done."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WRITE FILES  ·  CURRENCY  ·  MEDIA KEYS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _write_file(path: str, content: str) -> str:
    """Save text/markdown/code (or a .docx) to disk. Creates parent dirs."""
    path = os.path.expanduser(path.strip().strip('"'))
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if path.lower().endswith(".docx"):
            import docx
            doc = docx.Document()
            for line in content.split("\n"):
                doc.add_paragraph(line)
            doc.save(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return f"Saved {len(content)} characters to {path}."
    except Exception as e:
        return f"Couldn't write the file: {e}"


def _convert_currency(amount: float, frm: str, to: str) -> str:
    frm, to = frm.upper().strip(), to.upper().strip()
    try:
        data = requests.get(f"https://open.er-api.com/v6/latest/{frm}", timeout=10).json()
        rate = (data.get("rates") or {}).get(to)
        if not rate:
            return f"Couldn't get a {frm}->{to} rate."
        return f"{amount} {frm} = {round(amount * rate, 2)} {to} (rate {round(rate, 4)})."
    except Exception as e:
        return f"Currency error: {e}"


def _read_url(url: str, question: str = "") -> str:
    """Fetch a web page, extract readable text, and summarise/answer with Gemini."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        html = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception as e:
        return f"Couldn't fetch that page: {e}"
    html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:12000]
    if not text:
        return "That page had no readable text."
    task = question.strip() or "Summarise this page in 3-4 spoken-friendly sentences."
    messages = [
        {"role": "system", "content": "Use ONLY the page text below. Be concise and spoken-friendly.\n\nPAGE:\n" + text},
        {"role": "user", "content": task},
    ]
    try:
        return (_gemini_request(messages, None, 0.3).get("content") or "").strip()
    except Exception as e:
        return f"Couldn't analyse the page: {e}"


_COIN_IDS = {"btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum",
             "sol": "solana", "solana": "solana", "doge": "dogecoin", "dogecoin": "dogecoin",
             "ada": "cardano", "xrp": "ripple", "bnb": "binancecoin", "matic": "matic-network",
             "ltc": "litecoin", "dot": "polkadot", "shib": "shiba-inu"}


def _get_crypto_price(coin: str, currency: str = "usd") -> str:
    cid = _COIN_IDS.get(coin.lower().strip(), coin.lower().strip().replace(" ", "-"))
    cur = (currency or "usd").lower().strip()
    try:
        d = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": cid, "vs_currencies": cur, "include_24hr_change": "true"},
                         timeout=10).json()
        if cid not in d:
            return f"Couldn't find a crypto called '{coin}'."
        price = d[cid][cur]
        chg = d[cid].get(f"{cur}_24h_change")
        extra = f", {chg:+.1f}% in 24h" if isinstance(chg, (int, float)) else ""
        return f"{cid.capitalize()} is {price:,} {cur.upper()}{extra}."
    except Exception as e:
        return f"Crypto price error: {e}"


def _define_word(word: str) -> str:
    word = word.strip()
    try:
        d = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}",
                         timeout=10).json()
        if not isinstance(d, list):
            return f"I couldn't find a definition for '{word}'."
        out = []
        for meaning in d[0].get("meanings", [])[:3]:
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])
            if defs:
                out.append(f"({pos}) {defs[0].get('definition','')}")
        return f"{word}: " + " ".join(out) if out else f"No definition found for '{word}'."
    except Exception as e:
        return f"Dictionary error: {e}"


_UNIT_BASE = {  # convert everything to a base unit per dimension
    # length → metres
    "mm": 0.001, "cm": 0.01, "m": 1, "km": 1000, "inch": 0.0254, "in": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "yard": 0.9144, "yd": 0.9144,
    "mile": 1609.344, "mi": 1609.344,
    # mass → grams
    "mg": 0.001, "g": 1, "gram": 1, "kg": 1000, "lb": 453.592, "lbs": 453.592,
    "pound": 453.592, "oz": 28.3495, "ounce": 28.3495, "ton": 1_000_000,
    # volume → litres
    "ml": 0.001, "l": 1, "litre": 1, "liter": 1, "gal": 3.78541, "gallon": 3.78541,
    "cup": 0.236588, "pint": 0.473176,
    # speed → m/s
    "mps": 1, "kmh": 0.277778, "kph": 0.277778, "mph": 0.44704,
    # data → bytes
    "b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4,
}
_UNIT_DIM = {}
for _grp in [["mm","cm","m","km","inch","in","ft","foot","feet","yard","yd","mile","mi"],
             ["mg","g","gram","kg","lb","lbs","pound","oz","ounce","ton"],
             ["ml","l","litre","liter","gal","gallon","cup","pint"],
             ["mps","kmh","kph","mph"], ["b","kb","mb","gb","tb"]]:
    for _u in _grp:
        _UNIT_DIM[_u] = _grp[0]


_UNIT_ALIAS = {"miles": "mi", "mile": "mi", "kilometers": "km", "kilometres": "km",
               "kilometer": "km", "kilometre": "km", "meters": "m", "metres": "m",
               "metre": "m", "meter": "m", "centimeters": "cm", "centimetres": "cm",
               "millimeters": "mm", "inches": "in", "feet": "ft", "yards": "yd",
               "pounds": "lb", "kilograms": "kg", "kilogram": "kg", "grams": "g",
               "ounces": "oz", "litres": "l", "liters": "l", "gallons": "gal",
               "cups": "cup", "pints": "pint", "celsius": "c", "fahrenheit": "f",
               "kelvin": "k", "centigrade": "c"}


def _unit_norm(u: str) -> str:
    u = u.lower().strip()
    if u in _UNIT_ALIAS:
        return _UNIT_ALIAS[u]
    if u not in _UNIT_BASE and u.endswith("s") and u[:-1] in _UNIT_BASE:
        return u[:-1]
    return u


def _convert_units(value: float, frm: str, to: str) -> str:
    f, t = _unit_norm(frm), _unit_norm(to)
    temp = {"c", "celsius", "f", "fahrenheit", "k", "kelvin"}
    if f in temp and t in temp:
        c = value if f in ("c", "celsius") else (value - 32) * 5 / 9 if f in ("f", "fahrenheit") else value - 273.15
        out = c if t in ("c", "celsius") else c * 9 / 5 + 32 if t in ("f", "fahrenheit") else c + 273.15
        return f"{value}°{f[0].upper()} = {round(out, 2)}°{t[0].upper()}."
    if f in _UNIT_BASE and t in _UNIT_BASE and _UNIT_DIM.get(f) == _UNIT_DIM.get(t):
        out = value * _UNIT_BASE[f] / _UNIT_BASE[t]
        return f"{value} {frm} = {round(out, 4)} {to}."
    return f"I can't convert {frm} to {to} (incompatible or unknown units)."


def _media_control(action: str) -> str:
    keymap = {"play": "play/pause media", "pause": "play/pause media",
              "playpause": "play/pause media", "next": "next track",
              "previous": "previous track", "prev": "previous track",
              "stop": "stop media", "volup": "volume up", "voldown": "volume down",
              "mute": "volume mute"}
    key = keymap.get(action.lower().replace(" ", ""))
    if not key:
        return f"Unknown media action '{action}'."
    try:
        import keyboard
        keyboard.send(key)
        return f"Sent media key: {action}."
    except Exception as e:
        return f"Couldn't send media key: {e}"


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

    system = SYSTEM_PROMPT
    # ── Auto-recall: surface relevant long-term memories for this command ──────
    try:
        hits = _mem_search(user_input, k=4)
        if hits:
            recalled = "\n".join(f"- {t}" for t, _ in hits)
            system += ("\n\n━━ THINGS YOU REMEMBER ABOUT THIS USER (use if relevant) ━━\n"
                       + recalled)
            print(f"   🧠 recalled {len(hits)} memory(ies)")
            _hud_emit("toast", text=f"🧠 recalled {len(hits)} memory(ies)", level="info")
            _hud_emit("memory", count=len(_MEM), recalled=len(hits))
    except Exception:
        pass

    messages = [{"role": "system", "content": system}] + history[-20:]

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

    _mem_load()   # load long-term memory from disk
    if _MEM:
        print(f"Memory: {len(_MEM)} fact(s) loaded.")
    _hud_emit("memory", count=len(_MEM))

    _rem_load()   # restore any pending reminders from a previous session
    threading.Thread(target=_reminder_loop, daemon=True).start()   # fires reminders when due
    threading.Thread(target=_vitals_loop, daemon=True).start()     # streams live system telemetry
    threading.Thread(target=_routine_loop, daemon=True).start()    # proactive daily briefings

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

    awaiting_followup = False   # conversational mode: brief window after a reply
    while True:
        # Typed commands from the HUD jump the queue and skip the wake word.
        typed = None
        try:
            typed = _TEXT_CMDS.get_nowait()
        except _queue.Empty:
            pass

        if typed is not None:
            query, is_typed = typed, True
        else:
            query, is_typed = takeCommand(), False

        if not query or query == "none":
            awaiting_followup = False        # silence → go back to requiring the wake word
            continue

        q = query.lower().strip()

        # ── Activation gate ───────────────────────────────────────────────────
        # Normally needs the wake word. But right after Jarvis replies we open a
        # short follow-up window where you can just keep talking, no "Jarvis".
        # Typed commands are always activated.
        followup = (awaiting_followup or is_typed) and ("jarvis" not in q)
        awaiting_followup = False             # this turn consumes the window

        if "jarvis" not in q and not followup and not is_typed:
            print("   (no wake word — ignored)")
            continue

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

        if followup:
            print("   ↪ follow-up (no wake word needed)")
            clean = re.sub(r"(?i)\bjarvis\b[,\s]*", "", query.strip()).strip()
        else:
            print("   ✅ Wake word detected")
            # ── Strip wake word so the LLM gets clean intent ───────────────────
            clean = query.strip()
            for w in WAKE_WORDS:
                if clean.lower().startswith(w):
                    clean = clean[len(w):].strip()
                    break
            else:
                clean = re.sub(r"(?i)\bjarvis\b[,\s]*", "", clean).strip()

        # Nothing left after stripping (e.g. user just said "Jarvis")
        if not clean:
            speak("Yes?")
            awaiting_followup = True
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
            awaiting_followup = True
            time.sleep(0.3)
            continue

        _hud_emit("task")     # one task completed → bump the counter
        speak(response)
        _learn_async(clean, response)   # passively extract & remember durable facts
        awaiting_followup = True         # open a follow-up window for a natural back-and-forth

        time.sleep(0.3)
