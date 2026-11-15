/*
  DripGuard firmware v0.1
  ------------------------------------------------------------------
  Board : Espressif ESP32-DevKitC, ESP32-WROOM-32UE module
  Core  : Arduino-ESP32 2.x or 3.x  (Arduino IDE: "ESP32 Dev Module")

  What this firmware does
    - Samples the IR drop receiver at 1 kHz (4x oversampled) on core 1
      and detects drops with an adaptive baseline + noise threshold.
      Works for either signal polarity, so the receiver type
      (photodiode / phototransistor) doesn't need to be known up front.
    - Reads the HX711 at its native ~10 Hz on core 0 (bit-banged,
      critical section so SCK can never stay high long enough to
      power the chip down).
    - Every 500 ms, prints one flat JSON status line with:
        optical flow   (drop rate / drop factor)
        weight flow    (least-squares slope of mass over a window,
                        with its standard error)
        signal-quality metrics for both sensors
    - Prints one JSON line per detected drop.
    - Serial commands for tare / calibration / tuning, saved to flash.

  Sensor fusion, auto-calibration of drop volume, alerts and the
  dashboard are intentionally on the laptop side: they iterate faster
  in Python and every input they need is in the status stream.

  v0.2: OLED status display (GME12864-11, SSD1306, I2C 0x3C), `disp`
  command so the laptop can show its diagnosis on the device, BOOT-button
  pages, median-of-3 HX711 filter, optional optical moving average (`avg`).
  Needs libraries: "Adafruit SSD1306" (installs "Adafruit GFX" with it).
  Set USE_OLED to 0 to build without them.

  Water-only test rig. Not for clinical use.
  ------------------------------------------------------------------
*/
#define USE_OLED 1

#include <Arduino.h>
#include <Preferences.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#if USE_OLED
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#endif

// =================================================================
// Pins
// =================================================================
// Use ADC1 pins only: ADC2 is unusable while Wi-Fi is on.
static const int PIN_OPT_DROP   = 34;  // ADC1_CH6, input-only, no pull-ups
static const int PIN_OPT_BUBBLE = 35;  // ADC1_CH7, reserved for Level 3 air sensor
static const int PIN_HX_DOUT    = 19;
static const int PIN_HX_SCK     = 18;
static const int PIN_OLED_SDA   = 21;
static const int PIN_OLED_SCL   = 22;
static const int PIN_BTN        = 0;   // on-board BOOT button: cycles display pages

// =================================================================
// Fixed tuning constants
// =================================================================
static const int      OPT_OVERSAMPLE      = 4;
static const float    OPT_BASE_ALPHA      = 1.0f / 500.0f;   // ~0.5 s baseline time constant at 1 kHz
static const float    OPT_NOISE_ALPHA     = 1.0f / 2000.0f;  // ~2 s noise-estimate time constant
static const uint32_t OPT_MIN_EVENT_MS    = 2;      // shorter = single-sample spike, rejected
static const uint32_t OPT_BOOT_SETTLE_MS  = 2000;   // let baseline/noise converge after boot
static const uint32_t OPT_REBASE_SETTLE_MS= 300;    // after a long (non-drop) deviation
static const float    OPT_SAT_LO          = 40.0f;  // baseline near rails = bad optical setup
static const float    OPT_SAT_HI          = 4050.0f;

static const uint32_t STATUS_PERIOD_MS    = 500;
static const uint32_t HX_TIMEOUT_MS       = 1000;
static const int      DROP_RING           = 256;
static const int      MASS_RING           = 1300;   // > 120 s at 10 Hz

// =================================================================
// Persistent configuration
// =================================================================
struct Config {
  uint32_t magic;
  float    hxOffset;      // HX711 raw counts at zero load
  float    hxScale;       // raw counts per gram (sign included)
  uint8_t  hxCal;         // 1 once "cal" has been run
  float    emptyG;        // mass reading that corresponds to zero fluid
  float    dropFactor;    // drops per mL (confirm on the JorVet package)
  float    rho;           // fluid density, g/mL
  float    kSigma;        // drop threshold = kSigma * noise (mean abs deviation)
  float    minDelta;      // ... but never below this many ADC counts
  uint16_t refractoryMs;  // dead time after a drop
  uint16_t maxEventMs;    // longer deviations are not drops (stream / blockage)
  float    winS;          // weight regression window, s
  float    optWinS;       // optical rate window, s
  float    stabG;         // 1-s std above this (g) => weight unstable
};
static const uint32_t CFG_MAGIC = 0xD6170001;

// What the laptop last told us to show (`disp` command). Touched only by loop().
struct HostDisp {
  char level[9], big[12], line2[22], qbest[9], qo[9], qw[9], rem[9], cal[9];
  char opt, wt;
  uint32_t rxMs;
};
static HostDisp hd = {};
static const uint32_t HOST_TIMEOUT_MS = 3000;

// Optical "scope" for the display: 128 points, one per 4 samples (0.5 s window).
static const int SCOPE_N = 128;
static uint16_t scopeBuf[SCOPE_N];
static volatile uint16_t scopeHead = 0;
static volatile float scopeBase = 0, scopeThr = 0;
static volatile uint8_t avgN = 1;      // optical moving average length (1 = off), not persisted
static Config      cfg;
static Preferences prefs;

// Result types of computeOptical()/computeWeight(). Defined up here because the
// Arduino IDE auto-inserts function prototypes before the first function.
struct OStats {
  float base, noise, amp, rate, q, since;
  uint32_t drops, nWin, longEv, spikes;
  bool sat;
};

