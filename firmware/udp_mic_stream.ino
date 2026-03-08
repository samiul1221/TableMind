// udp_mic_stream.ino
// ─────────────────────────────────────────────────────────────────────
// ESP32 bidirectional audio over WiFi UDP:
//   • Continuously streams I2S mic audio TO the Pi  (port 5000)
//   • Receives TTS / WAV PCM audio FROM the Pi      (port 5001)
//     and plays it through the I2S speaker output
//   • NeoPixel LED ring + rectangle show current state
// ─────────────────────────────────────────────────────────────────────

#include <Adafruit_NeoPixel.h>
#include <MD_MAX72xx.h>
// MD_Parola removed — using MD_MAX72xx directly for pixel-exact clock layout
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <math.h>
#include <time.h>

#include "all_frames.h"  // 772 bitmap animation frames

// ─── NETWORK CONFIG ───────────────────────────────────────
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASS "YOUR_WIFI_PASSWORD"
#define PI_IP "YOUR_PI_IP"
#define TX_UDP_PORT 5000    // mic → Pi
#define RX_UDP_PORT 5001    // Pi TTS → ESP32
#define TEXT_UDP_PORT 5002  // Pi text/state → ESP32

// ─── SMART LOCK CONFIG ─────────────────────────────────────
#define LOCK_IP "YOUR_LOCK_ESP32_IP"  // ← set your lock ESP32's IP here
#define LOCK_PORT 80
#define LOCK_PASSWORD "x"
#define LOCK_RESP_UDP_PORT 5003  // Pi listens here for lock status replies

// ─── SMART LAMP CONFIG ─────────────────────────────────────
#define LAMP_IP "YOUR_LAMP_ESP32_IP"  // ← set your lamp ESP32's IP here
#define LAMP_PORT 80
#define LAMP_PASSWORD "x"
#define LAMP_ID "SmartLamp001"

// ─── I2S PINS ─────────────────────────────────────────────
#define I2S_WS 25    // Word Select (LRCK) — shared by mic & speaker
#define I2S_SCK 27   // Bit Clock (BCLK)   — shared
#define I2S_SD 33    // Mic serial data in
#define I2S_DOUT 26  // Speaker serial data out

// ─── AUDIO CONFIG ─────────────────────────────────────────
#define I2S_PORT I2S_NUM_0
#define SAMPLE_RATE 16000
#define PCM_BYTES 512  // bytes per mic UDP packet
#define PLAYBACK_BUF \
    512  // 512 samples × 2 = 1024 bytes — fits one WiFi frame (< 1500 MTU)
#define DMA_BUF_LEN 256  // DMA buffer length in samples
#define DMA_BUF_COUNT 8  // number of DMA buffers
#define VOLUME 70        // speaker test volume (0-100)
// TTS_SAMPLE_RATE (22050) is handled by the Pi before sending.
// The ESP32 always receives 16000 Hz PCM — no resampling needed.

// ─── MAX7219 Display Config ────────────────────────────────
#define MAX_DEVICES 4  // 4 chained 8×8 matrices = 32×8 display
#define CS_PIN 5
#define MOSI_PIN 23
#define CLK_PIN 18
#define HARDWARE_TYPE \
    MD_MAX72XX::FC16_HW  // FC16 is the standard 4-in-1 module type

// NTP
// NTP servers are passed inline to configTime() — three fallbacks for
// reliability
#define GMT_OFFSET_S \
    19800  // ← set your UTC offset in seconds (19800 = UTC+5:30 for India)
#define DST_OFFSET_S 0
// Clock display mode: true = 12-hour (with AM/PM), false = 24-hour
#define CLOCK_12HR true

// ─── LED CONFIG ───────────────────────────────────────────
#define LED_PIN 4
#define LED_COUNT 48
#define LED_BRIGHT 40  // 0-255

// LED segment indices
#define RING_START 0
#define RING_END 15
#define SIDE_A_START 16
#define SIDE_A_END 27
#define SIDE_B_START 28
#define SIDE_B_END 31
#define SIDE_C_START 32
#define SIDE_C_END 43
#define SIDE_D_START 44
#define SIDE_D_END 47

// ─── OLED CONFIG ──────────────────────────────────────────
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define OLED_I2C_ADDR 0x3C
#define FACE_REFRESH_MS 80  // min ms between OLED redraws (avoids I2C spam)

// ── Mochi face IDs ──
enum MochiFace {
    FACE_NORMAL,
    FACE_BLINK,
    FACE_PEACEFUL,
    FACE_YAWNING,
    FACE_CURIOUS_LEFT,
    FACE_CURIOUS_RIGHT,
    FACE_DISTRACTED_LEFT,
    FACE_DISTRACTED_RIGHT,
    FACE_SMILE,
    FACE_LOVE,
    FACE_WINK,
    FACE_UWU,
    FACE_LAUGH,
    FACE_CRYING,
    FACE_SMIRK,
    FACE_BLISSFUL,
    FACE_SNEEZE,
    FACE_TONGUE_OUT,
    FACE_DIZZY,
    FACE_HEAD_PAT,
    FACE_COUNT  // sentinel
};

// ── Bitmap animation sequence descriptor ──
struct AnimationSequence {
    int startFrame;
    int endFrame;
    bool loop;
    int fps;
};

// ── 22 pre-rendered animation sequences (frames stored in PROGMEM via
// all_frames.h) ──
const AnimationSequence ANIM_IDLE = {0, 35, true, 24};
const AnimationSequence ANIM_BLINK = {36, 48, false, 24};
const AnimationSequence ANIM_HAPPY = {49, 85, false, 24};
const AnimationSequence ANIM_EXCITED = {86, 130, false, 24};
const AnimationSequence ANIM_LOVE = {131, 175, false, 24};
const AnimationSequence ANIM_DUMB_LOVE = {176, 210, false, 24};
const AnimationSequence ANIM_UWU = {211, 250, false, 24};
const AnimationSequence ANIM_WINK_ANIM = {251, 280, false, 24};
const AnimationSequence ANIM_LAUGH = {281, 325, false, 24};
const AnimationSequence ANIM_AWKWARD_LAUGH = {326, 360, false, 24};
const AnimationSequence ANIM_SLEEPY = {361, 400, false, 24};
const AnimationSequence ANIM_CRYING = {401, 435, false, 24};
const AnimationSequence ANIM_CRYING_SMILE = {436, 470, false, 24};
const AnimationSequence ANIM_SNEEZE = {471, 505, false, 24};
const AnimationSequence ANIM_BIG_SNEEZE = {506, 540, false, 24};
const AnimationSequence ANIM_SMIRK_ANIM = {541, 580, false, 24};
const AnimationSequence ANIM_ANGRY = {581, 620, false, 24};
const AnimationSequence ANIM_ROAD_RAGE = {621, 655, false, 24};
const AnimationSequence ANIM_DISTRACTED = {656, 685, false, 24};
const AnimationSequence ANIM_DISTRACTED_2 = {686, 715, false, 24};
const AnimationSequence ANIM_LOOK_LEFT = {716, 745, false, 24};
const AnimationSequence ANIM_LOOK_RIGHT = {746, 771, false, 24};

// ── AI state enum — drives which face is shown ──
enum MochiAIState {
    AI_IDLE,
    AI_SLEEPING,
    AI_LISTENING,
    AI_PROCESSING,
    AI_SPEAKING
};

// ─── GLOBALS ──────────────────────────────────────────────
Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

WiFiUDP mic_udp;        // TX: mic stream to Pi
WiFiUDP rx_udp;         // RX: TTS audio from Pi
WiFiUDP text_udp;       // RX: text/state from Pi
WiFiUDP lock_resp_udp;  // TX: lock status replies → Pi

MD_MAX72XX mx =
    MD_MAX72XX(HARDWARE_TYPE, MOSI_PIN, CLK_PIN, CS_PIN, MAX_DEVICES);

Adafruit_SSD1306 oled(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ── Mochi face state (written by text_rx_task, read by face_update_task — both
// core 1) ──
volatile MochiAIState mochi_ai_state = AI_IDLE;
MochiFace mochi_current_face = (MochiFace)-1;  // -1 forces first draw
MochiFace mochi_reaction_face = FACE_NORMAL;
uint32_t mochi_reaction_end = 0;  // millis() when reaction overlay expires
uint32_t mochi_last_blink = 0;
uint32_t mochi_next_blink_interval = 5000;
uint32_t mochi_last_alt = 0;  // alternation timer
bool mochi_alt_toggle = false;

// ── Bitmap animation playback state ──
volatile bool mochi_anim_active = false;
int mochi_anim_start = 0;
int mochi_anim_end = 0;
int mochi_anim_frame = 0;
int mochi_anim_fps = 24;
bool mochi_anim_loop = false;
uint32_t mochi_anim_last_frame_ms = 0;

bool mochi_is_blinking = false;
uint32_t mochi_blink_end = 0;
uint32_t mochi_last_draw = 0;

// Queue shuttles fixed-size int16_t buffers from the UDP receiver
// task to the I2S playback task (depth = 8 packets).
QueueHandle_t playback_queue;

// Queue carries lock command IDs: 0=UNLOCK, 1=LOCK, 2=STATUS
QueueHandle_t lock_cmd_queue;

// ─── LAMP STATE GLOBALS ─────────────────────────────────────────
QueueHandle_t lamp_cmd_queue;
String lampAuthToken = "";          // current session token
unsigned long lampTokenExpiry = 0;  // millis() when token expires
const unsigned long LAMP_TOKEN_LIFETIME_MS =
    3500000UL;  // 3500 s (< 3600 s) (for show_done animation)
volatile bool playback_just_ended = false;

// Mutes mic stream during TTS playback to prevent echo/feedback loop.
// Set by audio_rx_task when packets arrive, cleared by audio_play_task
// when the queue drains.  The Pi also deafens during SPEAKING state
// as a safety net, but ESP32-side mute is the primary defence.
volatile bool tts_playing = false;

// State flags set by the text_rx_task based on Pi state messages
volatile bool pi_is_listening = false;
volatile bool pi_is_processing = false;

// Spinlock for cross-core access to tts_playing / playback_just_ended.
// Both cores read/write these bools, so volatile alone is not enough
// on the dual-core Xtensa — portMUX guarantees atomicity + visibility.
portMUX_TYPE tts_mux = portMUX_INITIALIZER_UNLOCKED;

// Spinlock for face state (mochi_ai_state, mochi_current_face,
// mochi_reaction_face, mochi_reaction_end, mochi_alt_toggle, mochi_last_alt).
// Written by text_rx_task, read by face_update_task — both on core 1.
// portMUX prevents torn reads/writes across FreeRTOS task preemptions.
portMUX_TYPE face_mux = portMUX_INITIALIZER_UNLOCKED;

// Spinlock guarding pi_is_listening / pi_is_processing:
// written by text_rx_task (core 1), read by loop() (core 0).
// volatile alone is NOT sufficient on dual-core Xtensa.
portMUX_TYPE state_mux = portMUX_INITIALIZER_UNLOCKED;

// ─── LED STATE MACHINE ────────────────────────────────────
// Each state gets a unique, instantly recognisable colour scheme:
//   BOOT          — white sweep (runs once in setup)
//   WIFI_CONNECT  — blue breathing ring
//   RECORDING     — red breathing ring (mic streaming, default)
//   BUFFERING     — yellow pulsing ring + orange rectangle fill
//   PLAYING       — green ring + blue rectangle chase
//   DONE          — green flash ×3 (then → RECORDING)
//   WIFI_LOST     — fast red blink on rectangle sides
enum LedState {
    LED_BOOT,
    LED_WIFI_CONNECT,
    LED_IDLE,
    LED_RECORDING,
    LED_LISTENING,
    LED_PROCESSING,
    LED_BUFFERING,
    LED_PLAYING,
    LED_DONE,
    LED_WIFI_LOST
};
volatile LedState led_state = LED_BOOT;

// ─── LED HELPERS ──────────────────────────────────────────

void set_segment(int from, int to, uint32_t color) {
    for (int i = from; i <= to; i++) leds.setPixelColor(i, color);
}

void set_rectangle(uint32_t color) {
    set_segment(SIDE_A_START, SIDE_A_END, color);
    set_segment(SIDE_B_START, SIDE_B_END, color);
    set_segment(SIDE_C_START, SIDE_C_END, color);
    set_segment(SIDE_D_START, SIDE_D_END, color);
}

void clear_all() {
    leds.clear();
    leds.show();
}

// Compute a 0-255 brightness that smoothly breathes using millis()
// period_ms controls speed (lower = faster)
static uint8_t breathe(uint16_t period_ms) {
    int phase = (millis() % period_ms) * 200 / period_ms;  // 0-199
    int val = (phase < 100) ? phase : (200 - phase);       // triangle 0-100-0
    return map(val, 0, 100, 20, 255);
}

// ── BOOT — white sweep (called once in setup) ──
void show_boot() {
    for (int i = 0; i < LED_COUNT; i++) {
        leds.setPixelColor(i, leds.Color(80, 80, 80));
        leds.show();
        delay(10);
    }
    delay(300);
    clear_all();
}

// ── WIFI CONNECTING — blue breathing ring ──
void show_wifi_connecting() {
    leds.clear();
    uint8_t b = breathe(1200);  // ~1.2 s cycle
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(0, 0, b));
    // Rectangle dim blue
    set_rectangle(leds.Color(0, 0, 15));
    leds.show();
}

// ── IDLE — all LEDs off (silent standby) ──
void show_idle() {
    leds.clear();
    leds.show();
}

// ── RECORDING — red breathing ring, rectangle off ──
void show_recording() {
    leds.clear();
    uint8_t b = breathe(2000);  // slow 2 s cycle
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(b, 0, 0));
    // Rectangle stays dark — only mic is active
    leds.show();
}

