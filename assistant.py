#!/usr/bin/env python3
"""
assistant.py — Always-listening ESP32 voice assistant.

Combines the UDP/VAD/Whisper STT pipeline (receiver.py) with the
Ollama LLM + Piper TTS pipeline (voice_assistant.py) into a single
4-thread system with clear state management.

Architecture (5 threads + main):
  Thread 1 (udp-rx):    UDP socket → audio_queue
  Thread 2 (vad):       audio_queue → VAD → inference_queue
  Thread 3 (whisper):   inference_queue → Whisper STT → llm_queue
  Thread 4 (assistant): llm_queue → API delegator / Ollama LLM → TTS chunks
  Thread 5 (tts-worker): _tts_queue → Piper TTS → UDP stream to ESP32
  Main thread:          Shutdown handler (Ctrl+C)
"""

import re
import random
import socket
import threading
import queue
import time
import subprocess
import os
import numpy as np
import webrtcvad
import ollama
from openwakeword.model import Model as OWWModel
from datetime import datetime
from faster_whisper import WhisperModel
from collections import deque
from memory import ConversationMemory
from public_release.anime_info_tools import AnimeManager
from environmental_manager import WeatherManager
from api_delegator import APIDelegator

# ─── Configuration Constants ─────────────────────────────────────────────────

# UDP / Audio
UDP_PORT = 5000
MIC_SAMPLE_RATE = 16000              # ESP32 mic → Pi: VAD, Whisper, OWW
SAMPLE_WIDTH = 2

# VAD
VAD_MODE = 2
VAD_FRAME_MS = 30
VAD_FRAME_BYTES = int(MIC_SAMPLE_RATE * SAMPLE_WIDTH * VAD_FRAME_MS / 1000)

# Speech detection thresholds
SPEECH_PAD_MS = 300
SILENCE_DURATION_MS = 800
MIN_SPEECH_DURATION_MS = 500

# Whisper
MODEL_SIZE = "base.en"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
LANGUAGE = "en"

# Ollama LLM  ← (model change) swap LLM_MODEL to switch between models
# LLM_MODEL = "llama3.2:3b-instruct-q4_K_M"
LLM_MODEL = "qwen2.5:1.5b"               # faster / lighter alternative
LLM_CONTEXT_TURNS = 1  # conversation turns to keep (1 turn = 1 user + 1 assistant msg)
# Must match the num_ctx passed to ollama.chat options below.
# Used by ConversationMemory to skip optional system messages on small models.
# Set to 256 for qwen2.5:1.5b — keeps context tight and skips emotion injection.
LLM_CONTEXT_WINDOW = 256

# ─── IoT Command Registry ────────────────────────────────────────────────────
# Single source of truth for all valid tags. Adding a scene/face here
# automatically updates the LLM prompt — no string editing needed.

_LAMP_SCENES: list[str] = [
    "READING", "OCEAN",   "RAINBOW", "FIRE",    "STARS",   "BREATHE",
    "NIGHT",   "CANDLE",  "AURORA",  "WAVES",   "SUNSET",  "METEOR",
    "STORM",   "RIPPLE",  "LAVA",    "FIREFLY",
]

_FACE_TAGS: list[str] = [
    "NORMAL", "HAPPY", "LOVE", "WINK", "UWU",
    "LAUGH", "CRY", "SMIRK", "DIZZY", "HEAD_PAT", "TONGUE", "BLISSFUL",
]

# Flat sets used at runtime to validate / strip tags without re-parsing
_VALID_CMD_TAGS: set[str] = (
    {"[CMD:LOCK:UNLOCK]", "[CMD:LOCK:LOCK]", "[CMD:LOCK:STATUS]",
     "[CMD:LAMP:ON]",     "[CMD:LAMP:OFF]"}
    | {f"[CMD:LAMP:SCENE:{s}]" for s in _LAMP_SCENES}
)
_VALID_FACE_TAGS: set[str] = {f"[FACE:{f}]" for f in _FACE_TAGS}


# ─── Prompt fragments (assembled per-turn by the intent router) ───────────

_PROMPT_PREAMBLE: str = (
    "You are a concise smart-home voice assistant. "
    "Embed tags inline at the moment of action — they are stripped before speech. "
    "1-2 short sentences max. No markdown, no bullet points.\n"
)

_FACE_LINE: str = (
    "Faces: " + " ".join(f"[FACE:{f}]" for f in _FACE_TAGS) + "\n"
)

_LAMP_BLOCK: str = (
    "LAMP TAGS:\n"
    "  [CMD:LAMP:ON]  [CMD:LAMP:OFF]\n"
    "  [CMD:LAMP:BRIGHTNESS:N]  N=0-255 (dim≈60, medium≈128, bright≈220, full=255)\n"
    "  [CMD:LAMP:COLOR:R,G,B]\n"
    "  [CMD:LAMP:SCENE:X]  X= " + " ".join(_LAMP_SCENES) + "\n"
    'Ex: "turn lamp red"→"[CMD:LAMP:COLOR:255,0,0] Got it!"\n'
    'Ex: "turn on the lights"→"[CMD:LAMP:ON] Done!"\n'        # fixes [LAMP:ON] miss
    'Ex: "make it brighter"→"[CMD:LAMP:BRIGHTNESS:200] Done!"\n'  # fixes 128 ceiling

)

_LOCK_BLOCK: str = (
    "LOCK TAGS:\n"
    "  [CMD:LOCK:UNLOCK]  [CMD:LOCK:LOCK]  [CMD:LOCK:STATUS]\n"
    'Ex: "unlock door"→"[CMD:LOCK:UNLOCK] Door unlocked."\n'
    'Ex: "lock the door"→"[CMD:LOCK:LOCK] Locked."\n'         # fixes UNLOCK when asked to LOCK

)

# Lightweight prompt — no CMD tags at all
_CHAT_SYSTEM_PROMPT: str = (
    "You are a concise, friendly voice assistant. "
    "1-2 short sentences max. No markdown, no bullet points.\n"
    + _FACE_LINE
    + "You may optionally place ONE emotion tag at the very start of your response "
    "(it will be stripped before speech). Use sparingly, only for strong emotions:\n"
    "[FACE:HAPPY] [FACE:LAUGH] [FACE:CRY] [FACE:LOVE] [FACE:WINK] [FACE:UWU] "
    "[FACE:SMIRK] [FACE:DIZZY] [FACE:HEAD_PAT] [FACE:TONGUE] [FACE:BLISSFUL]\n"
    "Example: '[FACE:LAUGH] That's hilarious! Here's a fun fact...'\n"
    "If unsure, omit the tag entirely.\n"
)

# ─── Tiered Intent Classifier ────────────────────────────────────────────────
# Level 1: IoT or chat?   Level 2: which device?

_LAMP_TRIGGERS: list[str] = [
    "lamp", "light", "color", "colour", "bright", "dim",
    "scene", "turn on", "turn off", "rgb", "glow",
]
_LOCK_TRIGGERS: list[str] = [
    "lock", "unlock", "door", "latch",
]


