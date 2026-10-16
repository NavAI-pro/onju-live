# ESP32-S3-Touch-LCD-1.85 (audio variant) — bring-up notes

Board: Waveshare **ESP32-S3-Touch-LCD-1.85** (audio variant, aka 1.85C smart-speaker).
ESP32-S3R8, 8MB PSRAM, 16MB flash.

Flash: `./flash.sh waveshare /dev/cu.usbmodem<NNNN> --no-monitor`
FQBN: `PartitionScheme=app3M_fat9M_16MB`. Native USB CDC — port re-enumerates
after a software reset; if it disappears, replug the USB (use a DATA cable).

## THE WORKING AUDIO CONFIG (hard-won)
The board has **ES8311 speaker codec (I2C 0x18)** + **ES7210 mic ADC (I2C 0x40)**
on ONE shared full-duplex I2S bus. (Docs wrongly said "PCM5101" and mislabeled
the MCLK pin as "MIC_WS".) I2C bus also has TCA9554 (0x20) + RTC (0x51).

Pins (from Waveshare's own 1.85C Arduino examples 03_audio_out / 08_esp_sr):
```
MCLK = GPIO2   (docs said "MIC_WS" — WRONG, it's the master clock!)
BCK  = GPIO48
WS   = GPIO38
DOUT = GPIO47  -> ES8311 speaker
DIN  = GPIO39  <- ES7210 mic
I2C: SDA=GPIO11, SCL=GPIO10.   MCLK = 256 * sample_rate (4.096MHz @16k).
```

**What finally worked (and what didn't):**
- Legacy `driver/i2s.h` + pschatzmann audio-driver library → mic stayed dead
  even with correct pins. The speaker worked (ES8311, SCLK) but mic never did.
- **Winner:** built-in `ESP_I2S` library (`I2SClass`, `i2s.begin(I2S_MODE_STD,...)`)
  + Waveshare's OWN bundled drivers `es7210.cpp` / `es8311.cpp` (in this folder,
  copied from the 1.85C repo), with **MCLK on GPIO2**. Both codecs use MCLK.
- ES7210 config: I2S fmt, mclk_ratio 256, 16-bit, bias 2.87V, gain 36dB,
  ADC volume ~90. ES8311: `mclk_from_mclk_pin=true`, 16-bit, volume ~75.

Result: mic produces live data (was 0 before), speaker plays. Both confirmed.

## Files
- `waveshare.ino` — bring-up (mic→speaker loopback). Uses ESP_I2S + es7210/es8311.
- `es7210.cpp/.h/_reg.h`, `es8311.cpp/.h/_reg.h` — Waveshare's codec drivers
  (from waveshareteam/ESP32-S3-Touch-LCD-1.85C, examples 03/08). Both use Wire.

## Reference (authoritative)
- github.com/waveshareteam/ESP32-S3-Touch-LCD-1.85C  → Arduino/examples/
  03_audio_out_no_tf (ES8311 speaker) and 08_esp_sr (ES7210 mic) — the exact
  working pin + codec config.

## Pipeline status
DONE — WiFi, multicast announce, UDP mu-law mic upstream, TCP Opus playback all
work (audio layer = ESP_I2S full-duplex read/write). Confirmed end-to-end:
VAD → STT (navai-ws stream) → LLM → TTS → speaker. No RGB LEDs on this board.
Mic is HOT — keep `MIC_GAIN 1` (raw AC ~31000 on speech; any boost clips → STT
returns empty). Server needs the laptop on 2.4 GHz (rejoin "Mutolaa") + a TCP
teach so the device learns the current server IP (DHCP churns it).

## LCD (ST77916 QSPI, 360x360 round) — hard-won
Pins (from the 1.85C repo example 01_lvgl_example, `Display_ST77916.h`):
```
SCK=40  D0=46  D1=45  D2=42  D3=41  CS=21    (QSPI, all distinct from audio pins)
Backlight = GPIO5 (active high).  RST = TCA9554 EXIO2 (bit 1), not a GPIO.
Touch reset = TCA9554 EXIO1.  I2C SDA11/SCL10 shared w/ codecs + expander(0x20).
```
- **Winner: Espressif `esp_lcd` public SPI-IO API — NOT Arduino_GFX.** Arduino_GFX's
  `Arduino_ESP32QSPI` bus corrupts complex quad data on this panel: solid RED/WHITE/
  BLUE (simple bit patterns) render, but GREEN/CYAN (`0x07E0`-style, multi-line quad
  transitions) come out as **vertical stripes** — data-dependent, and even 2 MHz only
  partly masks it. The factory demo is clean because it uses `esp_lcd`. (Don't be
  fooled into calling it a hardware defect — the glass is fine.)
- We replicate the factory driver with the **public** esp_lcd API (no vendor .c or
  internal headers): `esp_lcd_new_panel_io_spi` with `flags.quad_mode=1`,
  `lcd_cmd_bits=32`, `lcd_param_bits=8`, `spi_mode=0`, `pclk_hz=40 MHz`. Commands go
  out as `(0x02<<24)|(cmd<<8)`, pixels as `(0x32<<24)|(0x2C<<8)` (ST77916 QSPI
  opcodes). Init = factory `vendor_specific_init_new` → `st77916_init_cmds.h`, after
  SWRESET + MADCTL `0x00` + COLMOD `0x55`. Pixels are **big-endian** (store swapped).
- Reset: TCA9554 EXIO2 as output, pulse low→high, before creating the panel IO.
  Backlight = `digitalWrite(5, HIGH)`.
- **PSRAM framebuffer needs a banded blit.** SPI DMA can't send the 259 KB PSRAM fb
  directly — esp_lcd's same-size bounce alloc fails (`setup_dma_priv_buffer`) and
  also starves the WiFi task. Fix: copy 40-row bands into a small internal
  `MALLOC_CAP_DMA` scratch and `tx_color` each, waiting on an `on_color_trans_done`
  semaphore before reusing the scratch.
- UI = **voice light-ring** (dots near the rim): colour = state (cyan idle / green
  speaking / gray connecting), a bright point rotates while listening. Drawn from
  `loop()` when idle; playback paints one static "speaking" frame (no draws inside
  the audio loop). The whole panel renders cleanly now — the ring is just the first UI.