// ── LISTENING — solid cyan ring ──
void show_listening() {
    leds.clear();
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(0, 180, 180));
    leds.show();
}

// ── PROCESSING — purple chase/spin animation on ring ──
void show_processing() {
    leds.clear();
    int ring_len = RING_END - RING_START + 1;  // 16
    int chase_pos = (millis() / 60) % ring_len;
    int tail_len = 6;
    for (int i = RING_START; i <= RING_END; i++) {
        int idx = i - RING_START;
        int dist = (chase_pos - idx + ring_len) % ring_len;
        if (dist < tail_len) {
            uint8_t fade = map(dist, 0, tail_len, 200, 20);
            leds.setPixelColor(i, leds.Color(fade, 0, fade));  // purple
        }
    }
    leds.show();
}

// ── BUFFERING — yellow pulsing ring + orange rect fill ──
// Shown when TTS packets are arriving but queue is still filling
void show_buffering(int queue_depth) {
    leds.clear();
    uint8_t b = breathe(600);  // fast 0.6 s pulse

    // Ring: yellow pulse
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(b, b / 2, 0));

    // Rectangle: orange fill proportional to queue depth (0-8)
    int segments[4][2] = {{SIDE_A_START, SIDE_A_END},
                          {SIDE_B_START, SIDE_B_END},
                          {SIDE_C_START, SIDE_C_END},
                          {SIDE_D_START, SIDE_D_END}};
    int total_rect = 32;
    int fill = map(queue_depth, 0, 8, 0, total_rect);
    int counted = 0;
    for (int s = 0; s < 4; s++) {
        int len = segments[s][1] - segments[s][0] + 1;
        for (int j = 0; j < len; j++) {
            leds.setPixelColor(segments[s][0] + j, (counted < fill)
                                                       ? leds.Color(200, 80, 0)
                                                       : leds.Color(20, 8, 0));
            counted++;
        }
    }
    leds.show();
}

// ── PLAYING — green ring + blue rectangle chase ──
void show_playing(int queue_depth) {
    leds.clear();

    // Ring: solid green
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(0, 180, 0));

    // Rectangle: blue chase animation using millis()
    int segments[4][2] = {{SIDE_A_START, SIDE_A_END},
                          {SIDE_B_START, SIDE_B_END},
                          {SIDE_C_START, SIDE_C_END},
                          {SIDE_D_START, SIDE_D_END}};
    int total_rect = 32;
    int chase_pos = (millis() / 40) % total_rect;  // rotating chase head
    int tail_len =
        map(queue_depth, 0, 8, 4, total_rect);  // longer tail = more buffered

    int counted = 0;
    for (int s = 0; s < 4; s++) {
        int len = segments[s][1] - segments[s][0] + 1;
        for (int j = 0; j < len; j++) {
            // Distance behind the chase head (wrapping)
            int dist = (chase_pos - counted + total_rect) % total_rect;
            if (dist < tail_len) {
                uint8_t fade = map(dist, 0, tail_len, 200, 20);
                leds.setPixelColor(segments[s][0] + j, leds.Color(0, 0, fade));
            } else {
                leds.setPixelColor(segments[s][0] + j, leds.Color(0, 0, 10));
            }
            counted++;
        }
    }
    leds.show();
}

// ── DONE — flash green ring 3× ──
void show_done() {
    for (int f = 0; f < 3; f++) {
        set_segment(RING_START, RING_END, leds.Color(0, 200, 0));
        set_rectangle(leds.Color(0, 60, 0));
        leds.show();
        delay(150);
        clear_all();
        delay(100);
    }
}

// ── WIFI LOST — fast red blink on rectangle, dim red ring ──
void show_wifi_lost() {
    leds.clear();
    // Ring: dim steady red
    for (int i = RING_START; i <= RING_END; i++)
        leds.setPixelColor(i, leds.Color(60, 0, 0));
    // Rectangle: fast blink red (toggle every 250 ms)
    bool on = ((millis() / 250) % 2) == 0;
    set_rectangle(on ? leds.Color(200, 0, 0) : leds.Color(0, 0, 0));
    leds.show();
}

// ─── SPEAKER TEST ─────────────────────────────────────────
// Generates a pure sine-wave tone and writes it to I2S.
// Must be called AFTER setup_i2s() + i2s_start().
void play_tone(int freq_hz, int duration_ms) {
    int total_samples = (SAMPLE_RATE * duration_ms) / 1000;
    int32_t buf[PLAYBACK_BUF];
    int sent = 0;
    Serial.printf("[spk]  ♪ %dHz for %dms\n", freq_hz, duration_ms);
    while (sent < total_samples) {
        int chunk = min(PLAYBACK_BUF, total_samples - sent);
        for (int i = 0; i < chunk; i++) {
            float t = (float)(sent + i) / SAMPLE_RATE;
            // Amplitude scales with VOLUME (max ~16000 to avoid clipping)
            int16_t sample = (int16_t)((16000 * VOLUME / 100) *
                                       sinf(2.0f * M_PI * freq_hz * t));
            buf[i] = (int32_t)sample << 16;  // 16-bit PCM in upper word
        }
        size_t bytes_written = 0;
        i2s_write(I2S_PORT, buf, chunk * sizeof(int32_t), &bytes_written,
                  portMAX_DELAY);
        sent += chunk;
    }
}

// Three ascending beeps with yellow ring lit during the test
void speaker_test() {
    Serial.println("[spk]  === SPEAKER TEST ===");

    // Yellow ring = speaker test in progress
    leds.clear();
    set_segment(RING_START, RING_END, leds.Color(150, 100, 0));
    leds.show();

    play_tone(1000, 400);  // low beep
    delay(150);
    play_tone(1500, 400);  // mid beep
    delay(150);
    play_tone(2000, 400);  // high beep

    clear_all();
    Serial.println("[spk]  === SPEAKER TEST DONE ===");
}

// ─── I2S SETUP (TX + RX on same port) ────────────────────
void setup_i2s() {
    const i2s_config_t cfg = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,  // 32-bit I2S frames
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,   // mono
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = DMA_BUF_COUNT,
        .dma_buf_len = DMA_BUF_LEN,
        .use_apll = false,
        .tx_desc_auto_clear = true,  // silence when no data written
        .fixed_mclk = 0};
    ESP_ERROR_CHECK(i2s_driver_install(I2S_PORT, &cfg, 0, NULL));

    const i2s_pin_config_t pins = {
        .bck_io_num = I2S_SCK,
        .ws_io_num = I2S_WS,
        .data_out_num = I2S_DOUT,  // speaker output
        .data_in_num = I2S_SD      // mic input
    };
    ESP_ERROR_CHECK(i2s_set_pin(I2S_PORT, &pins));
}