def _classify_intent(text: str) -> str:
    """Return 'lamp', 'lock', 'both', or 'chat' based on keyword scan."""
    low = text.lower()
    lamp = any(kw in low for kw in _LAMP_TRIGGERS)
    lock = any(kw in low for kw in _LOCK_TRIGGERS)
    if lamp and lock:
        return "both"
    if lamp:
        return "lamp"
    if lock:
        return "lock"
    return "chat"


def _build_system_prompt(intent: str) -> str:
    """Assemble the minimal system prompt for the detected intent."""
    if intent == "chat":
        return _CHAT_SYSTEM_PROMPT
    parts = [_PROMPT_PREAMBLE]
    if intent in ("lamp", "both"):
        parts.append(_LAMP_BLOCK)
    if intent in ("lock", "both"):
        parts.append(_LOCK_BLOCK)
    parts.append(_FACE_LINE)
    return "".join(parts)

# Piper TTS
PIPER_DIR = os.path.expanduser("~/voice-assistant/piper")
PIPER_BIN = os.path.join(PIPER_DIR, "piper")
VOICE_MODEL = os.path.expanduser("~/voice-assistant/voices/en_US-ryan-medium.onnx")

# Wake word
# To use a custom word later, just change this string to the path of your .onnx file
# Example: WAKE_WORD_MODEL = "/home/samiul/voice-assistant/hey_samiul.onnx"
WAKE_WORD_MODEL = "alexa"

OWW_CHUNK_SAMPLES = 1280            # 80ms at 16kHz — minimum for openWakeWord
OWW_CHUNK_BYTES = OWW_CHUNK_SAMPLES * SAMPLE_WIDTH
OWW_CONFIDENCE = 0.8                # Detection threshold
OWW_CONFIRM_CHUNKS = 1              # Consecutive chunks above threshold required to trigger
LISTENING_TIMEOUT_S = 8.0           # Seconds before LISTENING falls back to SLEEPING

# ESP32 TTS output (UDP)
ESP32_IP       = "YOUR_ESP32_IP"    # ← set to your ESP32's IP
TTS_UDP_PORT   = 5001               # must match RX_UDP_PORT on ESP32
TTS_CHUNK_SIZE = 512               # samples per UDP packet
                                    # At 16 kHz: 1024 samples = 64 ms audio;
                                    # sleep(0.02) gives ~3× headroom vs starve
CHUNK_DURATION_S = TTS_CHUNK_SIZE / MIC_SAMPLE_RATE  # 512/16000 = 0.032s per chunk
TTS_SAMPLE_RATE = 22050             # Piper raw output rate for en_US-ryan-medium

# ESP32 Text/State output (UDP) — OLED + RGB ring
TEXT_UDP_PORT  = 5002               # text & state commands to ESP32

# IoT Device Control
LOCK_IP = "YOUR_LOCK_ESP32_IP"   # smart lock ESP32-C3 IP (user sets this)
LAMP_IP = "YOUR_LAMP_ESP32_IP"   # lamp ESP32 IP (user sets this)
# Note: lock and lamp commands are sent via TEXT_UDP_PORT (5002)
# using _send_text_udp() — same channel as OLED/state commands.
# The voice assistant ESP32 (YOUR_PI_IP) receives and forwards,
# OR you can send directly to each device if they each run UDP
# listeners on 5002. For now, all commands go to ESP32_IP:5002
# and the ESP32 firmware routes them. No change needed to sockets.

# Derived constants
SPEECH_PAD_FRAMES = int(SPEECH_PAD_MS / VAD_FRAME_MS)
SILENCE_FRAMES = int(SILENCE_DURATION_MS / VAD_FRAME_MS)
MIN_SPEECH_FRAMES = int(MIN_SPEECH_DURATION_MS / VAD_FRAME_MS)

# ─── Queues & Shared State ───────────────────────────────────────────────────

UDP_QUEUE_MAX = 50
audio_queue: queue.Queue = queue.Queue(maxsize=UDP_QUEUE_MAX)     # UDP packets
inference_queue: queue.Queue = queue.Queue()                       # numpy float32 arrays
llm_queue: queue.Queue = queue.Queue()                             # transcribed text strings

shutdown_event = threading.Event()
_memory_lock = threading.Lock()  # guards ConversationMemory writes vs save_all()
_memory: "ConversationMemory | None" = None  # set in main(), read by whisper thread


# ─── TTS → ESP32 UDP Streaming ───────────────────────────────────────────────

# Persistent UDP socket for TTS output (avoids create/teardown per response)
_tts_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Persistent UDP socket for text & state commands (OLED + RGB)
_text_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# ─── Persistent TTS Worker ───────────────────────────────────────────────────
# Single long-lived thread pulls chunks from _tts_queue — avoids spawning a
# new thread per LLM turn (previous design leaked threads on TTS exceptions).

_tts_queue: queue.Queue = queue.Queue()


