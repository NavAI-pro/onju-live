// Waveshare ESP32-S3-Touch-LCD-1.85 (audio variant) — onju voice front-end.
//
// Streams mic audio (mu-law over UDP) to the onju server and plays the server's
// Opus reply (over TCP) on the speaker. VOX / always-on mic; the server runs
// VAD + wake word. This is the full pipeline port of the m5_echo firmware, with
// the audio layer replaced by this board's ES7210 mic + ES8311 speaker on a
// shared full-duplex I2S bus (see BRINGUP_NOTES.md).
//
// Audio pins:  MCLK=2, BCK=48, WS=38, DOUT=47(->ES8311), DIN=39(<-ES7210).
// I2C:         SDA=11, SCL=10.  MCLK = 256 * sample_rate.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WiFiManager.h>          // portable WiFi: captive-portal provisioning
#include <esp_mac.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <Wire.h>
#include <opus.h>
#include "ESP_I2S.h"
#include "es7210.h"
#include "es8311.h"
#include "credentials.h"
#include "audio_compression.h"
#include <HTTPClient.h>          // standalone (Option 2): device -> cloud directly
#include <WiFiClientSecure.h>
#include <WebSocketsClient.h>    // STT over WSS (REST STT is broken server-side)
#include <ArduinoJson.h>
#include "secrets.h"
#define STANDALONE 1             // 1 = on-device pipeline (no laptop); 0 = server mode
#include "driver/spi_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_heap_caps.h"
#include "freertos/semphr.h"
#include "st77916_init_cmds.h"   // factory ST77916 init table (Waveshare vendor_specific_init_new)
#include "font_mono.h"           // Menlo 14x29 1bpp ASCII font

#if __has_include("git_hash.h")
#include "git_hash.h"
#else
#define GIT_HASH "------"
#endif

// ─── Audio pins / format ────────────────────────────────────
#define I2S_MCK_PIN  GPIO_NUM_2
#define I2S_BCK_PIN  GPIO_NUM_48
#define I2S_WS_PIN   GPIO_NUM_38
#define I2S_DOUT_PIN GPIO_NUM_47
#define I2S_DIN_PIN  GPIO_NUM_39
#define I2C_SDA_PIN  GPIO_NUM_11
#define I2C_SCL_PIN  GPIO_NUM_10

#define SAMPLE_RATE   16000
#define MCLK_MULT     256
#define MCLK_FREQ     (SAMPLE_RATE * MCLK_MULT)
#define SAMPLE_CHUNK  512               // 32ms @16k — matches server VAD frame
#define MIC_GAIN      1                 // mic is HOT (speech ~near full-scale already); no boost

#define HOST_NAME     "ws-audio"
#define DEFAULT_SERVER "default.server.com"
#define DEFAULT_VOLUME 65               // ES8311 volume 0..100
#define MIN_PLAY_VOLUME 50              // never play quieter than this

// ─── Opus ───────────────────────────────────────────────────
#define OPUS_FRAME_SIZE 320             // 20ms @16k
#define OPUS_MAX_PACKET 4000

// ─── Globals ────────────────────────────────────────────────
I2SClass i2s;
static es7210_dev_handle_t es7210_handle = NULL;
static es8311_handle_t es8311_handle = NULL;

Preferences preferences;
WiFiUDP udp;
WiFiServer tcpServer(3001);
WiFiClient audioClient;

String wifi_ssid, wifi_password, server_hostname;
char desired_hostname[32];
IPAddress serverIP(0, 0, 0, 0);
int speaker_volume = DEFAULT_VOLUME;

OpusDecoder *opus_decoder = NULL;
int16_t opus_pcm_buffer[OPUS_FRAME_SIZE];
uint8_t opus_packet_buffer[OPUS_MAX_PACKET];


int16_t micStereo[SAMPLE_CHUNK * 2];    // interleaved L/R from I2S
int16_t micMono[SAMPLE_CHUNK];          // one channel, processed
uint8_t micUlaw[SAMPLE_CHUNK];          // mu-law for UDP
volatile bool isPlaying = false;        // pause mic upstream during playback

// ─── LCD (ST77916 QSPI, 360x360 round) ──────────────────────
// Pins from Waveshare 1.85C repo example 01_lvgl_example. All distinct from the
// audio pins; I2C (SDA11/SCL10) is shared with the codecs + this expander.
#define LCD_CS_PIN   21
#define LCD_SCK_PIN  40
#define LCD_D0_PIN   46
#define LCD_D1_PIN   45
#define LCD_D2_PIN   42
#define LCD_D3_PIN   41
#define LCD_BL_PIN   5                  // backlight, active high
#define LCD_W        360
#define LCD_H        360
#define LCD_CX       180
#define LCD_CY       180                // ring is centered on the round screen

// TCA9554 I/O expander (0x20): LCD reset = EXIO2 (bit 1), touch reset = EXIO1.
#define TCA_ADDR         0x20
#define TCA_OUTPUT_REG   0x01
#define TCA_CONFIG_REG   0x03
#define TCA_LCD_RST_BIT  1              // EXIO2

// QSPI framing opcodes: commands prefixed with 0x02, pixel writes with 0x32
// (this is exactly what the factory esp_lcd driver does; Arduino_GFX's bus got
// the quad framing subtly wrong and striped green/cyan).
#define LCD_OP_CMD    0x02
#define LCD_OP_COLOR  0x32

esp_lcd_panel_io_handle_t lcd_io = NULL;
uint16_t *fb = NULL;                    // 360x360 RGB565 framebuffer (PSRAM), big-endian
bool lcdReady = false;

// The framebuffer is in PSRAM, which SPI DMA can't send directly, so each blit
// copies a horizontal band into this small INTERNAL DMA buffer and waits for it.
#define BAND_ROWS 40                    // divides 360
uint16_t *dmabuf = NULL;                // BAND_ROWS x 360 internal DMA scratch
SemaphoreHandle_t lcdDone = NULL;       // given when a band's color DMA completes
static bool IRAM_ATTR lcdTransDone(esp_lcd_panel_io_handle_t io,
                                   esp_lcd_panel_io_event_data_t *ed, void *ctx) {
  BaseType_t hpw = pdFALSE;
  xSemaphoreGiveFromISR(lcdDone, &hpw);
  return hpw == pdTRUE;
}

enum UiState { UI_CONNECTING, UI_LISTENING, UI_THINKING, UI_SPEAKING };
volatile int gAudioLevel = 0;          // 0..100, playback amplitude (drives the orb)
static int  drawnState = -1;