// ═══════════════════════════════════════════════════════════
// TASK 1 — mic_stream  (core 1, unchanged from previous)
// Reads I2S mic, converts 32→16 bit, sends 512-byte UDP packets.
// ═══════════════════════════════════════════════════════════
void mic_stream_task(void* arg) {
    const int SAMPLES_PER_PACKET = PCM_BYTES / sizeof(int16_t);  // 256
    int32_t raw[SAMPLES_PER_PACKET];
    int16_t pcm[SAMPLES_PER_PACKET];

    for (;;) {
        size_t bytes_read = 0;
        // Always read I2S to keep DMA ring drained (prevents overflow)
        i2s_read(I2S_PORT, raw, sizeof(raw), &bytes_read, portMAX_DELAY);

        int n = bytes_read / sizeof(int32_t);
        for (int i = 0; i < n; i++) pcm[i] = (int16_t)(raw[i] >> 16);

        // Mute mic stream while TTS is playing to prevent echo feedback.
        // I2S read above still runs so the DMA buffers don't stall.
        bool muted;
        taskENTER_CRITICAL(&tts_mux);
        muted = tts_playing;
        taskEXIT_CRITICAL(&tts_mux);

        if (!muted) {
            mic_udp.beginPacket(PI_IP, TX_UDP_PORT);
            mic_udp.write((const uint8_t*)pcm, n * sizeof(int16_t));
            mic_udp.endPacket();
        }
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// TASK 2 — audio_rx  (core 0)
// Listens on RX_UDP_PORT for raw 16-bit PCM from the Pi,
// enqueues fixed-size buffers for the playback task.
// ═══════════════════════════════════════════════════════════
void audio_rx_task(void* arg) {
    // Scratch buffer sized for the largest expected UDP payload
    int16_t pkt[PLAYBACK_BUF];

    for (;;) {
        // parsePacket returns payload size; blocks briefly then returns 0
        int len = rx_udp.parsePacket();
        if (len > 0) {
            static int pkt_count = 0;
            Serial.printf("[rx] pkt #%d len=%d queue=%d\n", ++pkt_count, len,
                          uxQueueMessagesWaiting(playback_queue));

            // Read up to PLAYBACK_BUF samples (clamp oversized packets)
            int bytes_to_read = min(len, (int)sizeof(pkt));
            int got = rx_udp.read((uint8_t*)pkt, bytes_to_read);

            // Pad remainder with silence if packet was short
            int samples = got / sizeof(int16_t);
            for (int i = samples; i < PLAYBACK_BUF; i++) pkt[i] = 0;

            // Signal mic mute on first TTS packet
            taskENTER_CRITICAL(&tts_mux);
            tts_playing = true;
            taskEXIT_CRITICAL(&tts_mux);

            // Enqueue — if queue is full the oldest packet is NOT dropped;
            // we simply block up to 10 ms then give up (back-pressure).
            xQueueSend(playback_queue, pkt, pdMS_TO_TICKS(10));
        } else {
            // No packet ready — yield briefly to avoid busy-spin
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// TASK 3 — audio_play  (core 0)
// Pulls buffers from the queue, converts 16→32 bit, writes I2S.
// ═══════════════════════════════════════════════════════════
void audio_play_task(void* arg) {
    int16_t pcm[PLAYBACK_BUF];
    int32_t out[PLAYBACK_BUF];

    for (;;) {
        if (xQueueReceive(playback_queue, pcm, pdMS_TO_TICKS(100)) == pdTRUE) {
            // BUG 1 fix: Pi already resampled to 16000 Hz.
            // Just widen 16-bit PCM to 32-bit I2S frame — no resampling.
            for (int i = 0; i < PLAYBACK_BUF; i++) {
                int32_t sample =
                    (int32_t)pcm[i] * 4;  // 4× gain — adjust if too loud/quiet
                sample = max((int32_t)-32768, min((int32_t)32767, sample));
                out[i] = sample << 16;
            }

            size_t bytes_written = 0;
            i2s_write(I2S_PORT, out, PLAYBACK_BUF * sizeof(int32_t),
                      &bytes_written, portMAX_DELAY);

            // Queue drained → unmute mic, trigger done animation
            if (uxQueueMessagesWaiting(playback_queue) == 0) {
                taskENTER_CRITICAL(&tts_mux);
                tts_playing = false;
                playback_just_ended = true;
                taskEXIT_CRITICAL(&tts_mux);
            }
        }
    }
    vTaskDelete(NULL);
}

// ─── SMART LOCK HTTP HELPERS ──────────────────────────────
// Called ONLY from lock_http_task (blocking HTTP is OK there).

void http_lock_command(const char* endpoint) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[lock] ABORT — WiFi not connected");
        return;
    }
    HTTPClient http;
    String url = String("http://") + LOCK_IP + ":" + LOCK_PORT + endpoint;
    Serial.printf("[lock] POST %s\n", url.c_str());
    bool ok = http.begin(url);
    Serial.printf("[lock] http.begin() = %s\n", ok ? "OK" : "FAIL");
    if (!ok) {
        http.end();
        return;
    }
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + LOCK_PASSWORD);
    Serial.printf("[lock] Headers set — Auth: Bearer %s\n", LOCK_PASSWORD);
    int code = http.POST("{}");
    Serial.printf("[lock] HTTP response code: %d\n", code);
    String resp;
    if (code > 0) {
        resp = http.getString();
        Serial.printf("[lock] Response body: %s\n", resp.c_str());
    } else {
        resp = "{\"error\":\"http_fail\",\"code\":" + String(code) + "}";
        Serial.printf("[lock] HTTPClient error: %s\n",
                      http.errorToString(code).c_str());
    }
    lock_resp_udp.beginPacket(PI_IP, LOCK_RESP_UDP_PORT);
    lock_resp_udp.print(resp);
    lock_resp_udp.endPacket();
    http.end();
    Serial.printf("[lock] %s → %d %s\n", endpoint, code, resp.c_str());
}

void http_lock_status() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[lock] ABORT status — WiFi not connected");
        return;
    }
    HTTPClient http;
    String url = String("http://") + LOCK_IP + ":" + LOCK_PORT + "/api/status";
    Serial.printf("[lock] GET %s\n", url.c_str());
    bool ok = http.begin(url);
    Serial.printf("[lock] http.begin() = %s\n", ok ? "OK" : "FAIL");
    if (!ok) {
        http.end();
        return;
    }
    http.addHeader("Authorization", String("Bearer ") + LOCK_PASSWORD);
    Serial.printf("[lock] Header set — Auth: Bearer %s\n", LOCK_PASSWORD);
    int code = http.GET();
    Serial.printf("[lock] HTTP response code: %d\n", code);
    String resp;
    if (code > 0) {
        resp = http.getString();
        Serial.printf("[lock] Response body: %s\n", resp.c_str());
    } else {
        resp = "{\"error\":\"http_fail\",\"code\":" + String(code) + "}";
        Serial.printf("[lock] HTTPClient error: %s\n",
                      http.errorToString(code).c_str());
    }
    lock_resp_udp.beginPacket(PI_IP, LOCK_RESP_UDP_PORT);
    lock_resp_udp.print(resp);
    lock_resp_udp.endPacket();
    http.end();
    Serial.printf("[lock] /api/status → %d\n", code);
}

// ═══════════════════════════════════════════════════════════
// TASK 6 — lock_http  (core 0)
// Receives command IDs from lock_cmd_queue and executes
// blocking HTTP requests to the smart lock.
// ═══════════════════════════════════════════════════════════
void lock_http_task(void* arg) {
    int cmd;
    for (;;) {
        if (xQueueReceive(lock_cmd_queue, &cmd, portMAX_DELAY) == pdTRUE) {
            if (cmd == 0)
                http_lock_command("/api/unlock");
            else if (cmd == 1)
                http_lock_command("/api/lock");
            else if (cmd == 2)
                http_lock_status();
        }
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// SMART LAMP HTTP HELPERS
// Called ONLY from lamp_http_task (blocking HTTP is fine there).
// ═══════════════════════════════════════════════════════════

// Step-1 auth: POST /api/auth → extract + store session token
bool http_lamp_authenticate() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[lamp] ABORT auth — WiFi not connected");
        return false;
    }
    HTTPClient http;
    String url = String("http://") + LAMP_IP + ":" + LAMP_PORT + "/api/auth";
    Serial.printf("[lamp] AUTH POST %s\n", url.c_str());
    if (!http.begin(url)) {
        Serial.println("[lamp] auth http.begin() FAIL");
        http.end();
        return false;
    }
    http.addHeader("Content-Type", "application/json");
    String body = String("{\"password\":\"") + LAMP_PASSWORD + "\"}";
    int code = http.POST(body);
    Serial.printf("[lamp] auth response code: %d\n", code);
    if (code != 200) {
        Serial.printf("[lamp] auth FAILED: %s\n",
                      http.errorToString(code).c_str());
        http.end();
        return false;
    }
    String resp = http.getString();
    http.end();
    // Simple token extraction: find "token":"<value>"
    int ti = resp.indexOf("\"token\":\"");
    if (ti < 0) {
        Serial.println("[lamp] auth: no token in response");
        return false;
    }
    ti += 9;  // skip past '"token":"'
    int te = resp.indexOf('"', ti);
    if (te < 0) {
        Serial.println("[lamp] auth: malformed token field");
        return false;
    }
    lampAuthToken = resp.substring(ti, te);
    lampTokenExpiry = millis() + LAMP_TOKEN_LIFETIME_MS;
    Serial.printf("[lamp] auth OK — token: %s\n", lampAuthToken.c_str());
    return true;
}

// Core HTTP sender for all lamp endpoints
void http_lamp_send(const char* endpoint, const char* method,
                    const String& body) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[lamp] ABORT — WiFi not connected");
        return;
    }
    // Ensure valid token
    if (lampAuthToken.length() == 0 || millis() >= lampTokenExpiry) {
        Serial.println("[lamp] Token missing/expired — authenticating...");
        if (!http_lamp_authenticate()) return;
    }
    HTTPClient http;
    String url = String("http://") + LAMP_IP + ":" + LAMP_PORT + endpoint;
    Serial.printf("[lamp] %s %s  body=%s\n", method, url.c_str(),
                  body.length() ? body.c_str() : "(none)");
    if (!http.begin(url)) {
        Serial.println("[lamp] http.begin() FAIL");
        http.end();
        return;
    }
    http.addHeader("Authorization", String("Bearer ") + lampAuthToken);
    if (body.length()) http.addHeader("Content-Type", "application/json");

    int code;
    if (strcmp(method, "POST") == 0)
        code = http.POST(const_cast<String&>(body));
    else
        code = http.GET();

    Serial.printf("[lamp] response: %d\n", code);
    if (code == 401) {
        // Token rejected — re-auth once and retry
        Serial.println("[lamp] 401 — re-authenticating and retrying...");
        http.end();
        if (!http_lamp_authenticate()) return;
        if (!http.begin(url)) return;
        http.addHeader("Authorization", String("Bearer ") + lampAuthToken);
        if (body.length()) http.addHeader("Content-Type", "application/json");
        code = (strcmp(method, "POST") == 0)
                   ? http.POST(const_cast<String&>(body))
                   : http.GET();
        Serial.printf("[lamp] retry response: %d\n", code);
    }
    if (code > 0)
        Serial.printf("[lamp] body: %s\n", http.getString().c_str());
    else
        Serial.printf("[lamp] error: %s\n", http.errorToString(code).c_str());
    http.end();
}

void http_lamp_power(bool on) {
    http_lamp_send("/api/power", "POST",
                   on ? "{\"on\":true}" : "{\"on\":false}");
}

void http_lamp_brightness(int percent) {
    http_lamp_send("/api/brightness", "POST",
                   "{\"brightnessPercent\":" + String(percent) + "}");
}

void http_lamp_color(int r, int g, int b) {
    http_lamp_send("/api/color", "POST",
                   "{\"r\":" + String(r) + ",\"g\":" + String(g) +
                       ",\"b\":" + String(b) + "}");
}

void http_lamp_pattern(int index) {
    http_lamp_send("/api/pattern", "POST",
                   "{\"pattern\":" + String(index) + "}");
}

void http_lamp_scene(const String& sceneName) {
    // Map friendly scene names → pattern indices
    struct {
        const char* name;
        int idx;
    } scenes[] = {
        {"READING", 0}, {"OCEAN", 1},   {"RAINBOW", 2}, {"FIRE", 3},
        {"STARS", 4},   {"BREATHE", 5}, {"NIGHT", 11},  {"CANDLE", 12},
        {"AURORA", 13}, {"WAVES", 14},  {"SUNSET", 15}, {"METEOR", 16},
        {"STORM", 17},  {"RIPPLE", 18}, {"LAVA", 24},   {"FIREFLY", 22},
    };
    for (auto& s : scenes) {
        if (sceneName.equalsIgnoreCase(s.name)) {
            Serial.printf("[lamp] Scene %s → pattern %d\n", s.name, s.idx);
            http_lamp_pattern(s.idx);
            return;
        }
    }
    Serial.printf("[lamp] WARNING: unknown scene '%s'\n", sceneName.c_str());
}