def _clean_for_tts(text: str) -> str:
    """Convert symbols and abbreviations that Piper TTS can't pronounce cleanly."""
    import re as _re
    t = text

    # ── Smart / curly quotes and typographic punctuation ──────────────────
    t = t.replace("\u2018", "'").replace("\u2019", "'")   # '' → '
    t = t.replace("\u201c", '"').replace("\u201d", '"')   # "" → "
    t = t.replace("\u2014", ", ").replace("\u2013", " to ")  # em-dash, en-dash
    t = t.replace("\u2026", "...")                        # ellipsis character

    # ── Common abbreviations / symbols ────────────────────────────────────
    t = _re.sub(r'\bN/A\b', "not available", t, flags=_re.I)
    t = _re.sub(r'\bvs\.?\b', "versus", t, flags=_re.I)
    t = _re.sub(r'\bep\.?\s*(\d+)\b', r'episode \1', t, flags=_re.I)
    t = _re.sub(r'\bvol\.?\s*(\d+)\b', r'volume \1', t, flags=_re.I)
    t = _re.sub(r'\bch\.?\s*(\d+)\b', r'chapter \1', t, flags=_re.I)
    t = _re.sub(r'\bS(\d+)E(\d+)\b', r'season \1 episode \2', t, flags=_re.I)
    t = t.replace("&", " and ")
    t = t.replace("+", " plus ")
    t = t.replace("#", " number ")

    # ── Scores / ratings ──────────────────────────────────────────────────
    # "8.5/10" → "8.5 out of 10"
    t = _re.sub(r'(\d+(?:\.\d+)?)\s*/\s*10\b', r'\1 out of 10', t)
    # "85/100" → "85 out of 100"
    t = _re.sub(r'(\d+)\s*/\s*100\b', r'\1 out of 100', t)

    # ── Anime-specific ─────────────────────────────────────────────────────
    # Underscores in tags/slugs → spaces ("slice_of_life" → "slice of life")
    t = t.replace("_", " ")
    # AniList status codes that might leak through
    t = _re.sub(r'\bNOT YET RELEASED\b', "not yet released", t, flags=_re.I)
    t = _re.sub(r'\bFINISHED\b', "finished", t)
    t = _re.sub(r'\bRELEASING\b', "currently releasing", t)
    t = _re.sub(r'\bCANCELLED\b', "cancelled", t)

    # ── Weather / environment symbols ─────────────────────────────────────
    t = t.replace("°C", " degrees Celsius")
    t = t.replace("°F", " degrees Fahrenheit")
    t = t.replace("°", " degrees")
    t = t.replace("km/h", " kilometres per hour")
    t = t.replace("m/s", " metres per second")
    t = t.replace("mph", " miles per hour")
    t = t.replace("%", " percent")
    t = t.replace("µg/m³", " micrograms per cubic metre")
    t = t.replace("μg/m³", " micrograms per cubic metre")
    t = t.replace("hPa", " hectopascals")
    t = _re.sub(r'\bUV\b', "U V", t)   # so Piper says "U V" not "uv"

    # Wind compass abbreviations → spoken words (longest first to avoid prefix clash)
    compass = [
        ("NNE", "north north east"), ("NNW", "north north west"),
        ("ENE", "east north east"), ("ESE", "east south east"),
        ("SSE", "south south east"), ("SSW", "south south west"),
        ("WNW", "west north west"), ("WSW", "west south west"),
        ("NE", "north east"), ("NW", "north west"),
        ("SE", "south east"), ("SW", "south west"),
        ("N", "north"), ("S", "south"), ("E", "east"), ("W", "west"),
    ]
    for abbr, spoken in compass:
        t = _re.sub(r'\b' + abbr + r'\b', spoken, t)

    # ── Known acronyms → spoken form ──────────────────────────────────────
    # Must run BEFORE the ALL-CAPS title-case catch-all so e.g. AQI → "A Q I"
    # not "Aqi". Use word-boundary regex for precision.
    _ACRONYMS = [
        # Air quality / environment
        (r'\bAQI\b',    "A Q I"),
        (r'\bPM2\.5\b', "P M 2.5"),
        (r'\bPM10\b',   "P M 10"),
        (r'\bCO2\b',    "carbon dioxide"),
        (r'\bNO2\b',    "nitrogen dioxide"),
        (r'\bSO2\b',    "sulfur dioxide"),
        (r'\bO3\b',     "ozone"),
        (r'\bVOC\b',    "V O C"),
        (r'\bRH\b',     "relative humidity"),
        # Anime media types
        (r'\bOVA\b',    "O V A"),
        (r'\bONA\b',    "O N A"),
        (r'\bOAV\b',    "O A V"),
        (r'\bNSFW\b',   "N S F W"),
        # Tech
        (r'\bHTTPS\b',  "H T T P S"),
        (r'\bHTTP\b',   "H T T P"),
        (r'\bAPI\b',    "A P I"),
        (r'\bLLM\b',    "L L M"),
        (r'\bURL\b',    "U R L"),
        (r'\bIoT\b',    "I O T"),
        (r'\bAI\b',     "A I"),
        (r'\bUI\b',     "U I"),
        (r'\bOS\b',     "O S"),
    ]
    for _pat, _spoken in _ACRONYMS:
        t = _re.sub(_pat, _spoken, t)

    # ── Punctuation cleanup ───────────────────────────────────────────────
    # Year in parentheses: "ONE PIECE (1999)" → "ONE PIECE, from 1999,"
    t = _re.sub(r'\s*\((\d{4})\)', r', from \1,', t)
    # Any remaining parenthetical content → strip parens, keep content
    t = _re.sub(r'\(([^)]*)\)', r'\1', t)
    # ALL-CAPS words (e.g. anime titles) → title case so Piper doesn't shout
    t = _re.sub(r'\b([A-Z]{2,})\b', lambda m: m.group(1).title(), t)
    # Ellipsis → brief pause word (Piper handles comma pauses well)
    t = _re.sub(r'\.{2,}', ',', t)
    # Multiple exclamation/question marks → single
    t = _re.sub(r'[!]{2,}', '!', t)
    t = _re.sub(r'[?]{2,}', '?', t)
    # Brackets with non-CMD content (e.g. "[Action]") → remove
    t = _re.sub(r'\[(?!CMD:|FACE:|ANIM:)[^\]]*\]', '', t)
    # Stray asterisks (markdown bold/italic)
    t = _re.sub(r'\*+', '', t)
    # Remove URLs
    t = _re.sub(r'https?://\S+', '', t)
    # Trailing commas left by substitutions
    t = _re.sub(r',\s*\.', '.', t)
    t = _re.sub(r',\s*,', ',', t)

    # ── Final whitespace collapse ─────────────────────────────────────────
    t = _re.sub(r' {2,}', ' ', t).strip()
    return t


def _tts_worker_loop():
    """Persistent TTS worker — pulls chunks from _tts_queue until shutdown."""
    while not shutdown_event.is_set():
        try:
            chunk = _tts_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            clean = _clean_for_tts(chunk)
            print(f"[tts] {clean}", flush=True)
            stream_tts_to_esp32(clean)
        except Exception as e:
            print(f"[error] TTS/playback failed: {e}", flush=True)
        finally:
            _tts_queue.task_done()


def _send_text_udp(message: str) -> None:
    """Send a UTF-8 text message to the ESP32 over the text/state UDP port."""
    try:
        _text_sock.sendto(message.encode("utf-8"), (ESP32_IP, TEXT_UDP_PORT))
    except OSError as e:
        print(f"[text-udp] ⚠️  send failed: {e}", flush=True)