// Captions: when active, the orb recedes to an ambient glow and text takes over.
#define LCD_DEMO 0                     // demo cycle off — orb tracks real state
char capUser[96]  = "";                // "Siz: ..." (what you said)
char capReply[192] = "";               // the assistant's reply
bool capActive = false;

// ─── TCA9554 minimal (only what we need to reset the LCD) ────
void tcaWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(TCA_ADDR); Wire.write(reg); Wire.write(val); Wire.endTransmission();
}
uint8_t tcaRead(uint8_t reg) {
  Wire.beginTransmission(TCA_ADDR); Wire.write(reg); Wire.endTransmission();
  Wire.requestFrom((int)TCA_ADDR, 1);
  return Wire.available() ? Wire.read() : 0xFF;
}
void tcaSetBit(uint8_t bit, bool high) {
  uint8_t v = tcaRead(TCA_OUTPUT_REG);
  if (high) v |= (1 << bit); else v &= ~(1 << bit);
  tcaWrite(TCA_OUTPUT_REG, v);
}

// ─── Framebuffer drawing (pixels stored big-endian for the panel) ─
static inline uint16_t be16(uint16_t c) { return (uint16_t)((c >> 8) | (c << 8)); }
static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}
void fbFill(uint16_t c) {
  uint16_t v = be16(c);
  for (int i = 0; i < LCD_W * LCD_H; i++) fb[i] = v;
}
void fbCircle(int cx, int cy, int r, uint16_t c) {
  uint16_t v = be16(c);
  int r2 = r * r;
  int y0 = (cy - r < 0) ? 0 : cy - r, y1 = (cy + r >= LCD_H) ? LCD_H - 1 : cy + r;
  for (int y = y0; y <= y1; y++) {
    int dy = y - cy, dx2 = r2 - dy * dy;
    if (dx2 < 0) continue;
    int dx = (int)sqrtf((float)dx2);
    int x0 = (cx - dx < 0) ? 0 : cx - dx, x1 = (cx + dx >= LCD_W) ? LCD_W - 1 : cx + dx;
    uint16_t *row = fb + y * LCD_W;
    for (int x = x0; x <= x1; x++) row[x] = v;
  }
}

// ─── Text (1bpp font blitted into the framebuffer) ─────────
void fbGlyph(int x, int y, char ch, uint16_t color) {
  uint8_t c = (uint8_t)ch;
  if (c < FONT_FIRST || c > FONT_LAST) c = ' ';
  const uint8_t *g = FONT[c - FONT_FIRST];
  uint16_t v = be16(color);
  for (int row = 0; row < FONT_H; row++) {
    int py = y + row;
    if (py < 0 || py >= LCD_H) continue;
    uint16_t *dst = fb + py * LCD_W;
    for (int col = 0; col < FONT_W; col++) {
      if (g[row * FONT_BPR + (col >> 3)] & (0x80 >> (col & 7))) {
        int px = x + col;
        if (px >= 0 && px < LCD_W) dst[px] = v;
      }
    }
  }
}
void fbText(int x, int y, const char *s, uint16_t color) {
  for (; *s; s++) { fbGlyph(x, y, *s, color); x += FONT_W; }
}
// Word-wrap `s` to maxW px, each line centred on cx, from y downward.
// Returns the y below the last line.
int fbTextWrap(const char *s, int cx, int y, int maxW, uint16_t color) {
  int maxChars = maxW / FONT_W;
  if (maxChars < 1) maxChars = 1;
  if (maxChars > 46) maxChars = 46;
  char line[48];
  while (*s) {
    while (*s == ' ') s++;
    if (!*s) break;
    int len = 0, brk = 0;
    while (s[len] && len < maxChars) { if (s[len] == ' ') brk = len; len++; }
    if (s[len] == 0) brk = len;          // remainder fits
    else if (brk == 0) brk = len;        // single long word -> hard break
    memcpy(line, s, brk); line[brk] = 0;
    fbText(cx - (brk * FONT_W) / 2, y, line, color);
    y += FONT_H + 5;
    s += brk;
  }
  return y;
}

// ─── esp_lcd command / pixel helpers (QSPI 0x02 / 0x32 framing) ─
static inline void lcdCmd(uint8_t cmd, const uint8_t *data, size_t len) {
  esp_lcd_panel_io_tx_param(lcd_io, ((uint32_t)LCD_OP_CMD << 24) | ((uint32_t)cmd << 8), data, len);
}
void lcdBlit() {
  uint8_t ca[4] = {0x00, 0x00, (uint8_t)((LCD_W - 1) >> 8), (uint8_t)((LCD_W - 1) & 0xFF)};
  lcdCmd(0x2A, ca, 4);   // CASET 0..359 (full width, once)
  for (int y = 0; y < LCD_H; y += BAND_ROWS) {
    int rows = (y + BAND_ROWS <= LCD_H) ? BAND_ROWS : (LCD_H - y);
    uint8_t ra[4] = {(uint8_t)(y >> 8), (uint8_t)(y & 0xFF),
                     (uint8_t)((y + rows - 1) >> 8), (uint8_t)((y + rows - 1) & 0xFF)};
    lcdCmd(0x2B, ra, 4);   // RASET for this band
    memcpy(dmabuf, fb + (size_t)y * LCD_W, (size_t)rows * LCD_W * 2);
    esp_lcd_panel_io_tx_color(lcd_io, ((uint32_t)LCD_OP_COLOR << 24) | ((uint32_t)0x2C << 8),
                              dmabuf, (size_t)rows * LCD_W * 2);
    xSemaphoreTake(lcdDone, pdMS_TO_TICKS(200));   // wait for this band's DMA before reusing dmabuf
  }
}