// ═══════════════════════════════════════════════════════════
// TASK 7 — lamp_http  (core 0, priority 1)
// Blocks on lamp_cmd_queue; each item is a 64-byte command string.
// ═══════════════════════════════════════════════════════════
void lamp_http_task(void* pvParameters) {
    char token[64];
    for (;;) {
        if (xQueueReceive(lamp_cmd_queue, token, portMAX_DELAY) == pdTRUE) {
            String cmd = String(token);
            Serial.printf("[lamp] Dispatch: %s\n", cmd.c_str());
            if (cmd == "ON") {
                http_lamp_power(true);
            } else if (cmd == "OFF") {
                http_lamp_power(false);
            } else if (cmd.startsWith("BRIGHT:")) {
                http_lamp_brightness(cmd.substring(7).toInt());
            } else if (cmd.startsWith("COLOR:")) {
                // Token format: COLOR:R,G,B
                String vals = cmd.substring(6);
                int c1 = vals.indexOf(',');
                int c2 = vals.indexOf(',', c1 + 1);
                if (c1 > 0 && c2 > c1) {
                    int r = vals.substring(0, c1).toInt();
                    int g = vals.substring(c1 + 1, c2).toInt();
                    int b = vals.substring(c2 + 1).toInt();
                    http_lamp_color(r, g, b);
                } else {
                    Serial.printf("[lamp] malformed COLOR token: %s\n",
                                  cmd.c_str());
                }
            } else if (cmd.startsWith("PATTERN:")) {
                http_lamp_pattern(cmd.substring(8).toInt());
            } else if (cmd.startsWith("SCENE:")) {
                http_lamp_scene(cmd.substring(6));
            } else {
                Serial.printf("[lamp] Unknown token: %s\n", cmd.c_str());
            }
        }
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// MOCHI FACE SYSTEM — code-drawn emotion faces for SSD1306
// Ported from mochi_all_emotions.ino (code-drawn only, no bitmaps).
// Each function clears → draws → calls oled.display().
// ═══════════════════════════════════════════════════════════

// ── Helper: small heart (used by Love face) ──
static void mochi_drawHeart(int x, int y, int size) {
    oled.fillCircle(x, y + size, size, SSD1306_WHITE);
    oled.fillCircle(x + size * 2, y + size, size, SSD1306_WHITE);
    oled.fillTriangle(x - size, y + size, x + size, y + size * 4, x + size * 3,
                      y + size, SSD1306_WHITE);
}

// ---------- Normal ----------
static void mochi_Normal() {
    oled.clearDisplay();
    oled.fillRoundRect(28, 20, 24, 28, 8, SSD1306_WHITE);
    oled.fillRoundRect(76, 20, 24, 28, 8, SSD1306_WHITE);
    oled.drawLine(54, 56, 74, 56, SSD1306_WHITE);
    oled.drawPixel(52, 55, SSD1306_WHITE);
    oled.drawPixel(76, 55, SSD1306_WHITE);
    oled.drawPixel(53, 57, SSD1306_WHITE);
    oled.drawPixel(75, 57, SSD1306_WHITE);
    oled.display();
}

// ---------- Blink ----------
static void mochi_Blink() {
    oled.clearDisplay();
    for (int i = 0; i < 3; i++) {
        oled.drawLine(28, 34 + i, 52, 34 + i, SSD1306_WHITE);
    }
    for (int i = 0; i < 3; i++) {
        oled.drawLine(76, 34 + i, 100, 34 + i, SSD1306_WHITE);
    }
    oled.drawLine(54, 56, 74, 56, SSD1306_WHITE);
    oled.drawPixel(52, 55, SSD1306_WHITE);
    oled.drawPixel(76, 55, SSD1306_WHITE);
    oled.display();
}

// ---------- Peaceful (sleeping base) ----------
static void mochi_Peaceful() {
    oled.clearDisplay();
    oled.fillRoundRect(28, 26, 24, 14, 6, SSD1306_WHITE);
    oled.fillRect(28, 33, 24, 7, SSD1306_BLACK);
    for (int i = 0; i < 2; i++)
        oled.drawLine(30, 34 + i, 50, 34 + i, SSD1306_WHITE);
    oled.fillRoundRect(76, 26, 24, 14, 6, SSD1306_WHITE);
    oled.fillRect(76, 33, 24, 7, SSD1306_BLACK);
    for (int i = 0; i < 2; i++)
        oled.drawLine(78, 34 + i, 98, 34 + i, SSD1306_WHITE);
    oled.drawLine(52, 54, 58, 52, SSD1306_WHITE);
    oled.drawLine(58, 52, 70, 52, SSD1306_WHITE);
    oled.drawLine(70, 52, 76, 54, SSD1306_WHITE);
    oled.drawLine(52, 55, 58, 53, SSD1306_WHITE);
    oled.drawLine(58, 53, 70, 53, SSD1306_WHITE);
    oled.drawLine(70, 53, 76, 55, SSD1306_WHITE);
    oled.display();
}

// ---------- Yawning (sleeping alt) ----------
static void mochi_Yawning() {
    oled.clearDisplay();
    for (int i = 0; i < 3; i++) {
        oled.drawLine(28, 28 + i, 48, 28 + i, SSD1306_WHITE);
        oled.drawLine(80, 28 + i, 100, 28 + i, SSD1306_WHITE);
    }
    oled.drawPixel(27, 29, SSD1306_WHITE);
    oled.drawPixel(49, 29, SSD1306_WHITE);
    oled.drawPixel(79, 29, SSD1306_WHITE);
    oled.drawPixel(101, 29, SSD1306_WHITE);
    oled.fillRoundRect(50, 46, 28, 16, 8, SSD1306_WHITE);
    oled.fillRoundRect(53, 49, 22, 10, 5, SSD1306_BLACK);
    oled.drawPixel(26, 32, SSD1306_WHITE);
    oled.drawPixel(25, 33, SSD1306_WHITE);
    oled.drawPixel(102, 32, SSD1306_WHITE);
    oled.drawPixel(103, 33, SSD1306_WHITE);
    oled.display();
}

// ---------- Curious Left O.o (listening base) ----------
static void mochi_CuriousLeft() {
    oled.clearDisplay();
    oled.fillRoundRect(20, 16, 32, 36, 12, SSD1306_WHITE);
    oled.fillCircle(28, 24, 3, SSD1306_BLACK);
    oled.fillCircle(27, 23, 1, SSD1306_WHITE);
    oled.fillCircle(38, 32, 8, SSD1306_BLACK);
    oled.fillCircle(36, 30, 2, SSD1306_WHITE);
    oled.fillRoundRect(80, 26, 20, 18, 8, SSD1306_WHITE);
    oled.fillCircle(90, 34, 4, SSD1306_BLACK);
    oled.drawPixel(89, 33, SSD1306_WHITE);
    oled.drawCircle(64, 54, 4, SSD1306_WHITE);
    oled.drawCircle(64, 54, 5, SSD1306_WHITE);
    oled.fillCircle(64, 54, 3, SSD1306_BLACK);
    oled.fillCircle(14, 36, 2, SSD1306_WHITE);
    oled.display();
}

// ---------- Curious Right o.O (listening alt) ----------
static void mochi_CuriousRight() {
    oled.clearDisplay();
    oled.fillRoundRect(28, 26, 20, 18, 8, SSD1306_WHITE);
    oled.fillCircle(38, 34, 4, SSD1306_BLACK);
    oled.drawPixel(37, 33, SSD1306_WHITE);
    oled.fillRoundRect(76, 16, 32, 36, 12, SSD1306_WHITE);
    oled.fillCircle(100, 24, 3, SSD1306_BLACK);
    oled.fillCircle(101, 23, 1, SSD1306_WHITE);
    oled.fillCircle(90, 32, 8, SSD1306_BLACK);
    oled.fillCircle(92, 30, 2, SSD1306_WHITE);
    oled.drawCircle(64, 54, 4, SSD1306_WHITE);
    oled.drawCircle(64, 54, 5, SSD1306_WHITE);
    oled.fillCircle(64, 54, 3, SSD1306_BLACK);
    oled.fillCircle(114, 36, 2, SSD1306_WHITE);
    oled.display();
}

// ---------- Distracted Left (thinking/processing) ----------
static void mochi_DistractedLeft() {
    oled.clearDisplay();
    oled.fillRoundRect(24, 24, 24, 24, 8, SSD1306_WHITE);
    oled.fillRoundRect(72, 24, 24, 24, 8, SSD1306_WHITE);
    oled.fillCircle(30, 34, 6, SSD1306_BLACK);
    oled.fillCircle(78, 34, 6, SSD1306_BLACK);
    oled.drawLine(56, 54, 68, 54, SSD1306_WHITE);
    oled.drawPixel(54, 53, SSD1306_WHITE);
    oled.drawPixel(70, 53, SSD1306_WHITE);
    oled.drawLine(8, 20, 10, 16, SSD1306_WHITE);
    oled.drawLine(12, 24, 14, 20, SSD1306_WHITE);
    oled.drawPixel(10, 28, SSD1306_WHITE);
    oled.display();
}

// ---------- Smile (speaking base) ----------
static void mochi_Smile() {
    oled.clearDisplay();
    oled.fillRoundRect(28, 20, 24, 26, 8, SSD1306_WHITE);
    oled.fillRoundRect(76, 20, 24, 26, 8, SSD1306_WHITE);
    for (int i = 0; i < 3; i++) {
        oled.drawCircle(64, 46, 14 + i, SSD1306_WHITE);
    }
    oled.fillRect(50, 46, 28, 10, SSD1306_BLACK);
    oled.drawLine(56, 48, 56, 52, SSD1306_WHITE);
    oled.drawLine(60, 48, 60, 52, SSD1306_WHITE);
    oled.drawLine(64, 48, 64, 52, SSD1306_WHITE);
    oled.drawLine(68, 48, 68, 52, SSD1306_WHITE);
    oled.drawLine(72, 48, 72, 52, SSD1306_WHITE);
    oled.display();
}

// ---------- Love (heart eyes — CMD reaction) ----------
static void mochi_Love() {
    oled.clearDisplay();
    oled.fillCircle(32, 26, 6, SSD1306_WHITE);
    oled.fillCircle(42, 26, 6, SSD1306_WHITE);
    oled.fillTriangle(26, 26, 37, 40, 48, 26, SSD1306_WHITE);
    oled.fillCircle(80, 26, 6, SSD1306_WHITE);
    oled.fillCircle(90, 26, 6, SSD1306_WHITE);
    oled.fillTriangle(74, 26, 85, 40, 96, 26, SSD1306_WHITE);
    for (int i = 0; i < 3; i++) {
        oled.drawCircle(64, 48, 12 + i, SSD1306_WHITE);
    }
    oled.fillRect(52, 48, 24, 8, SSD1306_BLACK);
    mochi_drawHeart(10, 8, 2);
    mochi_drawHeart(106, 8, 2);
    oled.display();
}

// ---------- Wink (CMD reaction) ----------
static void mochi_Wink() {
    oled.clearDisplay();
    oled.fillRoundRect(26, 22, 24, 26, 8, SSD1306_WHITE);
    oled.fillCircle(38, 32, 5, SSD1306_BLACK);
    oled.fillCircle(39, 31, 2, SSD1306_WHITE);
    for (int i = 0; i < 4; i++) {
        oled.drawLine(78, 30 + i, 100, 30 + i, SSD1306_WHITE);
    }
    oled.fillCircle(78, 31, 2, SSD1306_WHITE);
    oled.fillCircle(100, 31, 2, SSD1306_WHITE);
    for (int i = 0; i < 2; i++) {
        oled.drawCircle(64, 48, 10 + i, SSD1306_WHITE);
    }
    oled.fillRect(54, 48, 20, 6, SSD1306_BLACK);
    oled.fillCircle(106, 38, 3, SSD1306_WHITE);
    oled.display();
}

// ---------- UwU ----------
static void mochi_UwU() {
    oled.clearDisplay();
    for (int i = 0; i < 4; i++) {
        oled.drawLine(26, 28 + i, 50, 28 + i, SSD1306_WHITE);
        oled.drawLine(78, 28 + i, 102, 28 + i, SSD1306_WHITE);
    }
    oled.fillCircle(28, 29, 2, SSD1306_WHITE);
    oled.fillCircle(48, 29, 2, SSD1306_WHITE);
    oled.fillCircle(80, 29, 2, SSD1306_WHITE);
    oled.fillCircle(100, 29, 2, SSD1306_WHITE);
    oled.drawLine(52, 50, 56, 54, SSD1306_WHITE);
    oled.drawLine(56, 54, 64, 50, SSD1306_WHITE);
    oled.drawLine(64, 50, 72, 54, SSD1306_WHITE);
    oled.drawLine(72, 54, 76, 50, SSD1306_WHITE);
    oled.fillCircle(16, 36, 4, SSD1306_WHITE);
    oled.fillCircle(112, 36, 4, SSD1306_WHITE);
    oled.display();
}

// ---------- Distracted Right (thinking/processing alt) ----------
static void mochi_DistractedRight() {
    oled.clearDisplay();
    oled.fillRoundRect(32, 24, 24, 24, 8, SSD1306_WHITE);
    oled.fillRoundRect(80, 24, 24, 24, 8, SSD1306_WHITE);
    oled.fillCircle(50, 34, 6, SSD1306_BLACK);
    oled.fillCircle(98, 34, 6, SSD1306_BLACK);
    oled.drawLine(60, 54, 72, 54, SSD1306_WHITE);
    oled.drawPixel(58, 53, SSD1306_WHITE);
    oled.drawPixel(74, 53, SSD1306_WHITE);
    oled.drawLine(118, 20, 120, 16, SSD1306_WHITE);
    oled.drawLine(114, 24, 116, 20, SSD1306_WHITE);
    oled.drawPixel(118, 28, SSD1306_WHITE);
    oled.display();
}

// ---------- Laugh (X eyes, wide mouth) ----------
static void mochi_Laugh() {
    oled.clearDisplay();
    for (int i = 0; i < 3; i++) {
        oled.drawLine(26 + i, 24, 50 + i, 36, SSD1306_WHITE);
        oled.drawLine(50 - i, 24, 26 - i, 36, SSD1306_WHITE);
    }
    for (int i = 0; i < 3; i++) {
        oled.drawLine(78 + i, 24, 102 + i, 36, SSD1306_WHITE);
        oled.drawLine(102 - i, 24, 78 - i, 36, SSD1306_WHITE);
    }
    for (int i = 0; i < 4; i++) {
        oled.drawCircle(64, 48, 16 + i, SSD1306_WHITE);
    }
    oled.fillRect(48, 48, 32, 12, SSD1306_BLACK);
    oled.fillCircle(64, 54, 10, SSD1306_WHITE);
    oled.drawLine(18, 28, 20, 32, SSD1306_WHITE);
    oled.drawLine(16, 32, 18, 36, SSD1306_WHITE);
    oled.drawLine(110, 28, 108, 32, SSD1306_WHITE);
    oled.drawLine(112, 32, 110, 36, SSD1306_WHITE);
    oled.display();
}

// ---------- Crying (sad eyes + tears) ----------
static void mochi_Crying() {
    oled.clearDisplay();
    for (int i = 0; i < 5; i++) {
        oled.drawLine(26, 28 + i, 50, 28 + i, SSD1306_WHITE);
        oled.drawLine(76, 28 + i, 100, 28 + i, SSD1306_WHITE);
    }
    oled.fillCircle(24, 31, 2, SSD1306_WHITE);
    oled.fillCircle(52, 31, 2, SSD1306_WHITE);
    oled.fillCircle(74, 31, 2, SSD1306_WHITE);
    oled.fillCircle(102, 31, 2, SSD1306_WHITE);
    for (int i = 0; i < 3; i++) {
        oled.drawCircle(64, 42, 11 + i, SSD1306_WHITE);
    }
    oled.fillRect(50, 30, 28, 12, SSD1306_BLACK);
    oled.fillCircle(20, 36, 4, SSD1306_WHITE);
    oled.fillCircle(19, 41, 4, SSD1306_WHITE);
    oled.fillCircle(18, 46, 3, SSD1306_WHITE);
    oled.fillCircle(17, 50, 3, SSD1306_WHITE);
    oled.fillCircle(16, 54, 2, SSD1306_WHITE);
    oled.fillCircle(108, 36, 4, SSD1306_WHITE);
    oled.fillCircle(109, 41, 4, SSD1306_WHITE);
    oled.fillCircle(110, 46, 3, SSD1306_WHITE);
    oled.fillCircle(111, 50, 3, SSD1306_WHITE);
    oled.fillCircle(112, 54, 2, SSD1306_WHITE);
    oled.display();
}

// ---------- Smirk (asymmetric) ----------
static void mochi_Smirk() {
    oled.clearDisplay();
    oled.fillRoundRect(28, 22, 24, 24, 8, SSD1306_WHITE);
    oled.fillCircle(42, 32, 5, SSD1306_BLACK);
    oled.drawPixel(40, 30, SSD1306_WHITE);
    oled.fillRoundRect(78, 26, 22, 18, 8, SSD1306_WHITE);
    oled.fillCircle(89, 34, 4, SSD1306_BLACK);
    oled.drawPixel(88, 33, SSD1306_WHITE);
    oled.drawLine(50, 54, 55, 53, SSD1306_WHITE);
    oled.drawLine(55, 53, 60, 52, SSD1306_WHITE);
    oled.drawLine(60, 52, 66, 52, SSD1306_WHITE);
    oled.drawLine(66, 52, 72, 51, SSD1306_WHITE);
    oled.drawLine(72, 51, 78, 50, SSD1306_WHITE);
    oled.drawLine(50, 55, 55, 54, SSD1306_WHITE);
    oled.drawLine(55, 54, 60, 53, SSD1306_WHITE);
    oled.drawLine(60, 53, 66, 53, SSD1306_WHITE);
    oled.drawLine(66, 53, 72, 52, SSD1306_WHITE);
    oled.drawLine(72, 52, 78, 51, SSD1306_WHITE);
    oled.fillCircle(110, 40, 3, SSD1306_WHITE);
    oled.display();
}

// ---------- Blissful (content, nearly closed eyes, big smile + blush)
// ----------
static void mochi_Blissful() {
    oled.clearDisplay();
    for (int i = 0; i < 3; i++) {
        oled.drawLine(30, 30 + i, 48, 30 + i, SSD1306_WHITE);
        oled.drawLine(80, 30 + i, 98, 30 + i, SSD1306_WHITE);
    }
    oled.drawPixel(29, 31, SSD1306_WHITE);
    oled.drawPixel(49, 31, SSD1306_WHITE);
    oled.drawPixel(79, 31, SSD1306_WHITE);
    oled.drawPixel(99, 31, SSD1306_WHITE);
    for (int i = 0; i < 3; i++) {
        oled.drawCircle(64, 48, 12 + i, SSD1306_WHITE);
    }
    oled.fillRect(52, 48, 24, 8, SSD1306_BLACK);
    oled.fillCircle(18, 36, 3, SSD1306_WHITE);
    oled.fillCircle(110, 36, 3, SSD1306_WHITE);
    oled.display();
}

// ---------- Sneeze (eyes shut, open mouth, particles) ----------
static void mochi_Sneeze() {
    oled.clearDisplay();
    for (int i = 0; i < 6; i++) {
        oled.drawLine(26, 28 + i, 52, 28 + i, SSD1306_WHITE);
        oled.drawLine(76, 28 + i, 102, 28 + i, SSD1306_WHITE);
    }
    oled.fillCircle(64, 50, 8, SSD1306_WHITE);
    oled.drawPixel(80, 48, SSD1306_WHITE);
    oled.drawPixel(85, 46, SSD1306_WHITE);
    oled.drawPixel(90, 50, SSD1306_WHITE);
    oled.drawPixel(95, 48, SSD1306_WHITE);
    oled.drawPixel(88, 52, SSD1306_WHITE);
    oled.display();
}

// ---------- Tongue Out (playful wink + tongue) ----------
static void mochi_TongueOut() {
    oled.clearDisplay();
    for (int i = 0; i < 4; i++) {
        oled.drawLine(28, 28 + i, 50, 28 + i, SSD1306_WHITE);
    }
    oled.fillCircle(30, 29, 2, SSD1306_WHITE);
    oled.fillCircle(48, 29, 2, SSD1306_WHITE);
    oled.fillRoundRect(76, 22, 24, 24, 8, SSD1306_WHITE);
    oled.drawLine(54, 50, 74, 50, SSD1306_WHITE);
    oled.drawLine(54, 50, 52, 54, SSD1306_WHITE);
    oled.drawLine(74, 50, 76, 54, SSD1306_WHITE);
    oled.drawLine(52, 54, 76, 54, SSD1306_WHITE);
    oled.fillCircle(64, 56, 4, SSD1306_WHITE);
    oled.fillCircle(64, 58, 3, SSD1306_WHITE);
    oled.drawPixel(64, 60, SSD1306_WHITE);
    oled.display();
}

// ---------- Dizzy (spiral eyes, wavy mouth) ----------
static void mochi_Dizzy() {
    oled.clearDisplay();
    oled.drawCircle(38, 30, 10, SSD1306_WHITE);
    oled.drawCircle(38, 30, 7, SSD1306_WHITE);
    oled.drawCircle(38, 30, 4, SSD1306_WHITE);
    oled.fillCircle(38, 30, 2, SSD1306_WHITE);
    oled.drawCircle(90, 30, 10, SSD1306_WHITE);
    oled.drawCircle(90, 30, 7, SSD1306_WHITE);
    oled.drawCircle(90, 30, 4, SSD1306_WHITE);
    oled.fillCircle(90, 30, 2, SSD1306_WHITE);
    for (int x = 48; x < 80; x += 2) {
        int y = 50 + (int)(3 * sinf((x - 48) * 0.3f));
        oled.drawPixel(x, y, SSD1306_WHITE);
        oled.drawPixel(x + 1, y, SSD1306_WHITE);
    }
    oled.drawPixel(22, 20, SSD1306_WHITE);
    oled.drawPixel(23, 20, SSD1306_WHITE);
    oled.drawPixel(104, 20, SSD1306_WHITE);
    oled.drawPixel(105, 20, SSD1306_WHITE);
    oled.display();
}

// ---------- Head Pat (closed happy eyes + hearts) ----------
static void mochi_HeadPat() {
    oled.clearDisplay();
    for (int i = 0; i < 4; i++) {
        oled.drawLine(28, 28 + i, 52, 28 + i, SSD1306_WHITE);
        oled.drawLine(76, 28 + i, 100, 28 + i, SSD1306_WHITE);
    }
    oled.fillCircle(30, 29, 2, SSD1306_WHITE);
    oled.fillCircle(50, 29, 2, SSD1306_WHITE);
    oled.fillCircle(78, 29, 2, SSD1306_WHITE);
    oled.fillCircle(98, 29, 2, SSD1306_WHITE);
    for (int i = 0; i < 3; i++) {
        oled.drawCircle(64, 48, 12 + i, SSD1306_WHITE);
    }
    oled.fillRect(52, 48, 24, 8, SSD1306_BLACK);
    mochi_drawHeart(25, 4, 3);
    mochi_drawHeart(100, 4, 3);
    oled.display();
}

// ── Draw any face by enum ID ──
static void mochi_draw(MochiFace face) {
    switch (face) {
        case FACE_NORMAL:
            mochi_Normal();
            break;
        case FACE_BLINK:
            mochi_Blink();
            break;
        case FACE_PEACEFUL:
            mochi_Peaceful();
            break;
        case FACE_YAWNING:
            mochi_Yawning();
            break;
        case FACE_CURIOUS_LEFT:
            mochi_CuriousLeft();
            break;
        case FACE_CURIOUS_RIGHT:
            mochi_CuriousRight();
            break;
        case FACE_DISTRACTED_LEFT:
            mochi_DistractedLeft();
            break;
        case FACE_SMILE:
            mochi_Smile();
            break;
        case FACE_LOVE:
            mochi_Love();
            break;
        case FACE_WINK:
            mochi_Wink();
            break;
        case FACE_UWU:
            mochi_UwU();
            break;
        case FACE_DISTRACTED_RIGHT:
            mochi_DistractedRight();
            break;
        case FACE_LAUGH:
            mochi_Laugh();
            break;
        case FACE_CRYING:
            mochi_Crying();
            break;
        case FACE_SMIRK:
            mochi_Smirk();
            break;
        case FACE_BLISSFUL:
            mochi_Blissful();
            break;
        case FACE_SNEEZE:
            mochi_Sneeze();
            break;
        case FACE_TONGUE_OUT:
            mochi_TongueOut();
            break;
        case FACE_DIZZY:
            mochi_Dizzy();
            break;
        case FACE_HEAD_PAT:
            mochi_HeadPat();
            break;
        default:
            mochi_Normal();
            break;
    }
}

// ═══════════════════════════════════════════════════════════
// NON-BLOCKING FACE MANAGER — called from loop() every tick.
// Handles state→face mapping, alternation, periodic blink,
// and 2-second reaction overlays for CMD acknowledgements.
// ═══════════════════════════════════════════════════════════

// Set a temporary reaction face (2 s default) then return to base.
void mochi_show_reaction(MochiFace face, uint32_t duration_ms = 2000) {
    taskENTER_CRITICAL(&face_mux);
    mochi_anim_active = false;  // cancel any running animation
    mochi_reaction_face = face;
    mochi_reaction_end = millis() + duration_ms;
    mochi_current_face = (MochiFace)-1;  // force immediate redraw
    taskEXIT_CRITICAL(&face_mux);
}

// Start a bitmap animation sequence. Cancels any reaction overlay.
void mochi_start_animation(const AnimationSequence& seq) {
    taskENTER_CRITICAL(&face_mux);
    mochi_anim_active = true;
    mochi_anim_start = seq.startFrame;
    mochi_anim_end = seq.endFrame;
    mochi_anim_frame = seq.startFrame;
    mochi_anim_fps = seq.fps;
    mochi_anim_loop = seq.loop;
    mochi_anim_last_frame_ms = millis();
    mochi_reaction_end = 0;              // cancel any reaction overlay
    mochi_current_face = (MochiFace)-1;  // force redraw when anim ends
    taskEXIT_CRITICAL(&face_mux);
}

// Change the AI state (called from text_rx_task on core 1).
void mochi_set_state(MochiAIState state) {
    taskENTER_CRITICAL(&face_mux);
    if (mochi_ai_state != state) {
        mochi_ai_state = state;
        // Cancel LOOPING animations (e.g. ANIM_IDLE) immediately so the new
        // state face shows without delay.  NON-looping reactions (LAUGH, LOVE,
        // CRYING, etc.) are intentionally preserved: they were triggered just
        // before the state change and must play to completion — killing them
        // here would truncate the animation to the ~few-ms gap between the
        // Python sending [ANIM:*] and the follow-up [STATE:*].
        if (mochi_anim_loop) mochi_anim_active = false;
        mochi_alt_toggle = false;
        mochi_last_alt = millis();
        mochi_current_face = (MochiFace)-1;  // force redraw
    }
    taskEXIT_CRITICAL(&face_mux);
}

void update_mochi_face() {
    uint32_t now = millis();

    // ── Bitmap animation playback (runs at animation FPS, bypasses
    // FACE_REFRESH_MS) ──
    if (mochi_anim_active) {
        uint32_t frame_delay = 1000 / mochi_anim_fps;
        if (now - mochi_anim_last_frame_ms >= frame_delay) {
            oled.clearDisplay();
            oled.drawBitmap(0, 0, frames[mochi_anim_frame], FRAME_WIDTH,
                            FRAME_HEIGHT, SSD1306_WHITE);
            oled.display();
            mochi_anim_last_frame_ms = now;
            mochi_anim_frame++;
            if (mochi_anim_frame > mochi_anim_end) {
                if (mochi_anim_loop) {
                    mochi_anim_frame = mochi_anim_start;
                } else {
                    taskENTER_CRITICAL(&face_mux);
                    mochi_anim_active = false;
                    mochi_current_face = (MochiFace)-1;
                    taskEXIT_CRITICAL(&face_mux);
                }
            }
        }
        return;  // skip code-drawn face logic while animation plays
    }

    // ── Code-drawn face logic (throttled at FACE_REFRESH_MS) ──
    if (now - mochi_last_draw < FACE_REFRESH_MS) return;
    mochi_last_draw = now;

    // Snapshot shared state atomically — both tasks are on core 1, but
    // mochi_show_reaction() / mochi_set_state() update multiple variables;
    // portMUX prevents torn reads across a context switch mid-update.
    taskENTER_CRITICAL(&face_mux);
    MochiAIState ai_state = mochi_ai_state;
    MochiFace reaction_face = mochi_reaction_face;
    uint32_t reaction_end = mochi_reaction_end;
    MochiFace current_face = mochi_current_face;
    taskEXIT_CRITICAL(&face_mux);

    MochiFace target;
    bool clear_reaction = false;

    // 1. Reaction overlay takes priority (2 s flash on CMD)
    if (reaction_end > 0 && now < reaction_end) {
        target = reaction_face;
    }
    // 2. Periodic blink (200 ms)
    else if (mochi_is_blinking && now < mochi_blink_end) {
        target = FACE_BLINK;
    }
    // 3. Map AI state → face (with alternation)
    else {
        // Clear expired blink / reaction
        mochi_is_blinking = false;
        if (reaction_end > 0 && now >= reaction_end) clear_reaction = true;

        // Check if it's time to trigger a blink
        if (now - mochi_last_blink >= mochi_next_blink_interval) {
            mochi_is_blinking = true;
            mochi_blink_end = now + 200;
            mochi_last_blink = now;
            // Randomise next blink interval: 4-8 s
            mochi_next_blink_interval = 4000 + (esp_random() % 4001);
            target = FACE_BLINK;
        } else {
            // Alternation toggle (varies by state)
            uint32_t alt_period;
            switch (ai_state) {
                case AI_SLEEPING:
                    alt_period = 0;  // static — Python server drives idle faces
                    break;
                case AI_LISTENING:
                    alt_period = 0;  // static — Python server drives idle faces
                    break;
                case AI_PROCESSING:
                    alt_period =
                        2000;  // alternate distracted L/R while thinking
                    break;
                case AI_SPEAKING:
                    alt_period = 3000;
                    break;
                default:
                    alt_period = 0;
                    break;
            }
            if (alt_period > 0 && now - mochi_last_alt >= alt_period) {
                mochi_alt_toggle = !mochi_alt_toggle;
                mochi_last_alt = now;
            }

            switch (ai_state) {
                case AI_SLEEPING:
                    target = FACE_PEACEFUL;  // static; Python sends [FACE:YAWN]
                                             // overlays
                    break;
                case AI_LISTENING:
                    target = FACE_CURIOUS_LEFT;  // static; Python sends
                                                 // [FACE:*] overlays
                    break;
                case AI_PROCESSING:
                    target = mochi_alt_toggle ? FACE_DISTRACTED_RIGHT
                                              : FACE_DISTRACTED_LEFT;
                    break;
                case AI_SPEAKING:
                    target = mochi_alt_toggle ? FACE_NORMAL : FACE_SMILE;
                    break;
                case AI_IDLE:
                default:
                    target = FACE_NORMAL;
                    break;
            }
        }
    }

    // Commit state and redraw only if face changed.
    // mochi_draw() (I2C) runs OUTSIDE the critical section.
    if (target != current_face || clear_reaction) {
        taskENTER_CRITICAL(&face_mux);
        if (target != current_face) mochi_current_face = target;
        if (clear_reaction) mochi_reaction_end = 0;
        taskEXIT_CRITICAL(&face_mux);
        if (target != current_face) mochi_draw(target);
    }
}

// ═══════════════════════════════════════════════════════════
// TASK 8 — face_update  (core 1, priority 1)
// Drives the Mochi OLED face state machine every ~80 ms.
// MUST run on core 1 — all I2C (Wire/OLED) calls must stay on
// one core; face_update_task and clock_display_task both run on
// core 1 and never issue Wire calls concurrently.
// ═══════════════════════════════════════════════════════════
void face_update_task(void* arg) {
    for (;;) {
        update_mochi_face();  // self-throttles at FACE_REFRESH_MS
        vTaskDelay(
            pdMS_TO_TICKS(10));  // yield; avoids starving other core-1 tasks
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// Classic 5×7 font — IBM/Hitachi LCD character ROM digits.
// 5 columns wide × 7 rows tall; bit 0 = top pad (always 0),
// bit 1 = row 1 (top of digit), bit 7 = row 7 (bottom).
// Derived from standard 5×7 ROM: original << 1 = shift to bit1.
// ═══════════════════════════════════════════════════════════
static const uint8_t FONT_5x7[11][5] = {
    //  .XXX.   X...X   X...X   X...X   X...X   X...X   .XXX.
    {0x7C, 0xA2, 0x92, 0x8A, 0x7C},  // 0
    //  ..X..   .XX..   ..X..   ..X..   ..X..   ..X..   .XXX.
    {0x00, 0x84, 0xFE, 0x80, 0x00},  // 1
    //  .XXX.   X...X   ....X   ..XX.   .X...   X....   XXXXX
    {0x84, 0xC2, 0xA2, 0x92, 0x8C},  // 2
    //  .XXX.   X...X   ....X   ..XX.   ....X   X...X   .XXX.
    {0x42, 0x82, 0x8A, 0x96, 0x62},  // 3
    //  ..XX.   .X.X.   X..X.   XXXXX   ...X.   ...X.   ...X.
    {0x30, 0x28, 0x24, 0xFE, 0x20},  // 4
    //  XXXXX   X....   XXXX.   ....X   ....X   X...X   .XXX.
    {0x4E, 0x8A, 0x8A, 0x8A, 0x72},  // 5
    //  ..XX.   .X...   X....   XXXX.   X...X   X...X   .XXX.
    {0x78, 0x94, 0x92, 0x92, 0x60},  // 6
    //  XXXXX   ....X   ...X.   ..X..   .X...   .X...   .X...
    {0x02, 0xE2, 0x12, 0x0A, 0x06},  // 7
    //  .XXX.   X...X   X...X   .XXX.   X...X   X...X   .XXX.
    {0x6C, 0x92, 0x92, 0x92, 0x6C},  // 8
    //  .XXX.   X...X   X...X   .XXXX   ....X   ....X   .XXX.
    {0x0C, 0x92, 0x92, 0x92, 0x7C},  // 9
    //  blank (suppressed leading zero)
    {0x00, 0x00, 0x00, 0x00, 0x00},  // 10 = blank
};

// Colon: 1 column, dots at rows 2 and 5 → bits 2 and 5 = 0x24
static const uint8_t COLON_DOTS = 0x24;

// Map logical column (0 = leftmost visual) to FC16 physical column.
// FC16: physical col 31 = leftmost visual, col 0 = rightmost.
static void write_col(uint8_t col, uint8_t data) {
    mx.setColumn(31 - col, data);
}

// Write a 5-column digit at logical position col
static void write_digit(uint8_t col, uint8_t d) {
    write_col(col, FONT_5x7[d][0]);
    write_col(col + 1, FONT_5x7[d][1]);
    write_col(col + 2, FONT_5x7[d][2]);
    write_col(col + 3, FONT_5x7[d][3]);
    write_col(col + 4, FONT_5x7[d][4]);
}

// Write a 1-column colon at logical position col
static void write_colon(uint8_t col, bool on) {
    write_col(col, on ? COLON_DOTS : 0x00);
}

// ═══════════════════════════════════════════════════════════
// TASK 4 — clock_display  (core 1)
//
// HH:MM only — classic 5×7 digits across ALL 4 modules (32 cols):
//   col 0    : left pad
//   col 1-5  : H tens   (5px)  — blank if 12hr and < 10
//   col 6-7  : gap (2px)
//   col 8-12 : H units  (5px)
//   col 13-14: gap (2px)
//   col 15   : colon    (1px)
//   col 16-17: gap (2px)
//   col 18-22: M tens   (5px)
//   col 23-24: gap (2px)
//   col 25-29: M units  (5px)
//   col 30   : AM/PM dot (top = AM, bottom = PM)
//   col 31   : right pad
// Total: 1+5+2+5+2+1+2+5+2+5+1+1 = 32 ✓
// ═══════════════════════════════════════════════════════════
void clock_display_task(void* arg) {
    Serial.println("[clock] Waiting for NTP sync...");
    struct tm timeinfo;
    uint8_t attempts = 0;
    while (!getLocalTime(&timeinfo) && attempts < 60) {
        vTaskDelay(pdMS_TO_TICKS(500));
        attempts++;
    }
    if (attempts >= 60) {
        Serial.println("[clock] ⚠️  NTP sync failed — clock task exiting");
        vTaskDelete(NULL);
        return;
    }
    Serial.println("[clock] NTP synced ✓");

    bool colon_on = true;

    for (;;) {
        if (getLocalTime(&timeinfo)) {
            mx.control(MD_MAX72XX::UPDATE, MD_MAX72XX::OFF);

            // Clear all 32 columns
            for (int c = 0; c < 32; c++) mx.setColumn(c, 0x00);

            int hour = timeinfo.tm_hour;
            if (CLOCK_12HR) {
                hour = hour % 12;
                if (hour == 0) hour = 12;
            }

            // Hours
            uint8_t h_tens = hour / 10;
            write_digit(1, (CLOCK_12HR && h_tens == 0) ? 10 : h_tens);
            write_digit(8, hour % 10);

            // Colon
            write_colon(15, colon_on);

            // Minutes
            write_digit(18, timeinfo.tm_min / 10);
            write_digit(25, timeinfo.tm_min % 10);

            mx.control(MD_MAX72XX::UPDATE, MD_MAX72XX::ON);
            colon_on = !colon_on;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    vTaskDelete(NULL);
}

// ═══════════════════════════════════════════════════════════
// TASK 5 — text_rx  (core 1)
// Listens on TEXT_UDP_PORT for text/state strings from the Pi.
// State commands control LED state; plain text scrolls on OLED.
// ═══════════════════════════════════════════════════════════
void text_rx_task(void* arg) {
    char buf[512];

    for (;;) {
        int len = text_udp.parsePacket();
        if (len > 0) {
            int n = text_udp.read((uint8_t*)buf, sizeof(buf) - 1);
            if (n > 0) {
                buf[n] = '\0';
                String msg = String(buf);
                msg.trim();

                Serial.printf("[text] Received: %s\n", buf);

                if (msg == "[CLEAR]") {
                    // Spec: text-clear hint only — do NOT change AI state or
                    // face
                    Serial.println("[text] [CLEAR] — no state change");
                } else if (msg == "[STATE:LISTENING]") {
                    taskENTER_CRITICAL(&state_mux);
                    pi_is_listening = true;
                    pi_is_processing = false;
                    taskEXIT_CRITICAL(&state_mux);
                    mochi_set_state(AI_LISTENING);
                } else if (msg == "[STATE:PROCESSING]") {
                    taskENTER_CRITICAL(&state_mux);
                    pi_is_processing = true;
                    pi_is_listening = false;
                    taskEXIT_CRITICAL(&state_mux);
                    mochi_set_state(AI_PROCESSING);
                } else if (msg == "[STATE:SLEEPING]") {
                    taskENTER_CRITICAL(&state_mux);
                    pi_is_listening = false;
                    pi_is_processing = false;
                    taskEXIT_CRITICAL(&state_mux);
                    mochi_set_state(AI_SLEEPING);
                } else if (msg == "[STATE:SPEAKING]") {
                    taskENTER_CRITICAL(&state_mux);
                    pi_is_listening = false;
                    pi_is_processing = false;
                    taskEXIT_CRITICAL(&state_mux);
                    mochi_set_state(AI_SPEAKING);
                } else if (msg == "[CMD:LOCK:UNLOCK]") {
                    int cmd = 0;
                    xQueueSend(lock_cmd_queue, &cmd, 0);
                    Serial.println("[lock] Queued UNLOCK command");
                    mochi_show_reaction(FACE_WINK);
                } else if (msg == "[CMD:LOCK:LOCK]") {
                    int cmd = 1;
                    xQueueSend(lock_cmd_queue, &cmd, 0);
                    Serial.println("[lock] Queued LOCK command");
                    mochi_show_reaction(FACE_WINK);
                } else if (msg == "[CMD:LOCK:STATUS]") {
                    int cmd = 2;
                    xQueueSend(lock_cmd_queue, &cmd, 0);
                    Serial.println("[lock] Queued STATUS request");
                    mochi_show_reaction(FACE_LOVE);
                } else if (msg.startsWith("[CMD:LOCK:")) {
                    // Unknown lock sub-command — ignore silently
                    Serial.printf("[text] Ignored unhandled CMD: %s\n",
                                  msg.c_str());
                } else if (msg.startsWith("[CMD:LAMP:")) {
                    // ── LAMP COMMAND ROUTING ───────────────────────────
                    // Extract the part after "[CMD:LAMP:"
                    String sub = msg.substring(10);  // e.g. "ON]"
                    sub.replace("]", "");            // strip trailing ]
                    char lampToken[64];
                    sub.toCharArray(lampToken, sizeof(lampToken));

                    // Rewrite recognised prefixes into queue tokens
                    char qToken[64] = "";
                    if (sub == "ON") {
                        strncpy(qToken, "ON", sizeof(qToken));
                    } else if (sub == "OFF") {
                        strncpy(qToken, "OFF", sizeof(qToken));
                    } else if (sub.startsWith("BRIGHTNESS:")) {
                        snprintf(qToken, sizeof(qToken), "BRIGHT:%s",
                                 sub.substring(11).c_str());
                    } else if (sub.startsWith("COLOR:")) {
                        snprintf(qToken, sizeof(qToken), "COLOR:%s",
                                 sub.substring(6).c_str());
                    } else if (sub.startsWith("PATTERN:")) {
                        snprintf(qToken, sizeof(qToken), "PATTERN:%s",
                                 sub.substring(8).c_str());
                    } else if (sub.startsWith("SCENE:")) {
                        snprintf(qToken, sizeof(qToken), "SCENE:%s",
                                 sub.substring(6).c_str());
                    } else {
                        Serial.printf("[lamp] Unknown LAMP sub-cmd: %s\n",
                                      sub.c_str());
                    }
                    if (strlen(qToken) > 0) {
                        xQueueSend(lamp_cmd_queue, qToken, 0);
                        Serial.printf("[lamp] Queued: %s\n", qToken);
                    }
                    mochi_show_reaction(FACE_LOVE);
                } else if (msg.startsWith("[FACE:")) {
                    // ── FACE COMMAND ROUTING ──────────────────────────
                    // Extract name between "[FACE:" and "]"
                    String face = msg.substring(6);
                    face.replace("]", "");
                    face.trim();
                    Serial.printf("[face] Reaction request: %s\n",
                                  face.c_str());
                    if (face == "NORMAL") {
                        mochi_show_reaction(FACE_NORMAL, 2000);
                    } else if (face == "HAPPY") {
                        mochi_show_reaction(FACE_SMILE, 2000);
                    } else if (face == "LOVE") {
                        mochi_show_reaction(FACE_LOVE, 2000);
                    } else if (face == "WINK") {
                        mochi_show_reaction(FACE_WINK, 2000);
                    } else if (face == "UWU") {
                        mochi_show_reaction(FACE_UWU, 2000);
                    } else if (face == "BLINK") {
                        mochi_show_reaction(FACE_BLINK, 500);
                    } else if (face == "YAWN") {
                        mochi_show_reaction(FACE_YAWNING, 3000);
                    } else if (face == "LOOK_LEFT") {
                        mochi_show_reaction(FACE_DISTRACTED_LEFT, 2000);
                    } else if (face == "LOOK_RIGHT") {
                        mochi_show_reaction(FACE_CURIOUS_RIGHT, 2000);
                    } else if (face == "LAUGH") {
                        mochi_show_reaction(FACE_LAUGH, 3000);
                    } else if (face == "CRY") {
                        mochi_show_reaction(FACE_CRYING, 3000);
                    } else if (face == "SMIRK") {
                        mochi_show_reaction(FACE_SMIRK, 2000);
                    } else if (face == "BLISSFUL") {
                        mochi_show_reaction(FACE_BLISSFUL, 2000);
                    } else if (face == "SNEEZE") {
                        mochi_show_reaction(FACE_SNEEZE, 1500);
                    } else if (face == "TONGUE") {
                        mochi_show_reaction(FACE_TONGUE_OUT, 2000);
                    } else if (face == "DIZZY") {
                        mochi_show_reaction(FACE_DIZZY, 2000);
                    } else if (face == "HEAD_PAT") {
                        mochi_show_reaction(FACE_HEAD_PAT, 3000);
                    } else {
                        Serial.printf("[face] Unknown face tag: %s\n",
                                      face.c_str());
                    }
                } else if (msg.startsWith("[ANIM:")) {
                    // ── BITMAP ANIMATION ROUTING ─────────────────
                    String anim = msg.substring(6);
                    anim.replace("]", "");
                    anim.trim();
                    Serial.printf("[anim] Animation request: %s\n",
                                  anim.c_str());
                    if (anim == "IDLE")
                        mochi_start_animation(ANIM_IDLE);
                    else if (anim == "BLINK")
                        mochi_start_animation(ANIM_BLINK);
                    else if (anim == "HAPPY")
                        mochi_start_animation(ANIM_HAPPY);
                    else if (anim == "EXCITED")
                        mochi_start_animation(ANIM_EXCITED);
                    else if (anim == "LOVE")
                        mochi_start_animation(ANIM_LOVE);
                    else if (anim == "DUMB_LOVE")
                        mochi_start_animation(ANIM_DUMB_LOVE);
                    else if (anim == "UWU")
                        mochi_start_animation(ANIM_UWU);
                    else if (anim == "WINK")
                        mochi_start_animation(ANIM_WINK_ANIM);
                    else if (anim == "LAUGH")
                        mochi_start_animation(ANIM_LAUGH);
                    else if (anim == "AWKWARD_LAUGH")
                        mochi_start_animation(ANIM_AWKWARD_LAUGH);
                    else if (anim == "SLEEPY")
                        mochi_start_animation(ANIM_SLEEPY);
                    else if (anim == "CRYING")
                        mochi_start_animation(ANIM_CRYING);
                    else if (anim == "CRYING_SMILE")
                        mochi_start_animation(ANIM_CRYING_SMILE);
                    else if (anim == "SNEEZE")
                        mochi_start_animation(ANIM_SNEEZE);
                    else if (anim == "BIG_SNEEZE")
                        mochi_start_animation(ANIM_BIG_SNEEZE);
                    else if (anim == "SMIRK")
                        mochi_start_animation(ANIM_SMIRK_ANIM);
                    else if (anim == "ANGRY")
                        mochi_start_animation(ANIM_ANGRY);
                    else if (anim == "ROAD_RAGE")
                        mochi_start_animation(ANIM_ROAD_RAGE);
                    else if (anim == "DISTRACTED")
                        mochi_start_animation(ANIM_DISTRACTED);
                    else if (anim == "DISTRACTED_2")
                        mochi_start_animation(ANIM_DISTRACTED_2);
                    else if (anim == "LOOK_LEFT")
                        mochi_start_animation(ANIM_LOOK_LEFT);
                    else if (anim == "LOOK_RIGHT")
                        mochi_start_animation(ANIM_LOOK_RIGHT);
                    else if (anim == "STOP") {
                        taskENTER_CRITICAL(&face_mux);
                        mochi_anim_active = false;
                        mochi_current_face = (MochiFace)-1;
                        taskEXIT_CRITICAL(&face_mux);
                    } else {
                        Serial.printf("[anim] Unknown anim: %s\n",
                                      anim.c_str());
                    }
                } else {
                    // Plain text — face stays on current state
                    Serial.printf("[text] (ignored for face): %s\n",
                                  msg.c_str());
                }
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
    vTaskDelete(NULL);
}

// ─── SETUP ────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    Serial.println("\n[boot] Starting bidirectional audio streamer...");

    // ── NeoPixel ──
    leds.begin();
    leds.setBrightness(LED_BRIGHT);
    leds.clear();
    leds.show();
    show_boot();

    // ── WiFi ──
    led_state = LED_WIFI_CONNECT;
    WiFi.mode(WIFI_STA);
    WiFi.setHostname("Table Assistant");
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.print("[wifi] Connecting");
    while (WiFi.status() != WL_CONNECTED) {
        show_wifi_connecting();  // blue breathing while waiting
        delay(250);
        Serial.print('.');
    }
    Serial.printf("\n[wifi] Connected — IP: %s\n",
                  WiFi.localIP().toString().c_str());
    String mac = WiFi.macAddress();
    Serial.print("ESP32 MAC Address: ");
    Serial.println(mac);

    // ── NTP time sync ──
    configTime(GMT_OFFSET_S, DST_OFFSET_S, "pool.ntp.org", "time.google.com",
               "time.cloudflare.com");
    delay(500);  // give UDP stack time to send the first NTP request before
                 // tasks start
    Serial.println("[ntp]  Time sync started");

    // ── MAX7219 display init ──
    mx.begin();
    mx.control(MD_MAX72XX::INTENSITY, 5);  // brightness 0-15
    mx.clear();
    Serial.println("[disp] MAX7219 display ready");

    // ── I2S (TX + RX) ──
    setup_i2s();
    i2s_start(I2S_PORT);
    Serial.println("[i2s]  Mic + Speaker ready");
    speaker_test();  // 3 beeps — confirms I2S TX is wired correctly

    // ── UDP sockets ──
    mic_udp.begin(
        TX_UDP_PORT);  // bind local port (optional for TX but harmless)
    rx_udp.begin(RX_UDP_PORT);  // listen for incoming TTS audio
    Serial.printf("[udp]  TX → %s:%d  |  RX ← 0.0.0.0:%d\n", PI_IP, TX_UDP_PORT,
                  RX_UDP_PORT);

    // ── Playback queue: 24 slots × PLAYBACK_BUF × 2 bytes ──
    playback_queue = xQueueCreate(
        24,
        PLAYBACK_BUF *
            sizeof(int16_t));  // 24 × 32ms = ~768ms buffer absorbs WiFi jitter
    if (!playback_queue) {
        Serial.println("[ERR]  Failed to create playback queue!");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));  // halt
    }

    // ── Lock command queue + task ──
    lock_cmd_queue = xQueueCreate(4, sizeof(int));
    xTaskCreatePinnedToCore(lock_http_task, "lock_http", 8192, NULL, 1, NULL,
                            0);  // core 0

    // ── Lamp command queue + task ──
    lamp_cmd_queue = xQueueCreate(5, sizeof(char) * 64);
    xTaskCreatePinnedToCore(lamp_http_task, "lamp_http", 8192, NULL, 1, NULL,
                            0);  // core 0
    // Eager authentication so first lamp command has no auth delay
    http_lamp_authenticate();

    // ── Launch FreeRTOS tasks ──
    xTaskCreatePinnedToCore(mic_stream_task, "mic_stream", 8192, NULL, 1, NULL,
                            1);  // core 1

    xTaskCreatePinnedToCore(
        audio_rx_task, "audio_rx", 8192, NULL, 2, NULL,
        0);  // core 0, priority 2 (BUG 3 fix: 4096→8192 for PLAYBACK_BUF=1024)

    xTaskCreatePinnedToCore(audio_play_task, "audio_play", 8192, NULL, 3, NULL,
                            0);  // core 0, priority 3, stack 8192 (BUG 3 fix:
                                 // pcm[1024]+out[1024] need ~7KB)

    xTaskCreatePinnedToCore(clock_display_task, "clock_disp", 4096, NULL, 1,
                            NULL, 1);  // core 1

    // ── OLED init ──
    Wire.begin(21, 22);  // SDA=21, SCL=22
    if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
        Serial.println("[oled] SSD1306 init FAILED");
    } else {
        oled.clearDisplay();
        oled.display();
        Serial.println("[oled] SSD1306 display ready");
    }

    // ── Text UDP socket ──
    text_udp.begin(TEXT_UDP_PORT);
    Serial.printf("[udp]  Text/state RX ← 0.0.0.0:%d\n", TEXT_UDP_PORT);

    // ── Lock response UDP socket (TX only — bind ephemeral local port) ──
    lock_resp_udp.begin(0);
    Serial.printf("[udp]  Lock resp TX → %s:%d\n", PI_IP, LOCK_RESP_UDP_PORT);

    // ── Launch text receiver task ──
    xTaskCreatePinnedToCore(text_rx_task, "text_rx", 4096, NULL, 1, NULL,
                            1);  // core 1

    // ── OLED face update task — MUST be on core 1 (I2C / Wire) ──
    xTaskCreatePinnedToCore(face_update_task, "face_upd", 8192, NULL, 1, NULL,
                            1);  // core 1 (8KB: bitmap drawBitmap needs stack)

    Serial.println(
        "[boot] All tasks running — streaming mic & listening for TTS");

    // Default state: idle (LEDs off)
    led_state = LED_IDLE;
}

// ─── LOOP ─────────────────────────────────────────────────
// Runs on core 0.  Drives LED state machine + WiFi watchdog.
//
// LED colour key:
//   BLUE breathing    → WiFi connecting
//   RED breathing     → Recording / mic streaming (default)
//   YELLOW pulse      → Buffering (TTS packets arriving, queue filling)
//   GREEN + BLUE chase→ Playing TTS audio through speaker
//   GREEN flash ×3    → Playback finished
//   RED fast blink    → WiFi lost

// Buffering → Playing threshold: start playback animation once
// the queue has at least this many packets queued up.
#define BUFFER_THRESHOLD 3

void loop() {
    vTaskDelay(pdMS_TO_TICKS(50));
    // Note: update_mochi_face() moved to face_update_task (core 1).
    // loop() handles LED state machine only — no I2C, no blocking calls.

    // ── Determine LED state from system status ──
    UBaseType_t queued = uxQueueMessagesWaiting(playback_queue);

    // Snapshot cross-core flags inside their spinlocks (read once, use below).
    // volatile alone is NOT sufficient on dual-core Xtensa LX6.
    taskENTER_CRITICAL(&tts_mux);
    bool ended = playback_just_ended;
    taskEXIT_CRITICAL(&tts_mux);

    taskENTER_CRITICAL(&state_mux);
    bool is_listening = pi_is_listening;
    bool is_processing = pi_is_processing;
    taskEXIT_CRITICAL(&state_mux);

    if (WiFi.status() != WL_CONNECTED) {
        // WiFi gone — override everything
        led_state = LED_WIFI_LOST;
    } else if (ended) {
        led_state = LED_DONE;
    } else if (queued >= BUFFER_THRESHOLD) {
        // Enough data queued — actively playing
        led_state = LED_PLAYING;
    } else if (queued > 0) {
        // Packets arriving but still filling — buffering
        led_state = LED_BUFFERING;
    } else if (is_processing) {
        led_state = LED_PROCESSING;
    } else if (is_listening) {
        led_state = LED_LISTENING;
    } else if (led_state != LED_WIFI_CONNECT) {
        // No playback, WiFi OK — idle (LEDs off)
        led_state = LED_IDLE;
    }

    // ── Render current LED state ──
    switch (led_state) {
        case LED_WIFI_CONNECT:
            show_wifi_connecting();
            break;
        case LED_IDLE:
            show_idle();
            break;
        case LED_RECORDING:
            show_recording();
            break;
        case LED_LISTENING:
            show_listening();
            break;
        case LED_PROCESSING:
            show_processing();
            break;
        case LED_BUFFERING:
            show_buffering(queued);
            break;
        case LED_PLAYING:
            show_playing(queued);
            break;
        case LED_DONE: {
            // Non-blocking 3-flash state machine (avoids blocking loop()
            // for 750 ms which would starve audio tasks on core 0)
            static int done_phase = 0;
            static uint32_t done_timer = 0;
            if (millis() - done_timer > 150) {
                done_timer = millis();
                if (done_phase % 2 == 0) {
                    set_segment(RING_START, RING_END, leds.Color(0, 200, 0));
                    set_rectangle(leds.Color(0, 60, 0));
                    leds.show();
                } else {
                    clear_all();
                }
                done_phase++;
                if (done_phase >= 6) {  // 3 on + 3 off = 6 transitions
                    done_phase = 0;
                    done_timer = 0;  // BUG 4 fix: reset so next LED_DONE entry
                                     // starts clean
                    taskENTER_CRITICAL(&tts_mux);
                    playback_just_ended = false;
                    taskEXIT_CRITICAL(&tts_mux);
                    led_state = LED_IDLE;
                }
            }
            break;
        }
        case LED_WIFI_LOST:
            show_wifi_lost();
            break;
        default:
            break;
    }

    // ── WiFi watchdog (check every ~1 s = 20 × 50 ms loops) ──
    static uint8_t wifi_check_counter = 0;
    if (++wifi_check_counter >= 20) {
        wifi_check_counter = 0;

        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("[wifi] Connection lost — reconnecting...");
            led_state = LED_WIFI_CONNECT;

            WiFi.disconnect(true);
            WiFi.begin(WIFI_SSID, WIFI_PASS);

            uint8_t attempts = 0;
            while (WiFi.status() != WL_CONNECTED && attempts < 40) {
                show_wifi_connecting();  // keep animating while reconnecting
                vTaskDelay(pdMS_TO_TICKS(250));
                Serial.print('.');
                attempts++;
            }

            if (WiFi.status() == WL_CONNECTED) {
                Serial.printf("\n[wifi] Reconnected — IP: %s\n",
                              WiFi.localIP().toString().c_str());
                mic_udp.stop();
                rx_udp.stop();
                mic_udp.begin(TX_UDP_PORT);
                rx_udp.begin(RX_UDP_PORT);
                led_state = LED_IDLE;
                text_udp.stop();
                text_udp.begin(TEXT_UDP_PORT);
                lock_resp_udp.stop();
                lock_resp_udp.begin(0);
            } else {
                Serial.println("\n[wifi] Reconnect failed — will retry");
                led_state = LED_WIFI_LOST;
            }
        }
    }
}
