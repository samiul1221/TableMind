# 🍡 TableMind — ESP32 and Raspberry Pi based Voice Assistant

An always-listening, locally-run voice assistant built on an ESP32 + Raspberry Pi, featuring a hand-drawn animated face, smart home control, and natural conversation powered by on-device LLMs.

---

## ✨ Features

- 🎤 **Wake word detection** — always listening via openWakeWord (`alexa` by default, swappable)
- 🗣️ **Speech-to-text** — fast, offline transcription using `faster-whisper`
- 🧠 **On-device LLM** — Ollama-powered responses (`qwen2.5:1.5b` / `llama3.2:3b`)
- 🔊 **Text-to-speech** — natural voice output via Piper TTS, streamed over UDP to ESP32
- 😊 **Animated face** — 22 bitmap animation sequences + code-drawn emotion faces on an SSD1306 OLED
- 💡 **Smart lamp control** — color, brightness, and 16 scene modes via HTTP
- 🔐 **Smart lock control** — lock / unlock / status via HTTP
- 🌤️ **Weather & environment** — real-time weather data via `WeatherManager`
- 🎌 **Anime queries** — AniList API integration via `AnimeManager`
- 🧠 **Conversation memory** — 6-layer persistent memory with session tracking
- 💡 **NeoPixel LED ring** — reactive lighting that mirrors assistant state
- ⏰ **MAX7219 clock display** — HH:MM clock with 12/24-hr toggle
- 🤝 **Touch sensors** — pet, stop TTS, toggle mute, cycle brightness

---

## 🗂️ Project Structure

```
mochi/
├── firmware/
│   └── udp_mic_stream.ino       # ESP32 firmware — audio streaming, LEDs, OLED face, tasks
├── assistant.py                 # Main pipeline: UDP → VAD → Whisper → LLM → TTS (5 threads)
├── api_delegator.py             # Routes queries to anime / weather managers or Ollama
├── anime_info_tools.py          # AniList GraphQL API client
├── environmental_manager.py     # Weather / environment data manager
├── memory.py                    # 6-layer conversation memory with persistence
├── config.json                  # Runtime configuration (IPs, ports, model names)
└── readme.md
```

---

## 🏗️ Architecture

```
ESP32 (mic) ──UDP 5000──► Pi: udp_receive_loop
                               │
                          audio_queue
                               │
                          vad_loop  ◄── openWakeWord (wake word)
                               │        webrtcvad (speech detection)
                          inference_queue
                               │
                          whisper_inference_loop
                               │
                          llm_queue
                               │
                          assistant_loop ──► APIDelegator (anime/weather)
                               │         └─► Ollama LLM
                               │
                          _tts_queue
                               │
                          tts_worker_loop ──► Piper TTS ──UDP 5001──► ESP32 (speaker)
                                         └──────────────UDP 5002──► ESP32 (OLED/LEDs/commands)
```

---

## 🛠️ Hardware

| Component | Purpose |
|---|---|
| ESP32 (38-pin) | Audio streaming, speaker output, LED control, OLED face |
| Raspberry Pi 4 / 5 | STT, LLM, TTS processing |
| INMP441 I2S mic | Voice capture |
| MAX98357A I2S speaker | TTS audio playback |
| SSD1306 128×64 OLED | Animated Mochi face |
| MAX7219 4×(8×8) matrix | HH:MM clock display |
| WS2812B NeoPixel strip (48) | State-reactive LED ring + rectangle |
| Smart lock ESP32-C3 | HTTP-controlled door lock |
| Smart lamp ESP32 | HTTP-controlled RGBW lamp |

---

## ⚙️ Pi Setup

### 1. Install Python dependencies

```bash
pip install faster-whisper webrtcvad openwakeword ollama numpy
```

### 2. Install Ollama and pull a model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:1.5b
```

### 3. Install Piper TTS

```bash
# Download piper binary and a voice model
# Place piper binary at ~/voice-assistant/piper/piper
# Place voice model at ~/voice-assistant/voices/en_US-ryan-medium.onnx
```

### 4. Configure IPs

Edit `config.json` (or the constants at the top of `assistant.py`):

```json
{
  "ESP32_IP": "192.168.0.2",
  "LOCK_IP":  "192.168.0.9",
  "LAMP_IP":  "192.168.0.7"
}
```

### 5. Run

```bash
python assistant.py
```

---

## ⚙️ ESP32 Setup

1. Open `firmware/udp_mic_stream.ino` in Arduino IDE
2. Set your WiFi credentials and Pi IP at the top of the file:
   ```cpp
   #define WIFI_SSID "your_ssid"
   #define WIFI_PASS "your_password"
   #define PI_IP     "192.168.0.8"
   ```
3. Install dependencies via Arduino Library Manager:
   - `Adafruit NeoPixel`
   - `Adafruit SSD1306` + `Adafruit GFX`
   - `MD_MAX72XX`
4. Flash to ESP32

---

## 🔌 UDP Port Map

| Port | Direction | Purpose |
|---|---|---|
| 5000 | ESP32 → Pi | Raw mic audio (16-bit PCM, 16 kHz) |
| 5001 | Pi → ESP32 | TTS audio (16-bit PCM, 16 kHz) |
| 5002 | Pi → ESP32 | Text, state commands, IoT tags |
| 5003 | ESP32 → Pi | Smart lock HTTP response relay |

---

## 🏷️ Command Tag Reference

Mochi uses inline tags that the LLM embeds in its responses. Tags are stripped before TTS playback.

### State tags (Pi → ESP32)
```
[STATE:SLEEPING]  [STATE:LISTENING]  [STATE:PROCESSING]  [STATE:SPEAKING]
```

### IoT command tags
```
[CMD:LAMP:ON]  [CMD:LAMP:OFF]
[CMD:LAMP:BRIGHTNESS:N]     N = 0–255
[CMD:LAMP:COLOR:R,G,B]
[CMD:LAMP:SCENE:X]          X = READING | OCEAN | RAINBOW | FIRE | STARS | ...
[CMD:LOCK:UNLOCK]  [CMD:LOCK:LOCK]  [CMD:LOCK:STATUS]
```

### Face / animation tags
```
[FACE:HAPPY]  [FACE:LAUGH]  [FACE:CRY]  [FACE:LOVE]  [FACE:WINK]
[FACE:UWU]    [FACE:SMIRK]  [FACE:DIZZY] [FACE:HEAD_PAT] [FACE:TONGUE]
[ANIM:IDLE]   [ANIM:EXCITED]  [ANIM:LAUGH]  [ANIM:CRYING]  ...
```

---

## 🎛️ Touch Controls

| Sensor | Short Tap | Long Press |
|---|---|---|
| Touch 1 (GPIO 36) | Head pat reaction | Toggle relaxation mode |
| Touch 2 (GPIO 39) | Skip / abort TTS | Toggle mic mute |
| Touch 3 (GPIO 34) | Toggle 12/24-hr clock | Cycle LED brightness |

---

## 🔄 Swapping the Wake Word

Change the `WAKE_WORD_MODEL` constant in `assistant.py`:

```python
WAKE_WORD_MODEL = "alexa"          # built-in
# WAKE_WORD_MODEL = "/path/to/hey_mochi.onnx"   # custom model
```

---

## 🔄 Swapping the LLM

```python
LLM_MODEL = "qwen2.5:1.5b"         # fast, lightweight
# LLM_MODEL = "llama3.2:3b-instruct-q4_K_M"   # smarter, slower
```

Any model available via `ollama list` works. Adjust `LLM_CONTEXT_WINDOW` accordingly.

---

## 📝 License

MIT