// ─── "Living orb" UI — a soft radial glow that breathes, colour = state ────
// Rendered per-pixel from two precomputed radial falloff LUTs (a wide colour
// halo + a tight white-hot core), composited over a deep-dark background.
// Smooth, glowy, premium — the Siri/Halo look.
static uint8_t haloLUT[256], coreLUT[256];
static bool lutReady = false;
void buildLUT() {
  for (int i = 0; i < 256; i++) {
    float d  = sqrtf((float)(i << 8));          // distance from centre (px)
    float th = d / 150.0f, tc = d / 58.0f;
    haloLUT[i] = (uint8_t)(expf(-2.2f * th * th) * 255.0f);
    coreLUT[i] = (uint8_t)(expf(-3.0f * tc * tc) * 255.0f);
  }
  lutReady = true;
}
void drawUi(int s, uint32_t nowMs) {
  if (!lcdReady) return;
  if (!lutReady) buildLUT();
  uint8_t cr, cg, cb;
  switch (s) {
    case UI_SPEAKING:  cr = 60;  cg = 235; cb = 140; break;   // green
    case UI_LISTENING: cr = 55;  cg = 180; cb = 255; break;   // cyan-blue
    case UI_THINKING:  cr = 150; cg = 90;  cb = 255; break;   // violet
    default:           cr = 110; cg = 125; cb = 150; break;   // slate (connecting)
  }
  float breath = 0.5f + 0.5f * sinf((nowMs % 2600) / 2600.0f * 2.0f * PI);
  int hpulse, cpulse;
  if (capActive) {                               // recede to ambient glow behind text
    hpulse = 70 + (int)(breath * 28);
    cpulse = 22;
  } else if (s == UI_SPEAKING) {                 // bloom with the voice
    hpulse = 150 + gAudioLevel * 105 / 100;
    cpulse = 120 + gAudioLevel * 135 / 100;
  } else {                                       // gentle breathing
    hpulse = 150 + (int)(breath * 90);
    cpulse = 130 + (int)(breath * 100);
  }
  if (hpulse > 255) hpulse = 255;
  if (cpulse > 255) cpulse = 255;
  const int bgR = 4, bgG = 7, bgB = 14;          // deep dark background
  int cometX = -1000, cometY = -1000;
  if (s == UI_THINKING) {                        // a bright comet orbits the halo
    float a = (nowMs % 1100) / 1100.0f * 2.0f * PI;
    cometX = LCD_CX + (int)(118.0f * cosf(a));
    cometY = LCD_CY + (int)(118.0f * sinf(a));
  }
  for (int y = 0; y < LCD_H; y++) {
    int dy = y - LCD_CY;
    uint16_t *dst = fb + y * LCD_W;
    for (int x = 0; x < LCD_W; x++) {
      int dx = x - LCD_CX;
      int idx = (dx * dx + dy * dy) >> 8;
      if (idx > 255) idx = 255;
      int halo = (haloLUT[idx] * hpulse) >> 8;
      int core = (coreLUT[idx] * cpulse) >> 8;
      int r = bgR + (((cr - bgR) * halo) >> 8);  // blend bg -> colour (halo)
      int g = bgG + (((cg - bgG) * halo) >> 8);
      int b = bgB + (((cb - bgB) * halo) >> 8);
      r += ((255 - r) * core) >> 8;              // then -> white (core)
      g += ((255 - g) * core) >> 8;
      b += ((255 - b) * core) >> 8;
      if (cometX > -1000) {
        int cdx = x - cometX, cdy = y - cometY, cd2 = cdx * cdx + cdy * cdy;
        if (cd2 < 520) {
          int ci = (520 - cd2) * 255 / 520;
          r += ((255 - r) * ci) >> 8; g += ((255 - g) * ci) >> 8; b += ((255 - b) * ci) >> 8;
        }
      }
      dst[x] = be16(rgb565(r, g, b));
    }
  }
  // caption overlay (your words + the reply), front and centre
  if (capActive) {
    int ty = 96;
    if (capUser[0])  { ty = fbTextWrap(capUser,  LCD_CX, ty, 280, rgb565(120, 150, 185)); ty += 10; }
    if (capReply[0]) {       fbTextWrap(capReply, LCD_CX, ty, 285, rgb565(238, 246, 255)); }
  }
  drawnState = s;
  lcdBlit();
}

// Solid-fill check via the esp_lcd path — GREEN especially must be clean now
// (it striped under Arduino_GFX). Loops a few times so it can be photographed.
void lcdSelfTest() {
  const uint16_t cols[] = { rgb565(255, 0, 0), rgb565(0, 255, 0), rgb565(0, 0, 255), rgb565(255, 255, 255) };
  const char *nm[] = { "RED", "GREEN", "BLUE", "WHITE" };
  for (int pass = 0; pass < 3; pass++) {
    for (int k = 0; k < 4; k++) {
      Serial.printf("SELFTEST: full screen %s\n", nm[k]);
      fbFill(cols[k]); lcdBlit(); delay(1600);
    }
  }
}

// Shown while the WiFi captive portal is open — tells the user which AP to join
// from their phone. Called by WiFiManager's AP callback.
void drawWifiSetup(const String &ap) {
  if (!lcdReady) return;
  fbFill(rgb565(6, 9, 18));
  fbTextWrap("WiFi sozlash",                 LCD_CX, 108, 280, rgb565(60, 180, 255));
  fbTextWrap("Telefonni shu WiFi'ga ulang:", LCD_CX, 150, 280, rgb565(190, 200, 215));
  fbTextWrap(ap.c_str(),                     LCD_CX, 192, 300, rgb565(255, 255, 255));
  fbTextWrap("keyin sahifa ochiladi",        LCD_CX, 238, 280, rgb565(140, 145, 160));
  lcdBlit();
}