struct WStats {
  bool  ok, cal, qValid, regOk;
  int   n;
  float span, gNow, std1, slope, slopeSE, resid;
  int32_t raw;
};

static void setDefaults() {
  cfg.magic        = CFG_MAGIC;
  cfg.hxOffset     = 0.0f;
  cfg.hxScale      = 1.0f;
  cfg.hxCal        = 0;
  cfg.emptyG       = 0.0f;
  cfg.dropFactor   = 20.0f;
  cfg.rho          = 0.998f;
  cfg.kSigma       = 6.0f;
  cfg.minDelta     = 12.0f;
  cfg.refractoryMs = 40;
  cfg.maxEventMs   = 150;
  cfg.winS         = 30.0f;
  cfg.optWinS      = 30.0f;
  cfg.stabG        = 1.5f;
}

static void saveConfig() { prefs.putBytes("cfg", &cfg, sizeof(cfg)); }

static void loadConfig() {
  setDefaults();
  Config tmp;
  if (prefs.getBytesLength("cfg") == sizeof(Config) &&
      prefs.getBytes("cfg", &tmp, sizeof(tmp)) == sizeof(tmp) &&
      tmp.magic == CFG_MAGIC) {
    cfg = tmp;
  }
}

// =================================================================
// Small helpers
// =================================================================
static inline bool  isFiniteF(float v) { return v == v && v != INFINITY && v != -INFINITY; }
static inline uint32_t minU32(uint32_t a, uint32_t b) { return a < b ? a : b; }

// =================================================================
// Optical channel: shared state
// =================================================================
struct DropEv { uint32_t t; float amp; uint16_t width; };
struct RawS   { uint16_t x; uint16_t base; int16_t lo; uint16_t hi; uint8_t ev; };

static portMUX_TYPE optMux = portMUX_INITIALIZER_UNLOCKED;
static uint32_t dropT[DROP_RING];
static uint32_t dropTotal   = 0;
static float    optBase     = 0.0f;
static float    optNoise    = 0.0f;
static float    ampAvg      = 0.0f;
static float    ampLast     = 0.0f;
static uint32_t longEvents  = 0;
static uint32_t spikeEvents = 0;

static volatile bool     optResetReq = false;
static volatile uint16_t rawDecim    = 0;   // 0 = raw stream off; n = every n-th sample
static QueueHandle_t     dropQ;
static QueueHandle_t     rawQ;

static void registerDrop(uint32_t t, float peak, uint32_t width) {
  float a = fabsf(peak);
  portENTER_CRITICAL(&optMux);
  dropT[dropTotal % DROP_RING] = t;
  dropTotal++;
  ampLast = a;
  ampAvg  = (ampAvg == 0.0f) ? a : ampAvg + (a - ampAvg) * 0.1f;
  portEXIT_CRITICAL(&optMux);

  DropEv e;
  e.t = t; e.amp = peak; e.width = (uint16_t)width;
  xQueueSend(dropQ, &e, 0);
}

// 1 kHz sampler + drop detector. Runs on core 1 at high priority.
static void opticalTask(void *) {
  float    base = 0.0f, noise = 0.0f, evPeak = 0.0f;
  bool     init = false, inEv = false;
  uint32_t settleUntil = millis() + OPT_BOOT_SETTLE_MS;
  uint32_t evStart = 0, lastEvEnd = 0, n = 0;
  static float maHist[32];
  float maSum = 0.0f; uint8_t maIdx = 0, maLen = 0, maN = 1;
  float scopePick = 0.0f, scopeDev = -1.0f;
  TickType_t wake = xTaskGetTickCount();

  for (;;) {
    vTaskDelayUntil(&wake, pdMS_TO_TICKS(1));
    uint32_t now = millis();

    if (optResetReq) {
      portENTER_CRITICAL(&optMux);
      dropTotal = 0; ampAvg = 0; ampLast = 0; longEvents = 0; spikeEvents = 0;
      portEXIT_CRITICAL(&optMux);
      optResetReq = false;
    }

    int32_t acc = 0;
    for (int i = 0; i < OPT_OVERSAMPLE; i++) acc += analogRead(PIN_OPT_DROP);
    float x = (float)acc / OPT_OVERSAMPLE;

    // Optional moving average (`avg n`): ~17 samples nulls 60/120 Hz mains hum.
    uint8_t want = avgN;
    if (want != maN) { maN = want; maSum = 0; maIdx = 0; maLen = 0; }
    if (maN > 1) {
      if (maLen == maN) maSum -= maHist[maIdx]; else maLen++;
      maHist[maIdx] = x; maSum += x;
      maIdx = (uint8_t)((maIdx + 1) % maN);
      x = maSum / maLen;
    }

    if (!init) { base = x; noise = cfg.minDelta / cfg.kSigma; init = true; }

    float d   = x - base;
    float thr = fmaxf(cfg.kSigma * noise, cfg.minDelta);

    if (!inEv) {
      // Track slow changes (ambient light, LED warm-up) only outside events.
      base  += (x - base) * OPT_BASE_ALPHA;
      noise += (fabsf(d) - noise) * OPT_NOISE_ALPHA;
      bool settled = (int32_t)(now - settleUntil) >= 0;
      if (settled && fabsf(d) > thr && (now - lastEvEnd) >= cfg.refractoryMs) {
        inEv = true; evStart = now; evPeak = d;
      }
    } else {
      if (fabsf(d) > fabsf(evPeak)) evPeak = d;
      uint32_t dur = now - evStart;
      if (fabsf(d) < 0.5f * thr) {                 // hysteresis release
        inEv = false; lastEvEnd = now;
        if (dur >= OPT_MIN_EVENT_MS) {
          registerDrop(evStart, evPeak, dur);
        } else {
          portENTER_CRITICAL(&optMux); spikeEvents++; portEXIT_CRITICAL(&optMux);
        }
      } else if (dur > cfg.maxEventMs) {
        // Too long to be a drop: continuous stream, blocked beam,
        // bumped sensor or a light change. Re-baseline at the new level.
        inEv = false; lastEvEnd = now;
        base = x;
        settleUntil = now + OPT_REBASE_SETTLE_MS;
        portENTER_CRITICAL(&optMux); longEvents++; portEXIT_CRITICAL(&optMux);
      }
    }

    n++;
    if ((n & 0x0F) == 0) {
      portENTER_CRITICAL(&optMux);
      optBase = base; optNoise = noise;
      portEXIT_CRITICAL(&optMux);
    }

    // scope: keep the most deviating of every 4 samples so short drops stay visible
    float dev = fabsf(x - base);
    if (dev > scopeDev) { scopeDev = dev; scopePick = x; }
    if ((n & 3) == 0) {
      uint16_t h = scopeHead;
      scopeBuf[h] = (uint16_t)scopePick;
      scopeHead = (uint16_t)((h + 1) % SCOPE_N);
      scopeBase = base; scopeThr = thr;
      scopeDev = -1.0f;
    }

    uint16_t dec = rawDecim;
    if (dec && (n % dec) == 0) {
      RawS r;
      r.x    = (uint16_t)x;
      r.base = (uint16_t)base;
      r.lo   = (int16_t)(base - thr);
      r.hi   = (uint16_t)(base + thr);
      r.ev   = inEv ? 1 : 0;
      xQueueSend(rawQ, &r, 0);
    }
  }
}

