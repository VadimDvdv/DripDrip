# DripGuard firmware v0.2 — ESP32-DevKitC (WROOM-32UE)

The sketch is `DripDrip/DripDrip.ino` (Arduino IDE needs the folder and the `.ino` to share a name).

Water-only test rig. Not for clinical use.

## What's here

| File | Purpose |
|---|---|
| `DripDrip/DripDrip.ino` | Firmware (Arduino IDE: open this; PlatformIO: `pio run -t upload`) |
| `platformio.ini` | PlatformIO config, `esp32dev`, monitor at 921600 |
| `tools/dg_log.py` | Standalone logger. Superseded by the dashboard, which logs everything itself; never run both, since only one program can open the port |

**Split of work:** the ESP32 does timing-critical sampling, drop detection and the two flow estimates with their quality metrics. Fusion, drop-volume auto-calibration, alerts and the dashboard run on the laptop, where they're faster to iterate. Everything they need is in the status stream.

## Wiring

| Signal | ESP32 pin | Notes |
|---|---|---|
| Drop receiver → ADC | **GPIO34** (ADC1_CH6) | Input-only. Keep the signal within ~0.15–3.1 V |
| Bubble receiver (later) | GPIO35 (ADC1_CH7) | Reserved |
| HX711 DT/DOUT | **GPIO19** | |
| HX711 SCK | **GPIO18** | |
| OLED SDA / SCL | **GPIO21 / GPIO22** | VCC on 3V3 |
| HX711 VCC | **3V3** | **Not 5 V.** On these green boards VCC also sets the DOUT logic level, and 5 V logic into the ESP32 can damage it |
| HX711 GND, receiver GND, LED cathode side | GND | Common ground |
| IR LED anode | 5V via **150 Ω** | ≈25 mA, steady. Don't drive it from a GPIO |

Load cell → HX711 (usual colors): Red E+, Black E−, White A−, Green A+. If mass goes the wrong way it doesn't matter, because `cal` picks up the sign.

### Receiver front end (choose after the multimeter diode-mode test)

- **Photodiode** (≈0.5 V one way, OL the other): reverse-biased. Cathode to 3V3, anode to GPIO34, 470 kΩ from anode to GND (try 100 kΩ–1 MΩ). Optionally add 1 nF across the resistor to cut high-frequency noise (τ ≈ 0.5 ms, still fast enough for drops).
- **Phototransistor** (OL both ways): collector to 3V3, emitter to GPIO34, 10–47 kΩ from emitter to GND.

**Target:** with the LED on and no drop, `o_base` should be 800–3000 counts. If it's near 0 or `o_sat=1`, change the resistor, the alignment or the LED current; don't change the firmware.

## OLED screen (v0.2)

**Library:** Arduino IDE → Library Manager → install **Adafruit SSD1306** (accept "install all", which adds Adafruit GFX). To build without a screen, set `#define USE_OLED 0` at the top of the sketch.

**Wiring (GME12864-11, 4 pins):** VCC → **3V3**, GND → GND, SDA → **GPIO21**, SCL → **GPIO22**. Address 0x3C or 0x3D is detected automatically. If the screen isn't found, the board prints a message and carries on without it.