void initDisplay() {
  // LCD reset lives on TCA9554 EXIO2. Make ONLY that bit an output so we don't
  // disturb the touch/other expander lines or the already-working audio.
  uint8_t cfg = tcaRead(TCA_CONFIG_REG);          // power-on default 0xFF (all inputs)
  cfg &= ~(1 << TCA_LCD_RST_BIT);
  tcaWrite(TCA_CONFIG_REG, cfg);
  tcaSetBit(TCA_LCD_RST_BIT, true);  delay(20);
  tcaSetBit(TCA_LCD_RST_BIT, false); delay(20);   // pulse reset low
  tcaSetBit(TCA_LCD_RST_BIT, true);  delay(150);

  pinMode(LCD_BL_PIN, OUTPUT); digitalWrite(LCD_BL_PIN, HIGH);   // backlight on

  // QSPI bus (4 data lines) + esp_lcd panel IO — the correct quad framing that
  // Arduino_GFX's bus got wrong (it striped green/cyan even at 2MHz).
  spi_bus_config_t bus = {};
  bus.sclk_io_num  = LCD_SCK_PIN;
  bus.data0_io_num = LCD_D0_PIN;
  bus.data1_io_num = LCD_D1_PIN;
  bus.data2_io_num = LCD_D2_PIN;
  bus.data3_io_num = LCD_D3_PIN;
  bus.max_transfer_sz = LCD_W * LCD_H * 2 + 16;
  if (spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO) != ESP_OK) { Serial.println("LCD spi_bus FAIL"); return; }

  esp_lcd_panel_io_spi_config_t io = {};
  io.cs_gpio_num       = LCD_CS_PIN;
  io.dc_gpio_num       = -1;
  io.spi_mode          = 0;
  io.pclk_hz           = 40 * 1000 * 1000;
  io.trans_queue_depth = 10;
  io.lcd_cmd_bits      = 32;
  io.lcd_param_bits    = 8;
  io.flags.quad_mode   = 1;
  io.on_color_trans_done = lcdTransDone;
  lcdDone = xSemaphoreCreateBinary();
  if (esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io, &lcd_io) != ESP_OK) { Serial.println("LCD panel_io FAIL"); return; }

  // Panel bring-up: SWRESET, then MADCTL/COLMOD, then the factory init table.
  lcdCmd(0x01, NULL, 0); delay(120);            // SWRESET
  uint8_t madctl = 0x00, colmod = 0x55;         // RGB order, RGB565
  lcdCmd(0x36, &madctl, 1);
  lcdCmd(0x3A, &colmod, 1);
  for (size_t i = 0; i < sizeof(ST77916_INIT) / sizeof(ST77916_INIT[0]); i++) {
    lcdCmd(ST77916_INIT[i].cmd, ST77916_INIT[i].data, ST77916_INIT[i].len);
    if (ST77916_INIT[i].delay_ms) delay(ST77916_INIT[i].delay_ms);
  }

  fb = (uint16_t *)heap_caps_malloc((size_t)LCD_W * LCD_H * 2, MALLOC_CAP_SPIRAM);
  if (!fb) { Serial.println("LCD fb alloc FAIL"); return; }
  dmabuf = (uint16_t *)heap_caps_malloc((size_t)BAND_ROWS * LCD_W * 2, MALLOC_CAP_DMA);
  if (!dmabuf) { Serial.println("LCD dmabuf alloc FAIL"); return; }
  lcdReady = true;
  Serial.println("LCD ST77916 (esp_lcd) init OK");
  drawUi(UI_CONNECTING, millis());
}

// ─── Codec init ─────────────────────────────────────────────
void es7210_mic_init() {
  es7210_i2c_config_t ic = {};
  ic.i2c_port = I2C_NUM_0;
  ic.i2c_addr = ES7210_ADDRRES_00;      // 0x40
  if (es7210_new_codec(&ic, &es7210_handle) != ESP_OK) { Serial.println("ES7210 create FAIL"); return; }
  es7210_codec_config_t cc = {};
  cc.sample_rate_hz = SAMPLE_RATE;
  cc.mclk_ratio = MCLK_MULT;
  cc.i2s_format = ES7210_I2S_FMT_I2S;
  cc.bit_width = ES7210_I2S_BITS_16B;
  cc.mic_bias = ES7210_MIC_BIAS_2V87;
  cc.mic_gain = ES7210_MIC_GAIN_36DB;
  esp_err_t r = es7210_config_codec(es7210_handle, &cc);
  es7210_config_volume(es7210_handle, 90);
  Serial.printf("ES7210 mic init: %s\n", r == ESP_OK ? "OK" : "FAIL");
}

void es8311_spk_init() {
  es8311_handle = es8311_create(I2C_NUM_0, ES8311_ADDRRES_0);   // 0x18
  if (!es8311_handle) { Serial.println("ES8311 create FAIL"); return; }
  es8311_clock_config_t clk = {};
  clk.mclk_inverted = false;
  clk.sclk_inverted = false;
  clk.mclk_from_mclk_pin = true;
  clk.mclk_frequency = MCLK_FREQ;
  clk.sample_frequency = SAMPLE_RATE;
  esp_err_t r = es8311_init(es8311_handle, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16);
  es8311_voice_volume_set(es8311_handle, speaker_volume, NULL);
  es8311_microphone_config(es8311_handle, false);
  Serial.printf("ES8311 speaker init: %s\n", r == ESP_OK ? "OK" : "FAIL");
}

void initAudio() {
  pinMode(15, OUTPUT); digitalWrite(15, HIGH);   // GPIO15 = power-amp ENABLE
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);
  es8311_spk_init();
  es7210_mic_init();
  i2s.setPins(I2S_BCK_PIN, I2S_WS_PIN, I2S_DOUT_PIN, I2S_DIN_PIN, I2S_MCK_PIN);
  i2s.setTimeout(1000);
  bool ok = i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
  Serial.printf("I2S begin: %s\n", ok ? "OK" : "FAIL");
}

// ─── Config (NVS) ───────────────────────────────────────────
void loadConfig() {
  preferences.begin("ws-cfg", true);
  wifi_ssid = preferences.getString("wifi_ssid", WIFI_SSID);
  wifi_password = preferences.getString("wifi_pass", WIFI_PASSWORD);
  server_hostname = preferences.getString("server", DEFAULT_SERVER);
  speaker_volume = preferences.getUChar("volume", DEFAULT_VOLUME);
  uint32_t savedIP = preferences.getUInt("server_ip", 0);
  preferences.end();
  if (savedIP != 0) serverIP = IPAddress(savedIP);
  Serial.printf("SSID:%s Server:%s Vol:%d SavedIP:%s\n", wifi_ssid.c_str(),
                server_hostname.c_str(), speaker_volume, serverIP.toString().c_str());
}

void saveConfig() {
  preferences.begin("ws-cfg", false);
  preferences.putString("wifi_ssid", wifi_ssid);
  preferences.putString("wifi_pass", wifi_password);
  preferences.putString("server", server_hostname);
  preferences.putUChar("volume", speaker_volume);
  preferences.end();
}

void enterConfigMode() {
  Serial.println("Config mode. Commands: ssid, pass, server, volume, exit, cancel");
  while (true) {
    if (!Serial.available()) continue;
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "exit")            { saveConfig(); Serial.println("Saved. Restarting..."); delay(500); ESP.restart(); }
    else if (cmd == "cancel")     { Serial.println("Cancelled. Restarting..."); delay(500); ESP.restart(); }
    else if (cmd.startsWith("ssid "))   { wifi_ssid = cmd.substring(5); Serial.println("SSID: " + wifi_ssid); }
    else if (cmd.startsWith("pass "))   { wifi_password = cmd.substring(5); Serial.println("Password updated"); }
    else if (cmd.startsWith("server ")) { server_hostname = cmd.substring(7); Serial.println("Server: " + server_hostname); }
    else if (cmd.startsWith("volume ")) { speaker_volume = constrain(cmd.substring(7).toInt(), 0, 100); Serial.printf("Volume: %d\n", speaker_volume); }
    else Serial.println("Unknown command");
  }
}