def resample_pcm(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """
    Resample 16-bit mono PCM from from_rate to to_rate using linear
    interpolation.  Runs on Pi CPU — ~5 ms for a 3-second utterance
    at 22050→16000 Hz.  No external DSP library needed.
    """
    if from_rate == to_rate:
        return pcm_bytes

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    target_len = int(len(samples) * to_rate / from_rate)
    src_indices = np.linspace(0, len(samples) - 1, target_len)
    floor_idx = np.clip(src_indices.astype(np.int64), 0, len(samples) - 1)
    ceil_idx  = np.clip(floor_idx + 1, 0, len(samples) - 1)
    frac = (src_indices - floor_idx).astype(np.float32)
    resampled = samples[floor_idx] * (1.0 - frac) + samples[ceil_idx] * frac
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


def stream_tts_to_esp32(text: str) -> None:
    """
    Run Piper in --output-raw mode, resample the 22050 Hz PCM to 16000 Hz,
    and stream the result to the ESP32 via UDP.
    Pacing uses actual chunk duration to keep the ESP32 queue stable.
    """
    piper_cmd = [
        PIPER_BIN,
        "--model", VOICE_MODEL,
        "--output-raw",
        "--length-scale", "0.85",
    ]
    proc = subprocess.Popen(
        piper_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=PIPER_DIR,
    )
    raw_pcm, _ = proc.communicate(input=text.encode())

    if proc.returncode != 0:
        print("[error] Piper exited with non-zero status", flush=True)
        return

    # Resample entire audio Piper 22050 Hz → ESP32 16000 Hz
    pcm_16k = resample_pcm(raw_pcm, TTS_SAMPLE_RATE, MIC_SAMPLE_RATE)

    if not pcm_16k:
        print("[tts] ⚠️  Piper produced empty audio — skipping stream", flush=True)
        return

    chunk_bytes = TTS_CHUNK_SIZE * 2  # 2 bytes per 16-bit sample
    n_chunks = 0
    send_start = time.monotonic()

    for offset in range(0, len(pcm_16k), chunk_bytes):
        _tts_sock.sendto(
            pcm_16k[offset : offset + chunk_bytes],
            (ESP32_IP, TTS_UDP_PORT),
        )
        n_chunks += 1
        # Sleep until the exact moment this chunk should have played out.
        # This is clock-anchored so drift never accumulates.
        expected_time = send_start + (n_chunks * CHUNK_DURATION_S)
        sleep_for = expected_time - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    print(f"[tts] Streamed {n_chunks} chunks to ESP32 ({len(pcm_16k)} bytes, {TTS_CHUNK_SIZE*2} bytes/chunk)", flush=True)

# ─── Thread-Safe State Machine ───────────────────────────────────────────────

_state = "SLEEPING"
_state_lock = threading.Lock()
_listening_start_time = None  # Protected by _state_lock


def set_state(new_state):
    global _state, _listening_start_time
    with _state_lock:
        _state = new_state
        if new_state == "LISTENING":
            _listening_start_time = time.time()
        icons = {
            "SLEEPING":   f"💤  [SLEEPING]   — waiting for wake word '{WAKE_WORD_MODEL}'...",
            "LISTENING":  "🎤  [LISTENING]  — wake word heard! speak command...",
            "PROCESSING": "🧠  [PROCESSING] — thinking...",
            "SPEAKING":   "🔊  [SPEAKING]   — playing response",
        }
        print(f"\n{icons[new_state]}", flush=True)

    # Broadcast state to ESP32 RGB ring (outside lock to avoid blocking)
    _send_text_udp(f"[STATE:{new_state}]")


def get_state():
    with _state_lock:
        return _state


def get_listening_elapsed():
    """Returns seconds since LISTENING started, or None if not listening."""
    with _state_lock:
        if _state == "LISTENING" and _listening_start_time is not None:
            return time.time() - _listening_start_time
        return None


# ─── Thread 1: UDP Receive Loop ──────────────────────────────────────────────

def udp_receive_loop():
    """
    Opens a UDP socket on UDP_PORT and pushes every incoming packet's raw
    payload into audio_queue. Drops oldest packet when queue is full.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(0.5)

    print(f"[receiver] Listening for UDP audio on port {UDP_PORT} …")

    try:
        while not shutdown_event.is_set():
            try:
                data, addr = sock.recvfrom(65535)
                if data:
                    try:
                        audio_queue.put_nowait(data)
                    except queue.Full:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                        audio_queue.put_nowait(data)
            except socket.timeout:
                continue
    finally:
        sock.close()
        print("[receiver] UDP socket closed.")


# ─── Thread 2: VAD Loop ──────────────────────────────────────────────────────

def vad_loop():
    """
    Listens for the wake word using openWakeWord. Once triggered,
    uses webrtcvad to detect the complete spoken command, then enqueues it.
    """
    print(f"[vad] Loading openWakeWord model ('{WAKE_WORD_MODEL}')...")
    # OWWModel() with no args loads ALL bundled models (alexa, hey_mycroft, …).
    # The constructor uses **kwargs that leak to AudioFeatures, so custom
    # keyword args like inference_framework or wakeword_models cause a
    # TypeError. No-arg init is the safest call for this version.
    # The prediction dict key for alexa is simply "alexa", matched via
    # WAKE_WORD_MODEL so only one model can trigger a wake.
    oww_model = OWWModel()
    print("[vad] Wake word model ready.")

    vad = webrtcvad.Vad(VAD_MODE)
    ring_buffer = deque(maxlen=SPEECH_PAD_FRAMES)
    speech_frames = []
    triggered = False
    silence_counter = 0

    byte_buffer = bytearray()
    buf_offset = 0

    # OWW needs 80ms chunks (1280 samples), not 30ms VAD frames
    oww_buffer = bytearray()
    oww_hit_counter = 0  # consecutive chunks above OWW_CONFIDENCE

    print(f"[vad] Initialized webrtcvad (mode={VAD_MODE}, frame={VAD_FRAME_MS}ms)")

    while not shutdown_event.is_set():
        # Pull audio from queue
        try:
            data = audio_queue.get(timeout=0.1)
            current_state = get_state()

            # Software deafen during processing/speaking
            if current_state not in ["SLEEPING", "LISTENING"]:
                byte_buffer = bytearray()
                buf_offset = 0
                ring_buffer.clear()
                speech_frames = []
                triggered = False
                silence_counter = 0
                oww_buffer = bytearray()
                continue

            byte_buffer.extend(data)
        except queue.Empty:
            # Check LISTENING timeout even when no audio arrives
            elapsed = get_listening_elapsed()
            if elapsed is not None and elapsed > LISTENING_TIMEOUT_S:
                print("\n[vad] ⏱️  Listening timeout, back to sleep.", flush=True)
                _send_text_udp("[FACE:YAWN]")
                time.sleep(0.1)
                set_state("SLEEPING")
                _send_text_udp("[ANIM:IDLE]")
                ring_buffer.clear()
                speech_frames = []
                triggered = False
                silence_counter = 0
            elif triggered and len(byte_buffer) - buf_offset == 0:
                time.sleep(0.05)
            continue

        # Process complete frames
        while (len(byte_buffer) - buf_offset) >= VAD_FRAME_BYTES:
            frame = bytes(byte_buffer[buf_offset:buf_offset + VAD_FRAME_BYTES])
            buf_offset += VAD_FRAME_BYTES

            # Compact buffer when offset is large (>64 KB) to reclaim memory
            if buf_offset > 65536:
                byte_buffer = byte_buffer[buf_offset:]
                buf_offset = 0

            # ── WAKE WORD DETECTION PHASE ────────────────────────────
            if get_state() == "SLEEPING":
                # Accumulate 30ms VAD frames into 80ms OWW chunks
                oww_buffer.extend(frame)
                if len(oww_buffer) >= OWW_CHUNK_BYTES:
                    audio_array = np.frombuffer(
                        bytes(oww_buffer[:OWW_CHUNK_BYTES]), dtype=np.int16
                    )
                    oww_buffer = oww_buffer[OWW_CHUNK_BYTES:]
                    prediction = oww_model.predict(audio_array)

                    # Use WAKE_WORD_MODEL key directly — accurate and avoids
                    # any other loaded model accidentally triggering a wake.
                    # Require OWW_CONFIRM_CHUNKS consecutive hits to reject noise spikes.
                    if prediction.get(WAKE_WORD_MODEL, 0) > OWW_CONFIDENCE:
                        oww_hit_counter += 1
                    else:
                        oww_hit_counter = 0

                    if oww_hit_counter >= OWW_CONFIRM_CHUNKS:
                        oww_hit_counter = 0
                        set_state("LISTENING")
                        _send_text_udp("[ANIM:EXCITED]")
                        ring_buffer.clear()
                        speech_frames = []
                        oww_buffer = bytearray()
                continue  # Skip VAD logic while sleeping

            # ── LISTENING TIMEOUT CHECK ──────────────────────────────
            elapsed = get_listening_elapsed()
            if elapsed is not None and elapsed > LISTENING_TIMEOUT_S:
                print("\n[vad] ⏱️  Listening timeout, back to sleep.", flush=True)
                _send_text_udp("[FACE:YAWN]")
                time.sleep(0.1)
                set_state("SLEEPING")
                _send_text_udp("[ANIM:IDLE]")
                ring_buffer.clear()
                speech_frames = []
                triggered = False
                silence_counter = 0
                continue

            # ── VAD COMMAND RECORDING PHASE ──────────────────────────
            is_speech = vad.is_speech(frame, MIC_SAMPLE_RATE)

            if not triggered:
                # ── WAITING FOR SPEECH ───────────────────────────────
                ring_buffer.append((frame, is_speech))
                num_voiced = sum(1 for f, speech in ring_buffer if speech)

                if num_voiced > 0.5 * ring_buffer.maxlen:
                    triggered = True
                    speech_frames.extend(f for f, s in ring_buffer)
                    ring_buffer.clear()
                    silence_counter = 0
                    print("[vad] 🎤 Speech detected, recording...", end="", flush=True)
            else:
                # ── RECORDING SPEECH ─────────────────────────────────
                speech_frames.append(frame)

                if is_speech:
                    silence_counter = 0
                else:
                    silence_counter += 1

                if silence_counter >= SILENCE_FRAMES:
                    print(f" ({len(speech_frames)} frames)", flush=True)

                    if len(speech_frames) >= MIN_SPEECH_FRAMES:
                        audio_bytes = b"".join(speech_frames)
                        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                        inference_queue.put(samples)
                        set_state("PROCESSING")  # Close race window before Thread 3 picks it up
                    else:
                        print("[vad] ⚠️  Too short, skipping.", flush=True)
                        set_state("SLEEPING")
                        _send_text_udp("[ANIM:IDLE]")

                    triggered = False
                    speech_frames = []
                    silence_counter = 0
                    ring_buffer.clear()


# ─── Thread 3: Whisper Inference Loop ─────────────────────────────────────────

# ─── Fast-Path Short-Circuit Router ─────────────────────────────────────────
# Matches simple deterministic commands and fires UDP directly from Thread 3,
# bypassing Ollama + Piper entirely.  Latency: STT → UDP in <5 ms.

FAST_INTENTS: list[tuple[re.Pattern, str]] = [
    # Turn on + color (compound — must appear before individual on/off and color entries)
    # Uses lookaheads so word order doesn't matter ("turn on lamp red" OR "red lamp turn on")
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\bred\b)',            re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,0,0]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\bgreen\b)',          re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:0,255,0]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\bblue\b)',           re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:0,0,255]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\b(purple|violet)\b)',re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:128,0,128]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\bpink\b)',           re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,20,147]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\borange\b)',         re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,80,0]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\byellow\b)',         re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,200,0]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\bwhite\b)',          re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,255,255]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\b(warm white|warm light)\b)', re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:255,160,60]"),
    (re.compile(r'(?=.*\b(turn on|switch on|lamp on|light on|lights on)\b)(?=.*\b(cyan|teal)\b)',    re.I), "[CMD:LAMP:ON][CMD:LAMP:COLOR:0,255,200]"),
    # Lamp on/off
    (re.compile(r'\b(turn on|switch on|lamp on|light on|lights on)\b', re.I), "[CMD:LAMP:ON]"),
    (re.compile(r'\b(turn off|switch off|lamp off|light off|lights off)\b', re.I), "[CMD:LAMP:OFF]"),
    # Lock / unlock
    (re.compile(r'\b(unlock|open) (the )?(door|lock)\b', re.I), "[CMD:LOCK:UNLOCK]"),
    (re.compile(r'\b(lock|close|secure) (the )?(door|lock)\b', re.I), "[CMD:LOCK:LOCK]"),
    (re.compile(r'\b(lock|door) status\b', re.I), "[CMD:LOCK:STATUS]"),
    # Common scenes
    (re.compile(r'\b(good night|goodnight|night mode|sleep mode)\b', re.I),
     "[CMD:LAMP:SCENE:NIGHT][CMD:LAMP:BRIGHTNESS:30]"),
    (re.compile(r'\b(good morning|morning mode|wake up mode)\b', re.I),
     "[CMD:LAMP:SCENE:READING][CMD:LAMP:BRIGHTNESS:200]"),
    (re.compile(r'\b(party|party mode|party time)\b', re.I),
     "[CMD:LAMP:SCENE:RAINBOW][CMD:LAMP:BRIGHTNESS:255]"),
    (re.compile(r'\b(reading mode|reading light)\b', re.I), "[CMD:LAMP:SCENE:READING]"),
    (re.compile(r'\b(movie mode|movie time|cinema mode)\b', re.I), "[CMD:LAMP:BRIGHTNESS:30]"),
    # Brightness shortcuts
    (re.compile(r'\b(full brightness|maximum brightness|lights? (all )?the way up)\b', re.I),
     "[CMD:LAMP:BRIGHTNESS:255]"),
    (re.compile(r'\b(minimum brightness|lights? (all )?the way down|very dim)\b', re.I),
     "[CMD:LAMP:BRIGHTNESS:10]"),
    # Named color shortcuts
    (re.compile(r'\b(red|make it red|turn.*red|color.*red|colour.*red)\b', re.I),   "[CMD:LAMP:COLOR:255,0,0]"),
    (re.compile(r'\b(green|make it green|turn.*green|color.*green|colour.*green)\b', re.I), "[CMD:LAMP:COLOR:0,255,0]"),
    (re.compile(r'\b(blue|make it blue|turn.*blue|color.*blue|colour.*blue)\b', re.I),  "[CMD:LAMP:COLOR:0,0,255]"),
    (re.compile(r'\b(purple|violet|make it purple|turn.*purple|color.*purple|colour.*purple)\b', re.I), "[CMD:LAMP:COLOR:128,0,128]"),
    (re.compile(r'\b(pink|make it pink|turn.*pink|color.*pink|colour.*pink)\b', re.I),  "[CMD:LAMP:COLOR:255,20,147]"),
    (re.compile(r'\b(orange|make it orange|turn.*orange|color.*orange|colour.*orange)\b', re.I), "[CMD:LAMP:COLOR:255,80,0]"),
    (re.compile(r'\b(yellow|make it yellow|turn.*yellow|color.*yellow|colour.*yellow)\b', re.I), "[CMD:LAMP:COLOR:255,200,0]"),
    (re.compile(r'\b(white|make it white|turn.*white|color.*white|colour.*white)\b', re.I),  "[CMD:LAMP:COLOR:255,255,255]"),
    (re.compile(r'\b(warm white|warm light)\b', re.I),  "[CMD:LAMP:COLOR:255,160,60]"),
    (re.compile(r'\b(cyan|teal|make it cyan|turn.*cyan)\b', re.I),  "[CMD:LAMP:COLOR:0,255,200]"),
]


def execute_fast_path(text: str) -> bool:
    """
    Check text against ALL FAST_INTENTS patterns (not just the first match).
    Sends every matched command so compound requests like
    "unlock the door and turn the lamp red" fire both commands.
    Returns True if at least one pattern matched, False otherwise.
    """
    matched_tags: list[str] = []
    matched_patterns: set[int] = set()  # track by index to avoid duplicate fires

    for idx, (pattern, command) in enumerate(FAST_INTENTS):
        if idx in matched_patterns:
            continue
        if pattern.search(text):
            for tag in re.findall(r'\[[^\]]+\]', command):
                if tag not in matched_tags:      # deduplicate (e.g. two patterns → same CMD)
                    matched_tags.append(tag)
            matched_patterns.add(idx)

    if not matched_tags:
        return False

    for tag in matched_tags:
        _send_text_udp(tag)
        print(f"[fast-path] ⚡ {tag}", flush=True)
    return True


# ─── Thread 3: Whisper Inference Loop ────────────────────────────────────────

# Shared reference — set in main(), read by assistant thread
_delegator: APIDelegator = None  # type: ignore[assignment]


def _llm_classify_intent(text: str) -> str:
    """
    Tier 2 domain classifier — tiny LLM call for ambiguous queries where
    keyword scan matched both domains or neither.  Returns "ANIME", "WEATHER",
    or "CHAT".  Runs inside Thread 4 (assistant_loop) so it never blocks Whisper.
    """
    resp = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system",
             "content": "Classify the user's request into exactly one category. "
                        "Reply with ONLY the category name, nothing else.\n"
                        "Categories: ANIME, WEATHER, CHAT"},
            {"role": "user", "content": text},
        ],
        options={"num_ctx": 64, "num_predict": 3},
    )
    return resp["message"]["content"].strip()


def whisper_inference_loop(model: WhisperModel):
    """
    Pulls complete utterances from inference_queue, transcribes with Whisper.
    Fast path: simple IoT commands are dispatched via UDP immediately,
    bypassing Ollama entirely.  Everything else goes to llm_queue.
    """
    print("[whisper] Inference thread ready.\n")

    while not shutdown_event.is_set():
        try:
            samples = inference_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        set_state("PROCESSING")

        try:
            segments, info = model.transcribe(
                samples,
                language=LANGUAGE,
                beam_size=1,
                vad_filter=False,
                without_timestamps=True,
            )

            # Materialise the lazy generator immediately
            segments = list(segments)

            text = " ".join(seg.text.strip() for seg in segments).strip()

            # ── STT correction: fix common Whisper mishearings ───────────
            _STT_FIXES = [
                # Manhwa / manhua / webtoon often mishear
                (r'\bman(?:u?hua|awa|uwa|ohua|ua)\b', "manhwa"),
                (r'\bmanwha\b', "manhwa"),
                (r'\bweb\s*toon\b', "webtoon"),
                # Common anime title mishearings
                (r'\bone\s*peace\b', "one piece"),
                (r'\bdragon\s*ball\s*z\b', "dragon ball z"),
                (r'\bnaruto\s+shippu?den\b', "naruto shippuden"),
                (r'\bdemon\s*slayer\b', "demon slayer"),
                (r'\battack on tight\b', "attack on titan"),
                (r'\bjojo(?:\'?s\s+bizarre\s+adventure)?\b', "jojo's bizarre adventure"),
            ]
            import re as _re_stt
            for pattern, replacement in _STT_FIXES:
                text = _re_stt.sub(pattern, replacement, text, flags=_re_stt.I)
            # ────────────────────────────────────────────────────────────

            if text:
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f'[{timestamp}] 💬 "{text}"', flush=True)
                # ── Fast-path check: bypass LLM for deterministic commands ──
                if execute_fast_path(text):
                    set_state("SLEEPING")
                    _send_text_udp("[ANIM:IDLE]")
                    continue

                # Fell through — hand off to Thread 4 for delegator + LLM
                llm_queue.put(text)
                # Let the assistant thread manage state from here
            else:
                print("[whisper] ⚠️  Transcription empty.", flush=True)
                set_state("SLEEPING")
                _send_text_udp("[ANIM:IDLE]")

        except Exception as e:
            print(f"[error] Transcription failed: {e}", flush=True)
            set_state("SLEEPING")
            _send_text_udp("[ANIM:IDLE]")


# ─── Thread 4: Assistant Loop (LLM + TTS) ────────────────────────────────────

def should_flush(buffer: str, word_threshold: int = 12) -> bool:
    # Never flush while an IoT or face tag is open — commas inside tags like
    # [CMD:LAMP:COLOR:255,0,0] would otherwise trigger a punctuation flush
    # mid-tag, splitting it across chunks and sending garbage to TTS/OLED.
    if re.search(r'\[(?:CMD|FACE):[^\[\]]*$', buffer, re.IGNORECASE):
        return False
    stripped = buffer.rstrip()
    if stripped and stripped[-1] in '.!?,;:':
        return True
    if len(buffer.split()) >= word_threshold:
        return True
    return False


# Regex matches any [CMD:...] or [FACE:...] tag — allows internal newlines/spaces, case-insensitive
_IOT_TAG_RE = re.compile(r'\[(?:CMD|FACE):[^\[\]]+\]', re.IGNORECASE)


def parse_and_dispatch_iot(text: str) -> str:
    """
    Extract all [CMD:...] tags from text, send each to ESP32 via UDP,
    and return the cleaned text with tags removed.
    Called on each completed TTS flush chunk AND on the full response
    for memory recording.
    """
    tags = _IOT_TAG_RE.findall(text)
    for tag in tags:
        _send_text_udp(tag)
        print(f"[iot] \u2192 {tag}", flush=True)
    return _IOT_TAG_RE.sub('', text).strip()


# ─── Mochi Face / Animation Helpers ──────────────────────────────────────────

# Map strong ANIM emotions to their FACE overlay equivalents (sent during TTS)
_ANIM_TO_FACE: dict[str, str] = {
    "[ANIM:LAUGH]":         "[FACE:LAUGH]",
    "[ANIM:CRYING]":        "[FACE:CRY]",
    "[ANIM:LOVE]":          "[FACE:LOVE]",
    "[ANIM:EXCITED]":       "[FACE:HAPPY]",
    "[ANIM:HAPPY]":         "[FACE:HAPPY]",
    "[ANIM:SLEEPY]":        "[FACE:YAWN]",
    "[ANIM:ANGRY]":         "[FACE:SMIRK]",
    "[ANIM:UWU]":           "[FACE:UWU]",
    "[ANIM:WINK]":          "[FACE:WINK]",
    "[ANIM:AWKWARD_LAUGH]": "[FACE:SMIRK]",
    "[ANIM:DISTRACTED]":    "[FACE:LOOK_LEFT]",
}

# Per-animation playback durations (seconds) — used to wait for the animation
# to finish before set_state("SPEAKING") kills it on the ESP32.
_ANIM_DURATIONS: dict[str, float] = {
    "[ANIM:LAUGH]":         1.9,
    "[ANIM:CRYING]":        1.5,
    "[ANIM:LOVE]":          1.9,
    "[ANIM:HAPPY]":         1.5,
    "[ANIM:EXCITED]":       1.9,
    "[ANIM:DISTRACTED]":    1.3,
    "[ANIM:SLEEPY]":        1.7,
    "[ANIM:UWU]":           1.7,
    "[ANIM:ANGRY]":         1.7,
    "[ANIM:WINK]":          1.3,
    "[ANIM:AWKWARD_LAUGH]": 1.5,
}


def pick_emotion_tag(response_text: str) -> str | None:
    """
    Analyze LLM response text and return one [ANIM:*] tag for pre-TTS playback.
    Returns None if no strong emotion detected — state face handles it.
    First match wins. Uses [ANIM:*] only (called before SPEAKING state, not during TTS).
    """
    text = response_text.lower()
    if any(w in text for w in ["haha", "lol", "funny", "joke", "hilarious", "laugh"]):
        return "[ANIM:LAUGH]"
    if any(w in text for w in ["sorry", "sad", "unfortunately", "apolog", "oh no"]):
        return "[ANIM:CRYING]"
    if any(w in text for w in ["love", "adore", "heart", "miss you"]):
        return "[ANIM:LOVE]"
    if any(w in text for w in ["thank", "grateful", "appreciate", "sweet", "kind"]):
        return "[ANIM:HAPPY]"
    if any(w in text for w in ["wow", "amazing", "incredible", "awesome", "fantastic"]):
        return "[ANIM:EXCITED]"
    if any(w in text for w in ["hmm", "well", "not sure", "let me think", "interesting"]):
        return "[ANIM:DISTRACTED]"
    if any(w in text for w in ["confused", "don't understand", "unclear", "what?"]):
        return None
    if any(w in text for w in ["sleepy", "tired", "goodnight", "good night", "rest", "sleep"]):
        return "[ANIM:SLEEPY]"
    if any(w in text for w in ["cute", "aww", "adorable", "precious"]):
        return "[ANIM:UWU]"
    if any(w in text for w in ["angry", "furious", "mad", "unacceptable"]):
        return "[ANIM:ANGRY]"
    if any(w in text for w in ["wink", "secret", "between us", "sly"]):
        return "[ANIM:WINK]"
    if any(w in text for w in ["awkward", "nervous", "oops", "my bad", "embarrass"]):
        return "[ANIM:AWKWARD_LAUGH]"
    return None


def extract_leading_face_tag(response: str) -> tuple[str | None, str]:
    """
    Extract a leading [FACE:*] tag placed by the LLM at the start of its response.
    Returns (tag_or_None, cleaned_text_for_tts).
    Only [FACE:*] tags — never [ANIM:*] or [CMD:*] — are extracted here.
    """
    match = re.match(r'^\s*(\[FACE:[A-Z_]+\])\s*', response)
    if match:
        return match.group(1), response[match.end():]
    return None, response


def assistant_loop(memory: ConversationMemory):
    """
    Pulls transcribed text from llm_queue, sends it to Ollama for a response
    (streamed token-by-token), then speaks the reply using Piper + aplay.
    Uses ConversationMemory for context-aware prompting.
    """
    print("[assistant] LLM + TTS thread ready.\n")

    while not shutdown_event.is_set():
        try:
            text = llm_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        # ── API delegator: anime / weather (runs in Thread 4, not Thread 3,
        #    so Whisper stays unblocked during potentially slow API calls) ──
        if _delegator is not None:
            domain = _delegator.classify_domain(text)
            if domain:
                try:
                    result = _delegator.handle(text, domain)
                    if result:
                        _send_text_udp("[CLEAR]")
                        anim_tag = pick_emotion_tag(result)
                        if anim_tag:
                            _send_text_udp(anim_tag)
                            time.sleep(_ANIM_DURATIONS.get(anim_tag, 1.5))
                        set_state("SPEAKING")
                        # Chunk OLED text to prevent ESP32 buffer overflow
                        words = result.split()
                        for i in range(0, len(words), 8):
                            _send_text_udp(" ".join(words[i:i + 8]))
                            time.sleep(0.1)
                        _tts_queue.put(result)
                        with _memory_lock:
                            memory.record_turn(text, result, is_iot=False)
                        _tts_queue.join()
                        set_state("SLEEPING")
                        _send_text_udp("[ANIM:IDLE]")
                        continue
                except Exception as e:
                    print(f"[delegator] ⚠️  {e}", flush=True)

        # ── LLM PHASE ────────────────────────────────────────────────────
        set_state("PROCESSING")
        print(f'\n[you said] "{text}"', flush=True)
        print("[assistant] ", end="", flush=True)

        # ── Tiered Prompt Router ────────────────────────────────────
        # Top-down: classify intent → pick device → inject only the
        # relevant CMD block.  Chat queries get zero CMD tokens.
        intent = _classify_intent(text)
        chosen_prompt = _build_system_prompt(intent)

        # Always call build_prompt so topics and mood stay current, even on
        # IoT turns.  Only chat actually uses the returned message list.
        _prompt_messages = memory.build_prompt(text)
        if intent == "chat":
            messages = _prompt_messages
        else:
            messages = []

        # Inject chosen system prompt safely — only if not already present,
        # preventing context explosion on repeated turns.
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": chosen_prompt})
        else:
            # Replace any prior system content with the freshly-routed prompt
            messages[0]["content"] = chosen_prompt

        # Append the current user message as the final entry
        messages.append({"role": "user", "content": text})

        # Tell ESP32 to clear the OLED before new response
        _send_text_udp("[CLEAR]")

        # Truncate context to prevent Pi TTFT blowup
        _max_ctx = LLM_CONTEXT_TURNS * 2  # turns → messages
        if len(messages) > _max_ctx + 1:  # +1 for system prompt
            messages = [messages[0]] + messages[-_max_ctx:]

        full_response = ""
        tts_buffer = ""
        llm_error = False
        has_started_speaking = False
        try:
            stream = ollama.chat(
                model=LLM_MODEL,
                messages=messages,
                stream=True,
                keep_alive="1h",
                options={"num_ctx": LLM_CONTEXT_WINDOW, "num_predict": 80},
            )
            for chunk in stream:
                token = chunk["message"]["content"]
                print(token, end="", flush=True)
                full_response += token
                tts_buffer += token

                # ── INSTANT IOT EXECUTION ────────────────────────────────
                # Dispatch completed tags the moment ']' arrives — before any
                # flush boundary — so the lamp reacts while Ollama still types.
                tags = _IOT_TAG_RE.findall(tts_buffer)
                if tags:
                    for tag in tags:
                        _send_text_udp(tag)
                        print(f"\n[iot-fast] Executed instantly: {tag}", flush=True)
                    tts_buffer = _IOT_TAG_RE.sub('', tts_buffer)
                # ────────────────────────────────────────────────────────

                if should_flush(tts_buffer):
                    # Hold back any incomplete trailing [CMD:...] or [FACE:...] tag so it
                    # isn't split across flush boundaries and sent as garbage.
                    partial_match = re.search(r'\[(?:CMD|FACE):[^\[\]]*$', tts_buffer, re.IGNORECASE)
                    if partial_match:
                        held = tts_buffer[partial_match.start():]
                        tts_buffer = tts_buffer[:partial_match.start()]
                    else:
                        held = ""

                    clean_chunk = parse_and_dispatch_iot(tts_buffer)
                    if clean_chunk:          # skip if chunk was pure tag(s)
                        _tts_queue.put(clean_chunk)
                        if not has_started_speaking:
                            set_state("SPEAKING")
                            has_started_speaking = True
                        _send_text_udp(clean_chunk)  # OLED gets clean text only
                    tts_buffer = held  # carry incomplete tag into next chunk

        except Exception as e:
            print(f"\n[error] Ollama failed: {e}", flush=True)
            llm_error = True

        print(flush=True)  # newline after streamed response

        # Flush any remaining buffer content after LLM finishes
        if tts_buffer.strip():
            clean_chunk = parse_and_dispatch_iot(tts_buffer)
            if clean_chunk:
                _tts_queue.put(clean_chunk)
                _send_text_udp(clean_chunk)  # OLED gets clean tail text

        # ── Mochi face/anim: extract LLM face tag and pick emotion ──
        llm_face_tag, tts_text = extract_leading_face_tag(full_response)
        anim_tag = pick_emotion_tag(tts_text) if intent == "chat" else None
        if anim_tag:
            _send_text_udp(anim_tag)
            time.sleep(_ANIM_DURATIONS.get(anim_tag, 1.5))

        # Record turn while TTS may still be playing — full_response is complete
        # at this point and must not be skipped by any join/state exception.
        # NOTE: always pass full_response (not tts_text) so the raw LLM output
        # including any leading [FACE:*] tag is preserved verbatim in episodic
        # memory.  tts_text is only used for TTS audio; it must not leak here.
        if not llm_error and full_response.strip():
            with _memory_lock:  # prevent race with save_all() during shutdown
                memory.record_turn(text, full_response, is_iot=(intent != "chat"))

        # Edge case: if every chunk was a pure tag, no clean text was ever queued;
        # set SPEAKING now so the state machine doesn't stay stuck on PROCESSING.
        if not llm_error and full_response.strip() and not has_started_speaking:
            set_state("SPEAKING")
        # Send face overlay during TTS (2s overlay on ESP32)
        if not llm_error and full_response.strip():
            speaking_face = llm_face_tag or _ANIM_TO_FACE.get(anim_tag)
            if speaking_face and intent == "chat":
                _send_text_udp(speaking_face)

        # Wait for persistent TTS worker to finish all queued chunks
        try:
            _tts_queue.join()
        finally:
            set_state("SLEEPING")
            _send_text_udp("[ANIM:IDLE]")

        if llm_error or not full_response.strip():
            continue



# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("══════════════════════════════════════════════════════════")
    print("  ESP32 Voice Assistant")
    print(f"  STT: faster-whisper {MODEL_SIZE}  |  LLM: {LLM_MODEL} |  TTS: Piper")
    print("  Memory: 6-layer conversation memory with persistence")
    print("══════════════════════════════════════════════════════════")

    # Generate session ID
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Initialize conversation memory
    print("\n[init] Loading conversation memory…")
    memory = ConversationMemory(session_id, llm_context_window=LLM_CONTEXT_WINDOW)

    # Load the Whisper model (downloads on first run)
    print(f"[init] Loading faster-whisper model '{MODEL_SIZE}' …")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    print("[init] Model loaded.")

    # Pre-warm Ollama: load model weights into RAM so the first real
    # query hits a hot model instead of stalling 10-20s on disk load.
    # Using an empty prompt means zero tokens are generated — pure load.
    print("[init] Pre-warming Ollama model…")
    try:
        ollama.generate(model=LLM_MODEL, prompt="", keep_alive="1h")
        print("[init] Ollama warm-up done.")
    except Exception as _e:
        print(f"[init] ⚠️  Ollama warm-up failed (continuing anyway): {_e}")

    # Initialize API managers and delegator
    global _delegator, _memory
    _memory = memory
    print("[init] Loading AnimeManager & WeatherManager…")
    anime_mgr = AnimeManager()
    weather_mgr = WeatherManager()
    anime_mgr.warmup()
    _delegator = APIDelegator(
        anime_manager=anime_mgr,
        weather_manager=weather_mgr,
        llm_classify_fn=_llm_classify_intent,
    )
    print("[init] APIDelegator ready.")

    # Start all 5 threads (4 pipeline + 1 persistent TTS worker)
    threads = [
        threading.Thread(target=udp_receive_loop, daemon=True, name="udp-rx"),
        threading.Thread(target=vad_loop, daemon=True, name="vad"),
        threading.Thread(target=whisper_inference_loop, args=(model,), daemon=True, name="whisper"),
        threading.Thread(target=assistant_loop, args=(memory,), daemon=True, name="assistant"),
        threading.Thread(target=_tts_worker_loop, daemon=True, name="tts-worker"),
    ]
    for t in threads:
        t.start()

    set_state("SLEEPING")
    _send_text_udp("[ANIM:IDLE]")

    # Block until Ctrl+C
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[main] Shutting down …")
        # Signal threads to stop BEFORE saving memory — ensures record_turn()
        # cannot be mid-write on the assistant thread while save_all() reads
        # the same episodic_store list on the main thread.
        shutdown_event.set()
        for t in threads:
            t.join(timeout=15)  # allow up to 15s for Ollama stream to finish
        print("[main] Saving memory …")
        with _memory_lock:  # block until any in-progress record_turn() finishes
            memory.save_all()
    finally:
        _tts_sock.close()  # always close, even on unexpected exception
        _text_sock.close()
        print("[main] TTS & text sockets closed.", flush=True)
    print("[main] Goodbye.")


if __name__ == "__main__":
    main()