**Pages** (press the board's **BOOT** button to switch):

| Page | Shows |
|---|---|
| 0 Status | Big verdict word, a second line, sensor checks, mL remaining; a dot in the top-right corner blinks on every detected drop |
| 1 Agreement | Optical vs weight flow as two bars, plus the auto-calibrated drop factor (needs the dashboard) |
| 2 IR scope | Live receiver signal over the last 0.5 s with the detection threshold lines. Use it to align the sensor without the Serial Plotter |

- **With the dashboard connected,** the status page shows the dashboard's diagnosis. Alarms flash (inverted) and sensor faults get a frame; both take over any page except the scope.
- **Without the dashboard for more than 3 s,** the screen shows "SOLO" and only what the device knows by itself: drop-based flow, NO DROPS, NO IR, and the scale reading. It never claims "confirmed".

**Other v0.2 changes:**
- HX711 samples go through a median-of-3 filter, so one corrupt read can't knock out the weight channel.
- `avg <n>` sets an optical moving average (1 = off, the default; not saved). If the scope or plotter shows mains hum, try `avg 17`. It also smooths the drop pulse, so check the drops are still counted.

## Mechanical gotchas that ruin the weight signal

- The drip chamber hangs from the reservoir, so **anything attached to the chamber is weighed**. Mount the optical holder on the chamber with loose, slack wire loops, or mount it on the stand without touching the chamber.
- Tape the outlet tubing to the stand just below the chamber, so a swinging tube or the collection cup can't pull on the load cell.
- Bolt the load cell rigidly at one end (arrow on the label points in the load direction). Hang the reservoir only from the other end.

## Bring-up order

1. **Flash.** Arduino IDE: board "ESP32 Dev Module", serial monitor at **921600**, line ending "Newline". You should see `{"type":"boot",...}`, the config, and then status lines twice per second.
2. **HX711 first (no water):** `w_ok` should be 1. Run `hxraw 1` and press on the cell; raw counts should move by tens of thousands. Then `hxraw 0`.
3. **Calibrate:** hang the empty hook/fixture, then `tare`. Hang a known mass (a full 500 mL bottle weighed on a kitchen scale is fine), then `cal 500`. Check that `w_g` reads correctly, then remove the mass.
4. **Zero-fluid reference:** hang the empty reservoir with the IV set attached and run `empty`. Then `w_rem` is the remaining mL. Or just `tare` again with the empty reservoir, which keeps the calibration.
5. **Optical:** send `raw 1`, close the serial monitor and open **Tools → Serial Plotter** (921600). You'll see `raw`, `base`, `hi`/`lo` threshold lines and `det`, which steps up when an event is detected. Let water drip. Each drop should cross `hi` or `lo` once, and `det` should step once per drop. Send `raw 0` to return to JSON.
6. **Run `tools/dg_log.py`** and compare `OPT` vs `WT` flow over a few minutes at 2–3 roller-clamp positions.

## Tuning the drop detector

The threshold is `max(k × noise, mind)`, where `noise` is the running mean-absolute deviation (≈0.8σ). Simulated with the defaults: drops ≥ ~5σ above the noise are all counted; ~4σ drops are mostly missed.

| Symptom | Fix |
|---|---|
| Missed drops, `o_snr` < ~8 | Improve optics first (shielding, alignment, beam just below the drip tip, bigger receiver resistor). Then `k 4` |
| Double counts per drop | `refr 80` (keep it below the drop interval) |
| `o_spk` climbing | Electrical noise: shorter wires, the 1 nF cap, common ground |
| `o_long` climbing during normal flow | Flow is a stream, not drops (reduce flow), or `maxev 250` if drops are just slow |
| `o_long` +2 when you block the beam | Correct: it's the start and end of a non-drop deviation. Your fusion code can use it |

**Keep the demo flow at about 0.5–2 drops/s.** Above ~3–4 drops/s the drops merge into a stream.

## Weight flow: expect this precision

`w_q` is the least-squares slope over `win` seconds, and `w_qse` is its standard error. The absolute error is roughly the same at any flow rate. In simulation with 0.3 g noise and `win 30`, it was ±0.13 mL/min:

- at 1 mL/min, that's ±13 %
- at 3 mL/min (1 drop/s at 20 gtt/mL), it's ±4 %

Use 2–4 mL/min for the demo. Increase `win` to 60 for calibration runs, and decrease it if you need faster response. After a bump, `w_res` stays high until the bump leaves the window, so the weight estimate is unreliable for `win` seconds. That's the honest behavior.

## Commands

`help`, `cfg`, `reset`, `tare`, `cal <g>`, `empty [g]`, `df <gtt/mL>`, `rho <g/mL>`, `k <x>`, `mind <counts>`, `refr <ms>`, `maxev <ms>`, `win <s>`, `owin <s>`, `stab <g>`, `raw <n>`, `hxraw <0|1>`, `defaults`. Settings persist in flash.

## Output (newline-delimited JSON)

Status line `{"type":"s", ...}` every 500 ms:

| Key | Meaning |
|---|---|
| `t` | ESP32 ms since boot |
| `o_base`, `o_noise` | Optical baseline (ADC counts) and noise (mean abs dev) |
| `o_sat` | 1 if the baseline is near a rail: optics not usable |
| `o_drops`, `o_n` | Drops since reset / in the last `owin` s |
| `o_rate`, `o_q` | Drops/s and optical flow, mL/min (uses `df`) |
| `o_since` | Seconds since the last drop |
| `o_amp`, `o_snr` | Mean drop amplitude and amp/noise: optical health |
| `o_long`, `o_spk` | Non-drop long deviations / rejected 1-sample spikes (cumulative) |
| `w_ok`, `w_cal` | HX711 responding / calibrated |
| `w_raw`, `w_g` | Raw counts, mass in g (1 s mean) |
| `w_std1` | Mass std over the last ~1 s: bump detector |
| `w_q`, `w_qse` | Weight flow, mL/min, and its 1σ standard error (`null` until ≥10 s of data) |
| `w_res`, `w_span`, `w_n` | Residual std around the fit, window length, samples |
| `w_stable` | `w_std1 < stab` and `w_res < 2·stab` |
| `w_rem` | mL above the `empty` reference |

Drop line: `{"type":"drop","t":ms,"amp":signed_peak,"w":width_ms}`. Also `msg`, `err`, `cfg` and `hx` types; lines starting with `#` are human-only.

**For the laptop fusion step:** compare `o_q` and `w_q` using `w_qse`. For example, flag disagreement when |o_q − w_q| > 3·w_qse + 10 % of w_q, and only when `w_stable`, `!o_sat` and `w_span ≥ 20`. Auto-calibrate the drop volume as Δmass / (ρ · Δdrops) over the same stable interval.

## Notes

- **WROOM-32UE has no onboard antenna**, only a U.FL/IPEX connector. Wi-Fi without an external 2.4 GHz antenna will barely work, so this firmware uses USB serial only. Get an antenna before attempting remote monitoring.
- If uploads fail: hold **BOOT** when "Connecting…" appears, use a data (not charge-only) USB cable, or lower `upload_speed`.
- Compiles on Arduino-ESP32 core 3.3.12 and runs on the DevKitC: status lines verified with nothing wired. Not yet verified with sensors attached.
- **Aim the beam about 10 mm below the drip tip**, not at the tip: a drop hanging and growing in the beam makes a slow ramp that inflates the noise estimate and hides the real fall. In `raw 1` mode you want short, sharp pulses.
- **Use the smallest receiver resistor that keeps `o_base` in range.** A big resistor on long jumper wires picks up mains hum, which the detector counts as noise, raising the threshold until real drops are missed.