// ─── Mic task: read I2S -> mu-law -> UDP (own task so loop() is free for TCP) ─
#if STANDALONE
// ─── Standalone on-device energy VAD ────────────────────────
#define UTT_MAX_SAMP  (8 * SAMPLE_RATE)   // 8s max utterance
#define VAD_THRESH    3500                // frame peak for speech (noise floor ~300)
#define VAD_SIL_FRAMES 17                 // ~0.55s silence ends the utterance (32ms/frame)
#define VAD_MIN_SAMP  (SAMPLE_RATE / 3)   // ignore utterances shorter than ~0.33s
#define PRE_SAMP      (SAMPLE_RATE / 5)   // 200ms pre-roll so word onsets aren't clipped
int16_t *uttBuf = nullptr;                // PSRAM utterance buffer (append-only during an utterance)
volatile size_t uttLen = 0;               // samples captured so far (grows during speech)
volatile bool  vadRec = false;            // speech in progress (onset..end); watched by sttTask
volatile bool  sttReady = false;          // sttTask produced a transcript for the pipeline
volatile bool  gProcessing = false;       // pipeline running (LLM+TTS) — pause capture
String sttTranscript;                     // result handed from sttTask -> loop()
static int  vadSil = 0;
static int16_t preBuf[PRE_SAMP];          // ring of most-recent pre-speech audio
static size_t preHead = 0, preLen = 0;
// Streaming STT: energyVAD only runs the VAD + fills uttBuf. sttTask (core 0) opens the
// STT WebSocket at onset (vadRec) and uploads uttBuf AS it fills, so the transcript is
// ready almost as soon as the user stops — the handshake+upload overlap the speech.
void energyVAD(int16_t *pcm, int n, int framePeak) {
  if (uttBuf == nullptr || sttReady || gProcessing) return;   // busy: transcript pending / processing
  bool speech = framePeak > VAD_THRESH;
  if (speech || vadRec) {
    if (!vadRec && speech) {                        // onset: reset, prepend pre-roll, arm sttTask
      uttLen = 0;
      for (size_t i = 0; i < preLen && uttLen < (size_t)UTT_MAX_SAMP; i++)
        uttBuf[uttLen++] = preBuf[(preHead + PRE_SAMP - preLen + i) % PRE_SAMP];
      vadSil = 0;
      vadRec = true;                                // <- sttTask begins connecting immediately
    }
    if (uttLen + n <= UTT_MAX_SAMP) { memcpy(uttBuf + uttLen, pcm, (size_t)n * 2); uttLen += n; }
    if (speech) vadSil = 0;
    else if (++vadSil > VAD_SIL_FRAMES) vadRec = false;   // end -> sttTask sends "end", fetches final
  } else {                                          // idle: keep the pre-roll ring fresh
    for (int i = 0; i < n; i++) {
      preBuf[preHead] = pcm[i];
      preHead = (preHead + 1) % PRE_SAMP;
      if (preLen < PRE_SAMP) preLen++;
    }
  }
}
#endif

void micTask(void *param) {
  static float dc = 0.0f;
  uint32_t lastLog = 0;
  int32_t rawPeak = 0, sentPeak = 0;
  while (1) {
#if STANDALONE
    if (isPlaying || gProcessing) { vTaskDelay(pdMS_TO_TICKS(8)); continue; }
#else
    if (isPlaying || serverIP == IPAddress(0, 0, 0, 0)) { vTaskDelay(pdMS_TO_TICKS(8)); continue; }
#endif
    size_t br = i2s.readBytes((char *)micStereo, sizeof(micStereo));
    int frames = br / (sizeof(int16_t) * 2);
    int framePeak = 0;
    for (int i = 0; i < frames; i++) {
      int32_t l = micStereo[i * 2];
      dc += 0.01f * (l - dc);                       // faster DC tracker
      int32_t ac = l - (int32_t)dc;
      if (abs(ac) > rawPeak) rawPeak = abs(ac);
      if (abs(ac) > framePeak) framePeak = abs(ac);
      int32_t s = ac * MIC_GAIN;
      if (s > 32767) s = 32767; if (s < -32768) s = -32768;
      if (abs(s) > sentPeak) sentPeak = abs(s);
      micMono[i] = (int16_t)s;
    }
#if STANDALONE
    energyVAD(micMono, frames, framePeak);
#else
    encode_ulaw(micMono, micUlaw, frames);
    udp.beginPacket(serverIP, 3000);
    udp.write(micUlaw, frames);
    udp.endPacket();
#endif
    if (millis() - lastLog > 500) {
      lastLog = millis();
      Serial.printf("MIC rawAC=%d sent=%d rec=%d len=%u\n", (int)rawPeak, (int)sentPeak,
                    (int)
#if STANDALONE
                    vadRec, (unsigned)uttLen
#else
                    0, 0u
#endif
                    );
      rawPeak = 0; sentPeak = 0;
    }
  }
}

// ─── Opus playback (server -> speaker) ──────────────────────
void playOpusStream(WiFiClient &client) {
  int frameCount = 0;
  Serial.println("Playback start");
  isPlaying = true;
  // NOTE: no drawUi() here — a full-screen orb render + blit (~1-2s) before the
  // first audio frame delayed playback. Audio starts immediately now.

  while (client.connected() || client.available()) {
    if (client.available() < 2) {
      if (!client.connected()) break;
      delay(1);
      continue;
    }
    uint8_t lb[2];
    client.read(lb, 2);
    uint16_t frame_len = (lb[0] << 8) | lb[1];
    if (frame_len == 0) { Serial.println("End of speech"); break; }
    if (frame_len > OPUS_MAX_PACKET) { Serial.printf("Bad opus len %d\n", frame_len); break; }

    size_t got = 0;
    while (got < frame_len && (client.connected() || client.available())) {
      int a = client.available();
      if (a > 0) got += client.read(opus_packet_buffer + got, min(a, (int)(frame_len - got)));
      else delay(1);
    }
    if (got < frame_len) break;

    int n = opus_decode(opus_decoder, opus_packet_buffer, frame_len, opus_pcm_buffer, OPUS_FRAME_SIZE, 0);
    if (n < 0) { Serial.printf("Opus decode err %d\n", n); continue; }
    if (frameCount++ < 3) Serial.printf("Opus frame %d: %d samples\n", frameCount, n);

    // Duplicate mono -> stereo for the ES8311 (fed via the shared I2S TX).
    static int16_t out[OPUS_FRAME_SIZE * 2];
    for (int i = 0; i < n; i++) { out[i * 2] = opus_pcm_buffer[i]; out[i * 2 + 1] = opus_pcm_buffer[i]; }
    i2s.write((uint8_t *)out, n * 2 * sizeof(int16_t));
  }
  isPlaying = false;
  drawnState = -1;   // force the idle tick to repaint the listening orb
  Serial.println("Playback done");
}