// =================================================================
// HX711 channel
// =================================================================
struct MassS { uint32_t t; float g; };

static portMUX_TYPE      hxMux = portMUX_INITIALIZER_UNLOCKED;
static SemaphoreHandle_t massMtx;
static MassS             massBuf[MASS_RING];
static uint32_t          massCount = 0;          // total samples pushed
static volatile int32_t  hxLastRaw = 0;
static volatile uint32_t hxSeq = 0, hxLastMs = 0;
static volatile bool     massClearReq = false;
static volatile bool     hxRawPrint = false;
static volatile bool     hxStuck = false;     // identical raw words in a row => wiring fault

// Channel A, gain 128 (25 clocks). Interrupts off on this core for ~60 us
// so SCK can't be held high > 60 us (which powers the HX711 down).
static bool hxRead(int32_t &out) {
  if (digitalRead(PIN_HX_DOUT) != LOW) return false;
  uint32_t v = 0;
  portENTER_CRITICAL(&hxMux);
  for (int i = 0; i < 24; i++) {
    digitalWrite(PIN_HX_SCK, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | (digitalRead(PIN_HX_DOUT) ? 1u : 0u);
    digitalWrite(PIN_HX_SCK, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(PIN_HX_SCK, HIGH);   // 25th pulse: select A/128 for next conversion
  delayMicroseconds(1);
  digitalWrite(PIN_HX_SCK, LOW);
  delayMicroseconds(1);
  portEXIT_CRITICAL(&hxMux);
  if (v & 0x800000u) v |= 0xFF000000u; // sign-extend 24-bit two's complement
  out = (int32_t)v;
  return true;
}

static int32_t median3(int32_t a, int32_t b, int32_t c) {
  if (a > b) { int32_t t = a; a = b; b = t; }
  if (b > c) { b = c; }
  return a > b ? a : b;
}

static void hxTask(void *) {
  int32_t h0 = 0, h1 = 0; int primed = 0;
  int32_t prevIn = 0; int same = 0;
  for (;;) {
    int32_t rawIn;
    if (hxRead(rawIn)) {
      // A live HX711 always has tens of counts of noise, so 10 identical words in a row
      // means DOUT isn't really the HX711: e.g. no VCC (chip back-powered through SCK, DOUT
      // copies the clock -> raw = -1) or DOUT shorted to SCK. Also catches rail values.
      same = (rawIn == prevIn) ? same + 1 : 0;
      prevIn = rawIn;
      bool stuck = same >= 10;
      if (stuck && !hxStuck) Serial.println("{\"type\":\"err\",\"text\":\"HX711 data stuck - check HX711 VCC=3.3V and that DT/SCK aren't shorted\"}");
      hxStuck = stuck;
      // Median of the last 3 samples: one corrupt read can't knock out the weight channel.
      int32_t raw = rawIn;
      if (primed >= 2) raw = median3(h0, h1, rawIn); else primed++;
      h0 = h1; h1 = rawIn;
      uint32_t now = millis();
      float g = ((float)raw - cfg.hxOffset) / cfg.hxScale;
      xSemaphoreTake(massMtx, portMAX_DELAY);
      if (massClearReq) { massCount = 0; massClearReq = false; }
      MassS s; s.t = now; s.g = g;
      massBuf[massCount % MASS_RING] = s;
      massCount++;
      xSemaphoreGive(massMtx);
      hxLastRaw = raw;
      hxLastMs  = now;
      hxSeq     = hxSeq + 1;
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

// =================================================================
// Derived quantities
// =================================================================

static OStats computeOptical(uint32_t now) {
  static uint32_t ts[DROP_RING];  // static: keep 1 kB off the loop stack
  OStats o;
  uint32_t total;
  portENTER_CRITICAL(&optMux);
  total    = dropTotal;
  memcpy(ts, dropT, sizeof(ts));
  o.base   = optBase;
  o.noise  = optNoise;
  o.amp    = ampAvg;
  o.longEv = longEvents;
  o.spikes = spikeEvents;
  portEXIT_CRITICAL(&optMux);

  o.drops = total;
  o.sat   = (o.base < OPT_SAT_LO) || (o.base > OPT_SAT_HI);

  uint32_t avail = minU32(total, DROP_RING);
  uint32_t winMs = (uint32_t)(cfg.optWinS * 1000.0f);
  uint32_t first = 0, last = 0, n = 0;
  for (uint32_t i = 0; i < avail; i++) {
    uint32_t t = ts[(total - 1 - i) % DROP_RING];
    if (now - t > winMs) break;
    if (n == 0) last = t;
    first = t;
    n++;
  }
  o.nWin  = n;
  o.since = total ? (now - ts[(total - 1) % DROP_RING]) / 1000.0f : NAN;

  float rate;
  if (n >= 3 && last > first) rate = (n - 1) / ((last - first) / 1000.0f);
  else                        rate = n / cfg.optWinS;
  // If drops stop, don't wait for the window to empty: the rate can't be
  // higher than ~1.5 drops per "time since the last drop". Continuous at
  // since = 1.5/rate, and inactive during normal drop-to-drop jitter.
  if (total && isFiniteF(o.since) && o.since > 0.0f) rate = fminf(rate, 1.5f / o.since);

  o.rate = rate;
  o.q    = rate * 60.0f / cfg.dropFactor;
  return o;
}


static WStats computeWeight(uint32_t now) {
  WStats w;
  memset(&w, 0, sizeof(w));
  w.gNow = NAN; w.slope = NAN; w.slopeSE = NAN; w.resid = NAN; w.std1 = NAN;
  w.ok  = hxSeq > 0 && (now - hxLastMs) < HX_TIMEOUT_MS && !hxStuck;
  w.cal = cfg.hxCal != 0;
  w.raw = hxLastRaw;

  xSemaphoreTake(massMtx, portMAX_DELAY);
  uint32_t total = massCount;
  uint32_t avail = minU32(total, MASS_RING);
  if (avail == 0) { xSemaphoreGive(massMtx); return w; }

  const MassS newest = massBuf[(total - 1) % MASS_RING];

  // Last ~1 s: current mass and short-term scatter (bump detector).
  uint32_t k = minU32(avail, 10);
  double s = 0, ss = 0;
  for (uint32_t i = 0; i < k; i++) {
    double g = massBuf[(total - 1 - i) % MASS_RING].g;
    s += g; ss += g * g;
  }
  w.gNow = (float)(s / k);
  w.std1 = (k > 1) ? (float)sqrt(fmax(0.0, (ss - s * s / k) / (k - 1))) : 0.0f;

  // Least-squares line over the window. Time in s relative to newest
  // sample, mass relative to newest value, to keep sums well-conditioned.
  uint32_t winMs = (uint32_t)(cfg.winS * 1000.0f);
  double n = 0, St = 0, Sy = 0, Stt = 0, Sty = 0;
  uint32_t oldestT = newest.t, used = 0;
  for (uint32_t i = 0; i < avail; i++) {
    const MassS &m = massBuf[(total - 1 - i) % MASS_RING];
    if (newest.t - m.t > winMs) break;
    double t = -(double)(newest.t - m.t) / 1000.0;
    double y = (double)m.g - newest.g;
    n += 1; St += t; Sy += y; Stt += t * t; Sty += t * y;
    oldestT = m.t; used++;
  }
  w.n    = (int)used;
  w.span = (newest.t - oldestT) / 1000.0f;

  if (used >= 3) {
    double Sxx = Stt - St * St / n;
    if (Sxx > 1e-9) {
      double b = (Sty - St * Sy / n) / Sxx;
      double a = (Sy - b * St) / n;
      double rss = 0;
      for (uint32_t i = 0; i < used; i++) {
        const MassS &m = massBuf[(total - 1 - i) % MASS_RING];
        double t = -(double)(newest.t - m.t) / 1000.0;
        double r = ((double)m.g - newest.g) - (a + b * t);
        rss += r * r;
      }
      double sr = sqrt(rss / (n - 2));
      w.slope   = (float)b;            // g/s (negative while draining)
      w.resid   = (float)sr;
      w.slopeSE = (float)(sr / sqrt(Sxx));
      w.regOk   = true;
    }
  }
  xSemaphoreGive(massMtx);

  w.qValid = w.ok && w.regOk && w.n >= 20 && w.span >= fminf(10.0f, cfg.winS * 0.5f);
  return w;
}

// =================================================================
// JSON output
// =================================================================
struct JBuf {
  char b[768];
  int  n;
  JBuf() : n(0) { b[0] = 0; }
  void put(const char *fmt, ...) {
    if (n >= (int)sizeof(b) - 1) return;
    va_list ap; va_start(ap, fmt);
    int r = vsnprintf(b + n, sizeof(b) - n, fmt, ap);
    va_end(ap);
    if (r > 0) n += r;
    if (n > (int)sizeof(b) - 1) n = sizeof(b) - 1;
  }
  void f(const char *k, float v, int dec) {
    if (isFiniteF(v)) put("\"%s\":%.*f,", k, dec, (double)v);
    else              put("\"%s\":null,", k);
  }
  void i(const char *k, long v)  { put("\"%s\":%ld,", k, v); }
  void u(const char *k, unsigned long v) { put("\"%s\":%lu,", k, v); }
  void end() { if (n > 0 && b[n - 1] == ',') n--; put("}"); }
};

static void printStatus(uint32_t now) {
  OStats o = computeOptical(now);
  WStats w = computeWeight(now);

  float wq   = w.qValid ? -w.slope * 60.0f / cfg.rho : NAN;   // mL/min
  float wqse = w.qValid ?  w.slopeSE * 60.0f / cfg.rho : NAN;
  bool  stable = w.ok && w.cal && isFiniteF(w.std1) && w.std1 < cfg.stabG &&
                 (!w.regOk || w.resid < 2.0f * cfg.stabG);
  float rem  = (w.ok && w.cal) ? (w.gNow - cfg.emptyG) / cfg.rho : NAN;

  JBuf j;
  j.put("{\"type\":\"s\",");
  j.u("t", now);
  // optical
  j.f("o_base", o.base, 1);
  j.f("o_noise", o.noise, 2);
  j.i("o_sat", o.sat ? 1 : 0);
  j.u("o_drops", o.drops);
  j.u("o_n", o.nWin);
  j.f("o_rate", o.rate, 3);          // drops/s
  j.f("o_q", o.q, 3);                // mL/min
  j.f("o_since", o.since, 2);        // s since last drop
  j.f("o_amp", o.amp, 1);            // mean |peak| of recent drops, ADC counts
  j.f("o_snr", o.noise > 0 ? o.amp / o.noise : NAN, 1);
  j.u("o_long", o.longEv);
  j.u("o_spk", o.spikes);
  // weight
  j.i("w_ok", w.ok ? 1 : 0);
  j.i("w_cal", w.cal ? 1 : 0);
  j.i("w_raw", (long)w.raw);
  j.f("w_g", w.gNow, 2);             // g (counts if uncalibrated)
  j.f("w_std1", w.std1, 3);          // g, last ~1 s
  j.f("w_q", wq, 3);                 // mL/min from mass slope
  j.f("w_qse", wqse, 3);             // 1-sigma standard error of w_q
  j.f("w_res", w.resid, 3);          // g, residual std around the fitted line
  j.f("w_span", w.span, 1);          // s of data in the regression window
  j.i("w_n", w.n);
  j.i("w_stable", stable ? 1 : 0);
  j.f("w_rem", rem, 1);              // mL above the "empty" reference
  j.end();
  Serial.println(j.b);
}

static void printConfig() {
  JBuf j;
  j.put("{\"type\":\"cfg\",");
  j.f("hxOffset", cfg.hxOffset, 1);
  j.f("hxScale", cfg.hxScale, 4);
  j.i("hxCal", cfg.hxCal);
  j.f("emptyG", cfg.emptyG, 2);
  j.f("df", cfg.dropFactor, 3);
  j.f("rho", cfg.rho, 4);
  j.f("k", cfg.kSigma, 2);
  j.f("mind", cfg.minDelta, 1);
  j.i("refr", cfg.refractoryMs);
  j.i("maxev", cfg.maxEventMs);
  j.f("win", cfg.winS, 1);
  j.f("owin", cfg.optWinS, 1);
  j.f("stab", cfg.stabG, 2);
  j.end();
  Serial.println(j.b);
}

static void msg(const char *text) { Serial.printf("{\"type\":\"msg\",\"text\":\"%s\"}\n", text); }
static void err(const char *text) { Serial.printf("{\"type\":\"err\",\"text\":\"%s\"}\n", text); }

// =================================================================
// Commands
// =================================================================
static bool averageRaw(int count, double &out) {
  uint32_t seq = hxSeq, t0 = millis();
  uint32_t timeout = (uint32_t)count * 200u + 1000u;
  double s = 0; int k = 0;
  while (k < count) {
    if (millis() - t0 > timeout) return false;
    if (hxSeq != seq) { seq = hxSeq; s += hxLastRaw; k++; }
    delay(5);
  }
  out = s / count;
  return true;
}

static void printHelp() {
  Serial.println("# DripGuard commands (newline-terminated):");
  Serial.println("#   help | cfg | reset            reset = zero drop counters");
  Serial.println("#   tare                          zero the scale (whatever hangs now = 0 g)");
  Serial.println("#   cal <grams>                   hang a known mass, then run this");
  Serial.println("#   empty [grams]                 set zero-fluid reference (default: current reading)");
  Serial.println("#   df <drops/mL>                 drop factor (default 20)");
  Serial.println("#   raw <n>                       stream optical ADC for Serial Plotter, every n-th sample; raw 0 = off");
  Serial.println("#   hxraw <0|1>                   print every HX711 sample");
  Serial.println("#   k <x> | mind <counts> | refr <ms> | maxev <ms>   drop detector tuning");
  Serial.println("#   win <s> | owin <s> | stab <g> | rho <g/mL>       estimator tuning");
  Serial.println("#   avg <1-32>                    optical moving average (17 kills mains hum; 1 = off, not saved)");
  Serial.println("#   disp L|BIG|line2|q|qo|qw|rem|cal|opt|wt   (sent by the dashboard)");
  Serial.println("#   defaults                      restore defaults (clears calibration!)");
}

static bool argF(const char *a, float lo, float hi, float &out) {
  if (!a) { err("missing argument"); return false; }
  char *end; float v = strtof(a, &end);
  if (end == a || v < lo || v > hi) { err("argument out of range"); return false; }
  out = v; return true;
}

static void copyField(char *dst, size_t cap, const char *src, size_t len) {
  size_t k = len < cap - 1 ? len : cap - 1;
  memcpy(dst, src, k);
  dst[k] = 0;
}

// disp level|BIG|line2|q_best|q_opt|q_wt|rem_ml|cal_df|opt_flag|wt_flag
static void parseDisp(const char *p) {
  const char *f[10]; size_t len[10]; int n = 0;
  while (n < 10) {
    const char *q = strchr(p, '|');
    f[n] = p; len[n] = q ? (size_t)(q - p) : strlen(p); n++;
    if (!q) break;
    p = q + 1;
  }
  if (n < 10) { err("disp needs 10 fields"); return; }
  copyField(hd.level, sizeof(hd.level), f[0], len[0]);
  copyField(hd.big,   sizeof(hd.big),   f[1], len[1]);
  copyField(hd.line2, sizeof(hd.line2), f[2], len[2]);
  copyField(hd.qbest, sizeof(hd.qbest), f[3], len[3]);
  copyField(hd.qo,    sizeof(hd.qo),    f[4], len[4]);
  copyField(hd.qw,    sizeof(hd.qw),    f[5], len[5]);
  copyField(hd.rem,   sizeof(hd.rem),   f[6], len[6]);
  copyField(hd.cal,   sizeof(hd.cal),   f[7], len[7]);
  hd.opt = len[8] ? f[8][0] : '?';
  hd.wt  = len[9] ? f[9][0] : '?';
  hd.rxMs = millis();
  if (hd.rxMs == 0) hd.rxMs = 1;
}

static void handleCommand(char *line) {
  if (!strncmp(line, "disp ", 5)) { parseDisp(line + 5); return; }   // before strtok: fields contain spaces
  char *cmd = strtok(line, " \t");
  if (!cmd) return;
  char *arg = strtok(NULL, " \t");
  float v;

  if (!strcmp(cmd, "help"))       { printHelp(); }
  else if (!strcmp(cmd, "cfg"))   { printConfig(); }
  else if (!strcmp(cmd, "reset")) { optResetReq = true; msg("drop counters reset"); }
  else if (!strcmp(cmd, "tare")) {
    double avg;
    msg("taring - keep the rig still (~2 s)");
    if (!averageRaw(20, avg)) { err("HX711 not responding"); return; }
    cfg.hxOffset = (float)avg; massClearReq = true; saveConfig();
    printConfig();
  }
  else if (!strcmp(cmd, "cal")) {
    if (!argF(arg, 1.0f, 5000.0f, v)) return;
    double avg;
    msg("calibrating - keep the rig still (~2 s)");
    if (!averageRaw(20, avg)) { err("HX711 not responding"); return; }
    double delta = avg - cfg.hxOffset;
    if (fabs(delta) < 1000.0) { err("change too small - did you tare first and hang the mass?"); return; }
    cfg.hxScale = (float)(delta / v); cfg.hxCal = 1; massClearReq = true; saveConfig();
    printConfig();
  }
  else if (!strcmp(cmd, "empty")) {
    if (arg) { if (!argF(arg, -5000.0f, 5000.0f, v)) return; cfg.emptyG = v; }
    else {
      if (!cfg.hxCal) { err("calibrate first"); return; }
      double avg;
      if (!averageRaw(20, avg)) { err("HX711 not responding"); return; }
      cfg.emptyG = (float)((avg - cfg.hxOffset) / cfg.hxScale);
    }
    saveConfig(); printConfig();
  }
  else if (!strcmp(cmd, "df"))    { if (argF(arg, 1, 100, v))      { cfg.dropFactor = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "rho"))   { if (argF(arg, 0.5f, 2.0f, v))  { cfg.rho = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "k"))     { if (argF(arg, 2, 50, v))       { cfg.kSigma = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "mind"))  { if (argF(arg, 1, 2000, v))     { cfg.minDelta = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "refr"))  { if (argF(arg, 5, 1000, v))     { cfg.refractoryMs = (uint16_t)v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "maxev")) { if (argF(arg, 20, 2000, v))    { cfg.maxEventMs = (uint16_t)v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "win"))   { if (argF(arg, 5, 120, v))      { cfg.winS = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "owin"))  { if (argF(arg, 5, 60, v))       { cfg.optWinS = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "stab"))  { if (argF(arg, 0.05f, 100, v))  { cfg.stabG = v; saveConfig(); printConfig(); } }
  else if (!strcmp(cmd, "raw"))   { if (argF(arg, 0, 1000, v))     { rawDecim = (uint16_t)v; } }
  else if (!strcmp(cmd, "avg"))   { if (argF(arg, 1, 32, v))       { avgN = (uint8_t)v; msg("optical moving average set"); } }
  else if (!strcmp(cmd, "hxraw")) { if (argF(arg, 0, 1, v))        { hxRawPrint = (v != 0); } }
  else if (!strcmp(cmd, "defaults")) { setDefaults(); massClearReq = true; saveConfig(); printConfig(); }
  else { err("unknown command - type help"); }
}


// =================================================================
// OLED display (GME12864-11 = 0.96" SSD1306, 128x64, I2C)
// =================================================================
#if USE_OLED
static Adafruit_SSD1306 oled(128, 64, &Wire, -1);
static bool     oledOk = false;
static uint8_t  page = 0;                 // 0 status, 1 agreement, 2 IR scope
static uint32_t blinkUntil = 0, lastDraw = 0, btnChangeMs = 0;
static bool     btnPrev = true;

static void oledText(int x, int y, int size, const char *t) {
  oled.setTextSize(size);
  oled.setCursor(x, y);
  oled.print(t);
}

static bool hostAlive(uint32_t now) { return hd.rxMs && (now - hd.rxMs) < HOST_TIMEOUT_MS; }

// Big word: size 3 (18 px/char) if it fits 7 chars, else size 2 (12 px/char).
static void bigWord(const char *w, int y) {
  size_t n = strlen(w);
  if (n <= 7) oledText(0, y, 3, w);
  else        oledText(0, y + 4, 2, w);
}

static void drawStatus(uint32_t now) {
  char line[32];
  oledText(0, 0, 1, "DRIPGUARD");
  bool host = hostAlive(now);
  if (!host) oledText(66, 0, 1, "SOLO");
  if ((int32_t)(blinkUntil - now) > 0) oled.fillCircle(123, 3, 3, SSD1306_WHITE);

  if (host) {
    bigWord(hd.big, 12);
    oledText(0, 40, 1, hd.line2);
    snprintf(line, sizeof(line), "OPT %s WT %s %smL",
             hd.opt == 'o' ? "ok" : "ERR", hd.wt == 'o' ? "ok" : (hd.wt == '~' ? "~~" : "ERR"), hd.rem);
  } else {
    // Standalone: only what the device knows by itself (no fusion verdicts).
    OStats o = computeOptical(now);
    WStats w = computeWeight(now);
    if (o.sat)                                         { bigWord("NO IR", 12);    oledText(0, 40, 1, "optical signal lost"); }
    else if (o.drops > 0 && isFiniteF(o.since) && o.since > 8.0f) {
      bigWord("NO DROPS", 12);
      snprintf(line, sizeof(line), "none for %.0f s", (double)o.since); oledText(0, 40, 1, line);
    } else if (o.drops == 0)                           { bigWord("READY", 12);    oledText(0, 40, 1, "waiting for drops"); }
    else {
      snprintf(line, sizeof(line), "%.1f", (double)o.q); bigWord(line, 12);
      oledText(0, 40, 1, "mL/min (drop count)");
    }
    if (!w.ok)          snprintf(line, sizeof(line), "OPT %s WT ERR", o.sat ? "ERR" : "ok");
    else if (!w.cal)    snprintf(line, sizeof(line), "OPT %s WT uncal", o.sat ? "ERR" : "ok");
    else                snprintf(line, sizeof(line), "OPT %s WT %.0fg", o.sat ? "ERR" : "ok", (double)w.gNow);
  }
  oled.drawFastHLine(0, 52, 128, SSD1306_WHITE);
  oledText(0, 56, 1, line);
}

static void drawAgreement(uint32_t now) {
  oledText(0, 0, 1, "FLOW mL/min");
  if (!hostAlive(now)) { oledText(0, 24, 1, "needs the dashboard"); return; }
  float qo = (float)atof(hd.qo), qw = (float)atof(hd.qw);
  bool hasO = hd.qo[0] != '-', hasW = hd.qw[0] != '-';
  float mx = fmaxf(0.1f, fmaxf(hasO ? qo : 0, hasW ? qw : 0)) * 1.15f;
  oledText(0, 16, 1, "OPT");
  if (hasO) oled.fillRect(24, 15, (int)(70 * fminf(qo / mx, 1.0f)), 9, SSD1306_WHITE);
  oledText(98, 16, 1, hd.qo);
  oledText(0, 32, 1, "WT");
  if (hasW) oled.fillRect(24, 31, (int)(70 * fminf(qw / mx, 1.0f)), 9, SSD1306_WHITE);
  oledText(98, 32, 1, hd.qw);
  char line[32];
  snprintf(line, sizeof(line), "cal %s gtt/mL", hd.cal);
  oledText(0, 52, 1, line);
}

static void drawScope(uint32_t now) {
  (void)now;
  uint16_t buf[SCOPE_N];
  uint16_t head = scopeHead;
  for (int i = 0; i < SCOPE_N; i++) buf[i] = scopeBuf[(head + i) % SCOPE_N];
  float base = scopeBase, thr = scopeThr;
  float lo = base - 1.5f * thr, hi = base + 1.5f * thr;
  for (int i = 0; i < SCOPE_N; i++) { lo = fminf(lo, buf[i]); hi = fmaxf(hi, buf[i]); }
  if (hi - lo < 4) { hi += 2; lo -= 2; }
  const int y0 = 10, y1 = 63;
  auto Y = [&](float v) { return y1 - (int)((v - lo) / (hi - lo) * (y1 - y0)); };
  char line[32];
  float snr;
  portENTER_CRITICAL(&optMux); snr = optNoise > 0 ? ampAvg / optNoise : 0; portEXIT_CRITICAL(&optMux);
  snprintf(line, sizeof(line), "IR %4.0f snr %.0f", (double)base, (double)snr);
  oledText(0, 0, 1, line);
  for (int x = 0; x < 128; x += 4) {            // dashed threshold lines
    oled.drawPixel(x, Y(base + thr), SSD1306_WHITE);
    oled.drawPixel(x, Y(base - thr), SSD1306_WHITE);
  }
  for (int i = 1; i < SCOPE_N; i++) oled.drawLine(i - 1, Y(buf[i - 1]), i, Y(buf[i]), SSD1306_WHITE);
}

static void oledInit() {
  Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
  Wire.setClock(400000);
  pinMode(PIN_BTN, INPUT_PULLUP);
  // begin() only allocates the buffer and never checks the bus, so probe for an ACK first.
  uint8_t addr = 0;
  const uint8_t cand[2] = {0x3C, 0x3D};
  for (int i = 0; i < 2; i++) {
    Wire.beginTransmission(cand[i]);
    if (Wire.endTransmission() == 0) { addr = cand[i]; break; }
  }
  if (!addr) { msg("OLED not found at 0x3C/0x3D - check SDA=21 SCL=22 VCC=3.3V; continuing without display"); return; }
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, addr, true, false);
  if (!oledOk) { msg("OLED buffer allocation failed"); return; }
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oledText(0, 0, 2, "DripGuard");
  oledText(0, 24, 1, "water-only prototype");
  oledText(0, 40, 1, "starting...");
  oled.display();
}

static void oledService(uint32_t now) {
  // BOOT button (active low), 50 ms debounce, cycles pages
  bool b = digitalRead(PIN_BTN) == HIGH;
  if (b != btnPrev && now - btnChangeMs > 50) {
    btnChangeMs = now; btnPrev = b;
    if (!b) page = (uint8_t)((page + 1) % 3);
  }
  if (!oledOk || now - lastDraw < 200) return;
  lastDraw = now;

  bool host = hostAlive(now);
  bool alarm  = host && !strcmp(hd.level, "alarm");
  bool sensor = host && !strcmp(hd.level, "sensor");
  // Alarms and sensor faults take over every page except the scope (used during bring-up).
  uint8_t show = ((alarm || sensor) && page != 2) ? 0 : page;

  oled.clearDisplay();
  if (show == 0)      drawStatus(now);
  else if (show == 1) drawAgreement(now);
  else                drawScope(now);
  if (sensor && show == 0) oled.drawRect(0, 9, 128, 42, SSD1306_WHITE);
  oled.invertDisplay(alarm && show == 0 && ((now / 500) % 2 == 0));   // flashing alarm
  oled.display();                                                     // ~25 ms over I2C
}
#endif

// =================================================================
// Setup / loop
// =================================================================
static char     lineBuf[200];
static uint8_t  lineLen = 0;
static uint32_t lastStatus = 0;
static uint32_t lastHxSeqPrinted = 0;

void setup() {
  Serial.setTxBufferSize(8192);
  Serial.begin(921600);
  delay(200);

  prefs.begin("dripguard", false);
  loadConfig();

  analogReadResolution(12);
  analogSetPinAttenuation(PIN_OPT_DROP, ADC_11db);    // ~0.15-3.1 V usable
  analogSetPinAttenuation(PIN_OPT_BUBBLE, ADC_11db);

  pinMode(PIN_HX_SCK, OUTPUT);
  digitalWrite(PIN_HX_SCK, LOW);
  pinMode(PIN_HX_DOUT, INPUT_PULLUP);   // missing HX711 reads "not ready", not garbage

  massMtx = xSemaphoreCreateMutex();
  dropQ   = xQueueCreate(32, sizeof(DropEv));
  rawQ    = xQueueCreate(512, sizeof(RawS));

  xTaskCreatePinnedToCore(opticalTask, "opt", 4096, NULL, 5, NULL, 1);
  xTaskCreatePinnedToCore(hxTask,      "hx",  4096, NULL, 4, NULL, 0);

#if USE_OLED
  oledInit();
#endif
  Serial.println("{\"type\":\"boot\",\"fw\":\"dripguard-0.2\"}");
  printConfig();
  Serial.println("# type 'help' for commands");
  lastStatus = millis();
}

void loop() {
  // Serial commands
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) { lineBuf[lineLen] = 0; handleCommand(lineBuf); lineLen = 0; }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }

  // Raw optical stream (Arduino Serial Plotter format)
  RawS r;
  int budget = 64;
  while (budget-- > 0 && xQueueReceive(rawQ, &r, 0) == pdTRUE) {
    if (rawDecim) {
      Serial.printf("raw:%u,base:%u,hi:%u,lo:%d,det:%u\n",
                    r.x, r.base, r.hi, r.lo, r.ev ? (unsigned)(r.hi + (r.hi - r.base)) : (unsigned)r.base);
    }
  }

  // Drop events (suppressed in raw mode so the plotter stays clean)
  DropEv e;
  while (xQueueReceive(dropQ, &e, 0) == pdTRUE) {
#if USE_OLED
    blinkUntil = millis() + 120;
#endif
    if (!rawDecim) {
      Serial.printf("{\"type\":\"drop\",\"t\":%lu,\"amp\":%.1f,\"w\":%u}\n",
                    (unsigned long)e.t, (double)e.amp, (unsigned)e.width);
    }
  }

  uint32_t now = millis();

  if (hxRawPrint && !rawDecim && hxSeq != lastHxSeqPrinted) {
    lastHxSeqPrinted = hxSeq;
    float g = ((float)hxLastRaw - cfg.hxOffset) / cfg.hxScale;
    Serial.printf("{\"type\":\"hx\",\"t\":%lu,\"raw\":%ld,\"g\":%.2f}\n",
                  (unsigned long)hxLastMs, (long)hxLastRaw, (double)g);
  }

  if (now - lastStatus >= STATUS_PERIOD_MS) {
    lastStatus += STATUS_PERIOD_MS;
    if (now - lastStatus > STATUS_PERIOD_MS) lastStatus = now;  // don't burst after a stall
    if (!rawDecim) printStatus(now);
  }

#if USE_OLED
  oledService(millis());
#endif

  delay(1);
}