// ─── Standalone (Option 2): cloud TTS -> speaker (no laptop) ─
// POST multipart {target_text, voice_id, output_format} to NavAI TTS, read the
// 24kHz mono WAV, decimate to 16kHz, and play on the ES8311. Buffers the WAV in
// PSRAM. Returns true on success.
bool cloudTTSPlay(const String &text) {
  WiFiClientSecure tls;
  tls.setInsecure();                          // skip cert check (demo)
  HTTPClient http;
  http.setTimeout(20000);
  if (!http.begin(tls, "https://ai.navai.pro/api/tts/synthesize")) {
    Serial.println("TTS begin FAIL"); return false;
  }
  http.addHeader("X-API-Key", NAVAI_KEY);
  const String bnd = "----navaiB0undary";
  http.addHeader("Content-Type", "multipart/form-data; boundary=" + bnd);
  String body;
  body += "--" + bnd + "\r\nContent-Disposition: form-data; name=\"target_text\"\r\n\r\n" + text + "\r\n";
  body += "--" + bnd + "\r\nContent-Disposition: form-data; name=\"voice_id\"\r\n\r\nfrankfurt\r\n";
  body += "--" + bnd + "\r\nContent-Disposition: form-data; name=\"output_format\"\r\n\r\nwav\r\n";
  body += "--" + bnd + "--\r\n";
  uint32_t t0 = millis();
  int code = http.POST((uint8_t *)body.c_str(), body.length());
  Serial.printf("TTS POST -> %d (%lums)\n", code, (unsigned long)(millis() - t0));
  if (code != 200) { http.end(); return false; }

  // BUFFERED playback: download the whole reply into PSRAM first, THEN resample 24k->16k
  // (linear interp) and play. Streaming-while-downloading glitched — the ~55 kB/s TLS
  // ceiling barely exceeds the 48 kB/s WAV playback rate, so playback outran the download
  // and paused mid-word. Answers are terse now, so the full download is small (~1.3s) and
  // the audio is perfectly smooth. tstart measures the whole fetch.
  int total = http.getSize();
  size_t cap = (total > 0) ? (size_t)total + 8 : 500000;
  uint8_t *wav = (uint8_t *)ps_malloc(cap);
  if (!wav) { Serial.println("TTS ps_malloc FAIL"); http.end(); return false; }
  WiFiClient *st = http.getStreamPtr();
  size_t got = 0; uint32_t tlast = millis(), tdl = millis();
  while ((total < 0 || got < (size_t)total) && got < cap) {
    int r = st->read(wav + got, cap - got);
    if (r > 0) { got += r; tlast = millis(); }
    else {
      if (!http.connected() && st->available() == 0) break;
      if (millis() - tlast > 3000) break;
      delay(1);
    }
  }
  http.end();
  Serial.printf("TTS got %u bytes (%lums dl)\n", (unsigned)got, (unsigned long)(millis() - tdl));
  if (got < 44) { free(wav); return false; }

  int16_t *pcm = (int16_t *)(wav + 44);
  size_t nsamp = (got - 44) / 2;
  size_t outN = nsamp * 2 / 3;                 // 24k -> 16k
  isPlaying = true;
  static int16_t out[512 * 2];
  size_t oi = 0;
  for (size_t j = 0; j < outN; j++) {
    float sp = j * 1.5f;                        // source position for output sample j
    size_t i0 = (size_t)sp;
    float f = sp - i0;
    int a = pcm[i0];
    int b = (i0 + 1 < nsamp) ? pcm[i0 + 1] : a;
    int16_t s = (int16_t)(a + f * (b - a));
    out[oi * 2] = s; out[oi * 2 + 1] = s; oi++;
    if (oi == 512) { i2s.write((uint8_t *)out, 512 * 2 * sizeof(int16_t)); oi = 0; }
  }
  if (oi > 0) i2s.write((uint8_t *)out, oi * 2 * sizeof(int16_t));
  isPlaying = false;
  free(wav);
  return true;
}

// ─── Standalone: STT (NavAI) ────────────────────────────────
// Wrap the utterance PCM as a 16kHz mono WAV, POST multipart to NavAI STT,
// return the Uzbek transcript ("" on failure).
String cloudSTT(int16_t *pcm, size_t n) {
  size_t dataBytes = n * 2;
  const String bnd = "----navaiSTTb0und";
  String pre = "--" + bnd + "\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
  String post = "\r\n--" + bnd + "\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nuz\r\n--" + bnd + "--\r\n";
  size_t bodyLen = pre.length() + 44 + dataBytes + post.length();
  uint8_t *body = (uint8_t *)ps_malloc(bodyLen);
  if (!body) { Serial.println("STT ps_malloc FAIL"); return ""; }
  size_t p = 0;
  memcpy(body + p, pre.c_str(), pre.length()); p += pre.length();
  uint8_t *w = body + p;                              // 44-byte WAV header, 16kHz mono pcm16
  uint32_t sr = SAMPLE_RATE, byr = SAMPLE_RATE * 2, csz = 36 + dataBytes, dsz = dataBytes, f16 = 16;
  uint16_t fmt = 1, ch = 1, ba = 2, bps = 16;
  memcpy(w, "RIFF", 4); memcpy(w + 4, &csz, 4); memcpy(w + 8, "WAVE", 4);
  memcpy(w + 12, "fmt ", 4); memcpy(w + 16, &f16, 4);
  memcpy(w + 20, &fmt, 2); memcpy(w + 22, &ch, 2); memcpy(w + 24, &sr, 4);
  memcpy(w + 28, &byr, 4); memcpy(w + 32, &ba, 2); memcpy(w + 34, &bps, 2);
  memcpy(w + 36, "data", 4); memcpy(w + 40, &dsz, 4);
  p += 44;
  memcpy(body + p, pcm, dataBytes); p += dataBytes;
  memcpy(body + p, post.c_str(), post.length()); p += post.length();

  WiFiClientSecure tls; tls.setInsecure();
  HTTPClient http; http.setTimeout(20000);
  http.begin(tls, "https://ai.navai.pro/api/stt/transcribe/transcribe-single");
  http.addHeader("X-API-Key", NAVAI_KEY);
  http.addHeader("Content-Type", "multipart/form-data; boundary=" + bnd);
  uint32_t t0 = millis();
  int code = http.POST(body, bodyLen);
  String resp = http.getString();
  http.end(); free(body);
  Serial.printf("STT -> %d (%lums): %s\n", code, (unsigned long)(millis() - t0), resp.substring(0, 180).c_str());
  if (code != 200) return "";
  JsonDocument doc;
  if (deserializeJson(doc, resp)) return "";
  const char *t = doc["text"] | "";
  if (!t[0]) t = doc["transcription"] | "";
  if (!t[0]) t = doc["transcript"] | "";
  if (!t[0]) t = doc["result"] | "";
  return String(t);
}

// ─── Standalone: STT over WebSocket (the REST endpoint is broken) ─
// wss://ai.navai.pro/api/stt/transcribe/live, subprotocol "stt.v1":
//   send {"type":"start",...} -> raw pcm16 frames -> {"type":"end"}
//   receive {"type":"final","text":...}.
static WebSocketsClient g_sttWs;
static volatile bool g_sttStarted = false;
static volatile bool g_sttDone = false;
static String g_sttText;
void sttWsEvent(WStype_t type, uint8_t *payload, size_t len) {
  if (type == WStype_CONNECTED) {
    g_sttWs.sendTXT("{\"type\":\"start\",\"language\":\"uz\",\"sample_rate\":16000,\"encoding\":\"pcm16\"}");
    g_sttStarted = true;
  } else if (type == WStype_TEXT) {
    JsonDocument d;
    if (!deserializeJson(d, payload, len)) {
      const char *ty = d["type"] | "";
      if (!strcmp(ty, "final")) { g_sttText = String((const char *)(d["text"] | "")); g_sttDone = true; }
    }
  }
}
// Streaming STT task (pinned to core 0, next to WiFi). Watches vadRec: on speech onset
// it opens the WS and uploads uttBuf as it grows; on speech end it sends "end" and waits
// for the final transcript, then hands it to loop() via sttTranscript/sttReady. Because
// the TLS handshake + audio upload overlap the user's speech, the transcript is ready
// almost immediately when they stop — instead of a ~2.6s blocking call afterwards.
void sttTask(void *param) {
  for (;;) {
    if (!vadRec || sttReady || gProcessing) { vTaskDelay(pdMS_TO_TICKS(6)); continue; }
    uint32_t T = millis();
    g_sttStarted = false; g_sttDone = false; g_sttText = "";
    String url = String("/api/stt/transcribe/live?api_key=") + NAVAI_KEY;
    g_sttWs.beginSSL("ai.navai.pro", 443, url.c_str(), "", "stt.v1");
    g_sttWs.onEvent(sttWsEvent);
    while (!g_sttStarted && millis() - T < 8000) { g_sttWs.loop(); vTaskDelay(1); }
    size_t sent = 0;
    if (g_sttStarted) {
      while (vadRec || sent < uttLen) {               // stream while recording OR unsent data remains
        while (sent < uttLen) {
          size_t chunk = min(uttLen - sent, (size_t)1024);
          g_sttWs.sendBIN((uint8_t *)(uttBuf + sent), chunk * 2);
          sent += chunk;
          g_sttWs.loop();
        }
        g_sttWs.loop();
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      g_sttWs.sendTXT("{\"type\":\"end\"}");
      uint32_t t1 = millis();
      while (!g_sttDone && millis() - t1 < 8000) { g_sttWs.loop(); vTaskDelay(2); }
    }
    g_sttWs.disconnect();
    sttTranscript = (sent >= (size_t)VAD_MIN_SAMP) ? g_sttText : String("");   // drop tiny blips
    Serial.printf("STT(stream) %lums, %u samp -> %s\n",
                  (unsigned long)(millis() - T), (unsigned)sent, sttTranscript.c_str());
    sttReady = true;                                  // hand off to loop()
    while (sttReady) vTaskDelay(pdMS_TO_TICKS(5));     // wait until loop() consumes + resets
  }
}

// ─── Standalone: LLM (Gemini) ───────────────────────────────
String cloudLLM(const String &userText) {
  WiFiClientSecure tls; tls.setInsecure();
  HTTPClient http; http.setTimeout(20000);
  http.begin(tls, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions");
  http.addHeader("Authorization", String("Bearer ") + GEMINI_KEY);
  http.addHeader("Content-Type", "application/json");
  JsonDocument req;
  req["model"] = "gemini-3.5-flash-lite";
  req["max_tokens"] = 220;
  JsonArray msgs = req["messages"].to<JsonArray>();
  JsonObject sys = msgs.add<JsonObject>();
  sys["role"] = "system";
  sys["content"] = "Sen NavAI ovozli yordamchisan, o'zbek tilida javob berasan. Oddiy yoki faktik savolga JUDA qisqa javob ber (1-4 so'z, masalan 'Toshkent'). Agar foydalanuvchi hikoya, ertak, tushuntirish yoki ro'yxat so'rasa — 4-6 jumlada to'liq va tugallangan javob ber. Muqaddima va izohsiz, faqat javobning o'zi.";
  JsonObject usr = msgs.add<JsonObject>();
  usr["role"] = "user"; usr["content"] = userText;
  String reqBody; serializeJson(req, reqBody);
  uint32_t t0 = millis();
  int code = http.POST(reqBody);
  String resp = http.getString();
  http.end();
  Serial.printf("LLM -> %d (%lums)\n", code, (unsigned long)(millis() - t0));
  if (code != 200) { Serial.println(resp.substring(0, 150)); return ""; }
  JsonDocument doc;
  if (deserializeJson(doc, resp)) return "";
  const char *c = doc["choices"][0]["message"]["content"] | "";
  return String(c);
}

// ─── Setup ──────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n=== ws-audio onju front-end ===");
  Serial.println(">>> FW build: pipeline-v5 (LCD voice orb + status)");

  initAudio();
  initDisplay();     // ST77916 QSPI LCD — after initAudio() so Wire/I2C is up
  loadConfig();

  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(desired_hostname, sizeof(desired_hostname), "%s-%02X%02X%02X", HOST_NAME, mac[3], mac[4], mac[5]);
  WiFi.setHostname(desired_hostname);
  Serial.printf("Hostname: %s  Git: %s\n", desired_hostname, GIT_HASH);

  // Portable WiFi via WiFiManager: joins the saved network, or — if none saved
  // or it's out of range (moved to a new place) — opens a "NavAI-Setup" AP +
  // captive portal (shown on the LCD) so ANY WiFi can be set from a phone, no
  // laptop needed.
  WiFiManager wm;
  wm.setConfigPortalTimeout(300);            // 5 min in portal, then reboot & retry
  wm.setAPCallback([](WiFiManager *m) {
    Serial.printf("WiFi setup: join AP '%s'\n", m->getConfigPortalSSID().c_str());
    drawWifiSetup(m->getConfigPortalSSID());
  });
  Serial.print("WiFi (autoConnect)...");
  if (!wm.autoConnect("NavAI-Setup")) {
    Serial.println(" portal timeout — restarting");
    ESP.restart();
  }
  WiFi.setSleep(false);   // CRITICAL: disable modem power-save — otherwise the radio
                          // sleeps between DTIM beacons, throttling TLS downloads to
                          // ~28 kB/s and adding ~100ms wake latency to every round-trip.
  Serial.printf("\nIP: %s  RSSI: %d dBm\n", WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());

#if STANDALONE
  uttBuf = (int16_t *)ps_malloc((size_t)UTT_MAX_SAMP * 2);
  Serial.printf("STANDALONE uttBuf: %s\n", uttBuf ? "OK" : "FAIL");
  if (lcdReady) { drawnState = -1; drawUi(UI_SPEAKING, millis()); }
  cloudTTSPlay("Salom, tinglayapman.");   // short greeting (also HTTPS+TTS check)
  if (lcdReady) { drawnState = -1; drawUi(UI_LISTENING, millis()); }
#endif

  udp.begin(3000);
  tcpServer.begin();
  tcpServer.setNoDelay(true);
  MDNS.begin(desired_hostname);

  if (serverIP == IPAddress(0, 0, 0, 0) && server_hostname != DEFAULT_SERVER) {
    String q = server_hostname; if (q.endsWith(".local")) q = q.substring(0, q.length() - 6);
    for (int i = 0; i < 10; i++) { IPAddress r = MDNS.queryHost(q.c_str()); if (r != INADDR_NONE) { serverIP = r; break; } delay(200); }
  }

  udp.beginPacket(IPAddress(239, 0, 0, 1), 12345);
  String announce = String(desired_hostname) + " " + String(GIT_HASH);
  udp.write((const uint8_t *)announce.c_str(), announce.length());
  udp.endPacket();
  Serial.println("Announced on multicast (VOX)");

  int oe;
  opus_decoder = opus_decoder_create(SAMPLE_RATE, 1, &oe);
  Serial.println(oe == OPUS_OK ? "Opus decoder ready" : "Opus decoder FAIL");

  xTaskCreatePinnedToCore(micTask, "micTask", 4096, NULL, 1, NULL, 1);   // core 1 (with loop); core 0 runs WiFi
#if STANDALONE
  xTaskCreatePinnedToCore(sttTask, "sttTask", 12288, NULL, 1, NULL, 0);  // core 0 (with WiFi) — streams STT while you speak
#endif
  Serial.printf("Device: %s @ %s (%s)\nReady.\n", desired_hostname, WiFi.localIP().toString().c_str(), GIT_HASH);
}

// ─── Main loop: single task owns I2S — mic pump + TCP playback ──────────────
void loop() {
  if (Serial.available()) {
    char ch = Serial.read();
    if (ch == 'r') ESP.restart();
    else if (ch == 'c') enterConfigMode();
    else if (ch == 'w') { WiFiManager wm; wm.resetSettings(); Serial.println("WiFi reset — restarting to setup portal"); delay(300); ESP.restart(); }
  }

#if STANDALONE
  // Standalone conversation: sttTask already produced a transcript WHILE you spoke.
  // Here we just run LLM -> TTS -> play. (No drawUi() in the pipeline — the orb blit
  // stalls on the SPI DMA semaphore during WiFi/TLS activity.)
  if (sttReady) {
    gProcessing = true;
    uint32_t T = millis();
    String you = sttTranscript;
    Serial.printf("\nYOU: %s\n", you.c_str());
    if (you.length() > 0) {
      String reply = cloudLLM(you);
      Serial.printf("[+%lums] BOT: %s\n", (unsigned long)(millis() - T), reply.c_str());
      if (reply.length() > 0) cloudTTSPlay(reply);
      Serial.printf("[+%lums] done\n", (unsigned long)(millis() - T));
    }
    sttReady = false;      // releases sttTask to re-arm for the next utterance
    if (lcdReady) { drawnState = -1; drawUi(UI_LISTENING, millis()); }
    gProcessing = false;
    return;
  }
#endif

  WiFiClient client = tcpServer.available();
  if (!client) {
    // Idle: pulse the voice orb. No TCP playback in flight here, so the QSPI
    // draw doesn't contend with the audio path.
    static uint32_t lastUiMs = 0;
    uint32_t now = millis();
    if (lcdReady && !isPlaying && now - lastUiMs > 60) {
      lastUiMs = now;
#if LCD_DEMO
      uint32_t ph = (now / 1000) % 14;                 // 14s showcase loop
      if (ph < 4)       { capActive = false; drawUi(UI_LISTENING, now); }
      else if (ph < 7)  { capActive = false; drawUi(UI_THINKING, now); }
      else {
        strcpy(capUser,  "Siz: Bugun havo qanday?");
        strcpy(capReply, "Toshkentda 21 daraja, ochiq va shamolsiz.");
        capActive = true; drawUi(UI_SPEAKING, now);
      }
#else
      drawUi(serverIP == IPAddress(0, 0, 0, 0) ? UI_CONNECTING : UI_LISTENING, now);
#endif
    }
    delay(5);
    return;   // micTask handles mic on its own core
  }

  if (serverIP != client.remoteIP()) {
    serverIP = client.remoteIP();
    preferences.begin("ws-cfg", false);
    preferences.putUInt("server_ip", (uint32_t)serverIP);
    preferences.end();
    Serial.println("Server IP learned: " + serverIP.toString());
  }

  uint32_t waitStart = millis();
  while (client.available() < 6) {
    if ((millis() - waitStart) > 2000) { client.stop(); return; }
    delay(1);
  }
  uint8_t header[6];
  client.read(header, 6);

  if (header[0] == 0xAA) {              // audio playback
    uint8_t vol = header[3];           // server volume 0..20
    uint8_t compression = header[5];
    int v = constrain((int)vol * 5, MIN_PLAY_VOLUME, 100);   // -> ES8311, with floor
    if (v != speaker_volume) { speaker_volume = v; es8311_voice_volume_set(es8311_handle, v, NULL); }
    if (compression == 2 && opus_decoder) {
      playOpusStream(client);
    }
    client.stop();
  } else {
    // 0xBB/0xCC/0xDD (LED / mic-timeout) — no LEDs on this board; just ack.
    client.stop();
  }
}
