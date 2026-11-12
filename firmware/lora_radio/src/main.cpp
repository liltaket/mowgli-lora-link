#include <Arduino.h>
#include <RadioLib.h>
#include <esp_system.h>

constexpr uint8_t SCK_PIN = 7, MISO_PIN = 8, MOSI_PIN = 9, NSS_PIN = 41, DIO1_PIN = 39,
                  RST_PIN = 42, BUSY_PIN = 40;
constexpr float FREQ = 868.3F, BW = 500.0F;
constexpr uint8_t SF = 5, CR = 5, V = 1;
#ifndef MOWGLI_LORA_TX_POWER_DBM
#define MOWGLI_LORA_TX_POWER_DBM 10
#endif

constexpr int8_t POWER = MOWGLI_LORA_TX_POWER_DBM;
static_assert(POWER >= -9 && POWER <= 22,
              "MOWGLI_LORA_TX_POWER_DBM must be within the SX1262 -9 to +22 dBm range");
constexpr uint32_t TX_WATCHDOG_MS = 2000;
// Keep a wedged SX1262 from holding the USB modem's Arduino loop hostage.
constexpr uint32_t RADIO_BUSY_PRECHECK_MS = 150;
constexpr uint32_t RADIO_SPI_TIMEOUT_MS = 75;
constexpr uint32_t RADIO_RESET_PULSE_MS = 10;
constexpr uint32_t RADIO_RETRY_BACKOFF_MS = 250;
constexpr uint8_t RADIO_INIT_ATTEMPTS = 3;
static_assert(RADIO_INIT_ATTEMPTS > 0, "radio initialisation needs at least one attempt");
static_assert(RADIO_SPI_TIMEOUT_MS > 0 && RADIO_SPI_TIMEOUT_MS <= RADIO_BUSY_PRECHECK_MS,
              "SPI timeout must be a finite boot-time bound");
constexpr size_t UH = 16, UM = 256, UD = 274, UE = 278, AM = 200, AD = 218, QD = 6;
constexpr uint8_t HELLO = 1, SEND = 2, GET_STATUS = 3, GET_DIAGNOSTICS = 4, INFO = 0x81,
                  TXOK = 0x82, STAT = 0x83, DIAGNOSTICS = 0x84, RX_EVENT = 0x90, ERR = 0xff;
constexpr uint16_t E_UNSUPPORTED = 1, E_BAD = 2, E_NOTREADY = 3, E_WRONG = 4, E_BUSY = 5,
                   E_STALE = 6, E_REUSED = 7, E_RADIO = 8;
// Keep the Module visible so its per-transfer BUSY timeout can be bounded
// before RadioLib probes the SX1262.
Module radioModule(NSS_PIN, DIO1_PIN, RST_PIN, BUSY_PIN);
SX1262 radio(&radioModule);
enum RS : uint8_t
{
  DOWN = 0,
  RECV = 1,
  TRANSMITTING = 2
};
volatile bool irq = false;
volatile RS rs = DOWN;
// Published only after configuration and the first receive transition succeed.
volatile bool ready = false;
struct RadioInitResult
{
  bool ready;
  uint32_t errors;
};
QueueHandle_t radioInitResults = nullptr;
struct C
{
  uint32_t tx = 0, rx = 0, rxBad = 0, usbBad = 0, radio = 0, drops = 0;
  // End-to-end diagnostic counters.  They deliberately count stages rather
  // than inferred outcomes, so a host can identify where an RX event stopped.
  uint32_t usbFrames = 0, usbAccepted = 0, sendAccepted = 0, txStarted = 0;
  uint32_t rxEventQueued = 0, rxEventWritten = 0;
} c;
struct F
{
  uint8_t b[UD];
  size_t n;
  bool event;
};
F q[QD];
size_t qh = 0, qt = 0, qn = 0;
uint8_t in[UE];
size_t inN = 0;
bool over = false;
uint32_t boot = 1, airNext = 1, session = 0, lastSeq = 0, txSession = 0, txSeq = 0, txAir = 0,
         txStart = 0;
uint32_t txStartedMillis = 0;
bool active = false, pending = false, haveLast = false;
bool protocolSelfTestPassed = false;
uint8_t lastReq[UD], lastResp[UD];
uint8_t txPacket[AD];
size_t lastReqN = 0, lastRespN = 0;
void isr()
{
  irq = true;
}
bool waitForBusyLow(uint32_t timeoutMs)
{
  const uint32_t started = millis();
  while (digitalRead(BUSY_PIN) != LOW)
  {
    if (millis() - started >= timeoutMs)
      return false;
    vTaskDelay(pdMS_TO_TICKS(5));
  }
  return true;
}
void resetRadioHardware()
{
  pinMode(NSS_PIN, OUTPUT);
  digitalWrite(NSS_PIN, HIGH);
  pinMode(RST_PIN, OUTPUT);
  digitalWrite(RST_PIN, LOW);
  vTaskDelay(pdMS_TO_TICKS(RADIO_RESET_PULSE_MS));
  digitalWrite(RST_PIN, HIGH);
  pinMode(BUSY_PIN, INPUT);
}
bool startReceiveAfterInit()
{
  irq = false;
  const int st = radio.startReceive();
  return st == RADIOLIB_ERR_NONE;
}
void radioInitTask(void*)
{
  RadioInitResult result{false, 0};
  for (uint8_t attempt = 0; attempt < RADIO_INIT_ATTEMPTS; ++attempt)
  {
    resetRadioHardware();
    if (!waitForBusyLow(RADIO_BUSY_PRECHECK_MS))
    {
      ++result.errors;
    }
    else
    {
      // RadioLib uses this value for every BUSY wait during begin().
      radioModule.spiConfig.timeout = RADIO_SPI_TIMEOUT_MS;
      const int st = radio.begin(FREQ, BW, SF, CR, RADIOLIB_SX126X_SYNC_WORD_PRIVATE, POWER, 12,
                                 1.8, false);
      if (st == RADIOLIB_ERR_NONE && radio.setCRC(true) == RADIOLIB_ERR_NONE &&
          radio.setDio2AsRfSwitch(true) == RADIOLIB_ERR_NONE)
      {
        radio.setDio1Action(isr);
        if (startReceiveAfterInit())
        {
          result.ready = true;
          xQueueSend(radioInitResults, &result, 0);
          vTaskDelete(nullptr);
          return;
        }
        ++result.errors;
      }
      else
      {
        ++result.errors;
      }
    }
    if (attempt + 1 < RADIO_INIT_ATTEMPTS)
      vTaskDelay(pdMS_TO_TICKS(RADIO_RETRY_BACKOFF_MS));
  }
  // USB remains available and no recovery path transmits autonomously.
  xQueueSend(radioInitResults, &result, 0);
  vTaskDelete(nullptr);
}
void publishRadioInitResult()
{
  if (!radioInitResults)
    return;
  RadioInitResult result{};
  if (xQueueReceive(radioInitResults, &result, 0) != pdPASS)
    return;
  c.radio += result.errors;
  ready = result.ready;
  rs = result.ready ? RECV : DOWN;
  vQueueDelete(radioInitResults);
  radioInitResults = nullptr;
}
uint16_t crc(const uint8_t* d, size_t n)
{
  uint16_t x = 0xffff;
  while (n--)
  {
    x ^= uint16_t(*d++) << 8;
    for (uint8_t i = 0; i < 8; i++)
      x = (x & 0x8000) ? uint16_t((x << 1) ^ 0x1021) : uint16_t(x << 1);
  }
  return x;
}
void w16(uint8_t* p, uint16_t x)
{
  p[0] = x >> 8;
  p[1] = x;
}
void w32(uint8_t* p, uint32_t x)
{
  p[0] = x >> 24;
  p[1] = x >> 16;
  p[2] = x >> 8;
  p[3] = x;
}
uint16_t r16(const uint8_t* p)
{
  return uint16_t(p[0]) << 8 | p[1];
}
uint32_t r32(const uint8_t* p)
{
  return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
}
size_t enc(const uint8_t* a, size_t n, uint8_t* o)
{
  size_t ri = 0, wi = 1, ci = 0;
  uint8_t code = 1;
  while (ri < n)
  {
    if (!a[ri])
    {
      o[ci] = code;
      code = 1;
      ci = wi++;
      ri++;
    }
    else
    {
      o[wi++] = a[ri++];
      if (++code == 0xff)
      {
        o[ci] = code;
        code = 1;
        ci = wi++;
      }
    }
  }
  o[ci] = code;
  return wi;
}
bool dec(const uint8_t* a, size_t n, uint8_t* o, size_t* on)
{
  size_t ri = 0, wi = 0;
  while (ri < n)
  {
    uint8_t code = a[ri++];
    if (!code || ri + code - 1 > n)
      return false;
    for (uint8_t i = 1; i < code; i++)
    {
      if (wi >= UD)
        return false;
      o[wi++] = a[ri++];
    }
    if (code != 0xff && ri < n)
    {
      if (wi >= UD)
        return false;
      o[wi++] = 0;
    }
  }
  *on = wi;
  return true;
}
bool protocolSelfTest()
{
  static const uint8_t kCrcVector[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  static const uint8_t kUsbVector[] = {
      'M', 'U', 1, 0x03, 0, 0, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0xec, 0x59};
  if (crc(kCrcVector, sizeof(kCrcVector)) != 0x29b1 ||
      crc(kUsbVector, sizeof(kUsbVector) - 2) != r16(kUsbVector + sizeof(kUsbVector) - 2))
    return false;
  uint8_t encoded[UE], decoded[UD];
  const size_t encodedLength = enc(kUsbVector, sizeof(kUsbVector), encoded);
  size_t decodedLength = 0;
  return dec(encoded, encodedLength, decoded, &decodedLength) &&
         decodedLength == sizeof(kUsbVector) &&
         memcmp(decoded, kUsbVector, sizeof(kUsbVector)) == 0;
}
bool putQ(const uint8_t* b, size_t n, bool event)
{
  if (qn == QD)
  {
    if (event)
    {
      c.drops++;
      return false;
    }
    // Never let a correlated response disappear behind unsolicited RX events.
    size_t eventOffset = QD;
    for (size_t offset = 0; offset < qn; ++offset)
    {
      if (q[(qh + offset) % QD].event)
      {
        eventOffset = offset;
        break;
      }
    }
    if (eventOffset == QD)
      return false;  // Queue holds only responses; preserve their ordering.
    for (size_t offset = eventOffset; offset + 1 < qn; ++offset)
      q[(qh + offset) % QD] = q[(qh + offset + 1) % QD];
    qt = (qt + QD - 1) % QD;
    --qn;
    ++c.drops;
  }
  memcpy(q[qt].b, b, n);
  q[qt].n = n;
  q[qt].event = event;
  qt = (qt + 1) % QD;
  qn++;
  if (event)
    ++c.rxEventQueued;
  return true;
}
size_t frame(uint8_t type, uint32_t ses, uint32_t seq, const uint8_t* p, size_t pn, uint8_t* out)
{
  out[0] = 'M';
  out[1] = 'U';
  out[2] = V;
  out[3] = type;
  out[4] = out[5] = 0;
  w32(out + 6, ses);
  w32(out + 10, seq);
  w16(out + 14, pn);
  if (pn)
    memcpy(out + 16, p, pn);
  w16(out + 16 + pn, crc(out, 16 + pn));
  return 18 + pn;
}
void flush()
{
  if (!qn)
    return;
  uint8_t e[UE];
  size_t n = enc(q[qh].b, q[qh].n, e);
  if (Serial.availableForWrite() < int(n + 1))
    return;
  e[n] = 0;
  const bool wasEvent = q[qh].event;
  const size_t written = Serial.write(e, n + 1);
  if (written == n + 1 && wasEvent)
    ++c.rxEventWritten;
  else if (written != n + 1)
  {
    if (wasEvent)
      ++c.drops;
    else
      ++c.usbBad;
  }
  qh = (qh + 1) % QD;
  qn--;
}
void reply(uint8_t type, uint32_t seq, const uint8_t* p, size_t n, bool cache = true)
{
  uint8_t b[UD];
  size_t z = frame(type, session, seq, p, n, b);
  if (cache)
  {
    memcpy(lastResp, b, z);
    lastRespN = z;
    haveLast = true;
  }
  putQ(b, z, false);
}
void error(uint32_t seq, uint16_t code, int16_t detail, uint8_t type, bool cache = true)
{
  uint8_t p[5];
  w16(p, code);
  w16(p + 2, uint16_t(detail));
  p[4] = type;
  reply(ERR, seq, p, 5, cache);
}
void errorFor(uint32_t ses, uint32_t seq, uint16_t code, int16_t detail, uint8_t type)
{
  uint8_t p[5], b[UD];
  w16(p, code);
  w16(p + 2, uint16_t(detail));
  p[4] = type;
  const size_t frameLength = frame(ERR, ses, seq, p, 5, b);
  putQ(b, frameLength, false);
}
void saveReq(const uint8_t* b, size_t n)
{
  memcpy(lastReq, b, n);
  lastReqN = n;
}
void resetSession(uint32_t s)
{
  session = s;
  active = true;
  lastSeq = 0;
  haveLast = false;
  lastReqN = lastRespN = 0;
  pending = false;
}
void listen()
{
  if (!ready)
    return;
  irq = false;
  int st = radio.startReceive();
  if (st == RADIOLIB_ERR_NONE)
    rs = RECV;
  else
  {
    rs = DOWN;
    c.radio++;
  }
}
void received(const uint8_t* b, size_t n, float rssi, float snr)
{
  if (n < 18 || b[0] != 'M' || b[1] != 'L' || b[2] != V || b[3] != 1 || b[4] || b[5])
  {
    c.rxBad++;
    return;
  }
  uint16_t pn = r16(b + 14);
  if (!pn || pn > AM || n != 18 + pn || r16(b + n - 2) != crc(b, n - 2))
  {
    c.rxBad++;
    return;
  }
  c.rx++;
  if (!active)
    return;
  uint8_t p[UM];
  w32(p, r32(b + 6));
  w32(p + 4, r32(b + 10));
  w16(p + 8, uint16_t(lroundf(rssi * 100)));
  w16(p + 10, uint16_t(lroundf(snr * 100)));
  w32(p + 12, millis());
  memcpy(p + 16, b + 16, pn);
  uint8_t f[UD];
  const size_t frameLength = frame(RX_EVENT, session, 0, p, 16 + pn, f);
  putQ(f, frameLength, true);
}
void radioTask()
{
  if (!irq || !ready)
    return;
  irq = false;
  if (rs == TRANSMITTING)
  {
    int st = radio.finishTransmit();
    rs = DOWN;
    if (st == RADIOLIB_ERR_NONE)
    {
      c.tx++;
      if (active && session == txSession)
      {
        uint8_t p[12];
        w32(p, boot);
        w32(p + 4, txAir);
        w32(p + 8, micros() - txStart);
        pending = false;
        reply(TXOK, txSeq, p, 12);
      }
    }
    else
    {
      c.radio++;
      if (active && session == txSession)
      {
        pending = false;
        error(txSeq, E_RADIO, st, SEND);
      }
    }
    listen();
    return;
  }
  uint8_t b[AD] = {};
  size_t n = radio.getPacketLength();
  int st = radio.readData(b, sizeof(b));
  float r = radio.getRSSI(), s = radio.getSNR();
  if (st == RADIOLIB_ERR_NONE)
    received(b, n, r, s);
  else
    c.radio++;
  listen();
}
void txWatchdog()
{
  if (rs != TRANSMITTING || millis() - txStartedMillis < TX_WATCHDOG_MS)
    return;
  ++c.radio;
  radio.standby();
  rs = DOWN;
  if (active && session == txSession)
  {
    pending = false;
    error(txSeq, E_RADIO, RADIOLIB_ERR_UNKNOWN, SEND);
  }
  listen();
}
void info(uint32_t seq)
{
  uint8_t p[22] = {};
  uint64_t m = ESP.getEfuseMac();
  for (uint8_t i = 0; i < 6; i++)
    p[i] = m >> (8 * i);
  w32(p + 6, boot);
  w32(p + 10, 15);
  w16(p + 14, AM);
  p[16] = ready;
  p[17] = 1;
  w32(p + 18, millis());
  reply(INFO, seq, p, 22);
}
void status(uint32_t seq)
{
  uint8_t p[36] = {};
  w32(p, boot);
  w32(p + 4, millis());
  p[8] = uint8_t(rs);
  w32(p + 12, c.tx);
  w32(p + 16, c.rx);
  w32(p + 20, c.rxBad);
  w32(p + 24, c.usbBad);
  w32(p + 28, c.radio);
  w32(p + 32, c.drops);
  reply(STAT, seq, p, 36);
}
void diagnostics(uint32_t seq)
{
  // Versioned by message type.  Keep LINK_STATUS at its published 36-byte
  // shape so existing hosts remain compatible.
  uint8_t p[40] = {};
  w32(p, c.usbFrames);
  w32(p + 4, c.usbAccepted);
  w32(p + 8, c.sendAccepted);
  w32(p + 12, c.txStarted);
  w32(p + 16, c.tx);
  w32(p + 20, c.rx);
  w32(p + 24, c.rxEventQueued);
  w32(p + 28, c.rxEventWritten);
  w32(p + 32, c.drops);
  w32(p + 36, qn);
  reply(DIAGNOSTICS, seq, p, sizeof(p));
}
void sendAir(const uint8_t* p, size_t pn, uint32_t seq)
{
  if (!ready)
  {
    error(seq, E_NOTREADY, 0, SEND);
    return;
  }
  if (rs == TRANSMITTING || pending)
  {
    error(seq, E_BUSY, 0, SEND);
    return;
  }
  txPacket[0] = 'M';
  txPacket[1] = 'L';
  txPacket[2] = V;
  txPacket[3] = 1;
  txPacket[4] = txPacket[5] = 0;
  w32(txPacket + 6, boot);
  uint32_t a = airNext++;
  if (!a)
    a = airNext++;
  w32(txPacket + 10, a);
  w16(txPacket + 14, pn);
  memcpy(txPacket + 16, p, pn);
  w16(txPacket + 16 + pn, crc(txPacket, 16 + pn));
  // Establish state first: a fast DIO1 completion must not be cleared after TX
  // starts.
  irq = false;
  rs = TRANSMITTING;
  pending = true;
  txSession = session;
  txSeq = seq;
  txAir = a;
  txStart = micros();
  txStartedMillis = millis();
  int st = radio.startTransmit(txPacket, 18 + pn);
  if (st != RADIOLIB_ERR_NONE)
  {
    c.radio++;
    rs = DOWN;
    pending = false;
    error(seq, E_RADIO, st, SEND);
    listen();
    return;
  }
  ++c.txStarted;
}
void handle(const uint8_t* b, size_t n)
{
  if (n < 18 || b[0] != 'M' || b[1] != 'U' || b[2] != V || b[4] || b[5] || r16(b + 14) + 18 != n ||
      r16(b + n - 2) != crc(b, n - 2))
  {
    c.usbBad++;
    return;
  }
  uint8_t t = b[3];
  uint32_t s = r32(b + 6), seq = r32(b + 10);
  uint16_t pn = r16(b + 14);
  const uint8_t* p = b + 16;
  if (!s || !seq)
  {
    c.usbBad++;
    return;
  }
  ++c.usbFrames;
  if (t == HELLO)
  {
    if (pn)
    {
      // Invalid HELLO is correlated but never changes the active session.
      errorFor(s, seq, E_BAD, 0, t);
      return;
    }
    if (active && s == session && pending)
    {
      // A same-session HELLO cannot erase a pending send's retry cache.
      error(seq, E_BUSY, 0, t, false);
      return;
    }
    if (!active || s != session)
      resetSession(s);
    else if (seq < lastSeq)
    {
      error(seq, E_STALE, 0, t, false);
      return;
    }
    else if (seq == lastSeq)
    {
      if (haveLast && n == lastReqN && !memcmp(b, lastReq, n))
        putQ(lastResp, lastRespN, false);
      else
        error(seq, E_REUSED, 0, t, false);
      return;
    }
    saveReq(b, n);
    lastSeq = seq;
    ++c.usbAccepted;
    info(seq);
    return;
  }
  if (!active || s != session)
  {
    errorFor(s, seq, E_WRONG, 0, t);
    return;
  }
  if (seq < lastSeq)
  {
    error(seq, E_STALE, 0, t, false);
    return;
  }
  if (seq == lastSeq)
  {
    if (pending)
    {
      error(seq, E_BUSY, 0, t, false);
      return;
    }
    if (haveLast && n == lastReqN && !memcmp(b, lastReq, n))
      putQ(lastResp, lastRespN, false);
    else
      error(seq, E_REUSED, 0, t, false);
    return;
  }
  if (pending)
  {
    error(seq, E_BUSY, 0, t, false);
    return;
  }
  saveReq(b, n);
  lastSeq = seq;
  haveLast = false;
  if (t == SEND)
  {
    if (!pn || pn > AM)
      error(seq, E_BAD, 0, t);
    else
    {
      ++c.usbAccepted;
      ++c.sendAccepted;
      sendAir(p, pn, seq);
    }
  }
  else if (t == GET_STATUS)
  {
    if (pn)
      error(seq, E_BAD, 0, t);
    else
    {
      ++c.usbAccepted;
      status(seq);
    }
  }
  else if (t == GET_DIAGNOSTICS)
  {
    if (pn)
      error(seq, E_BAD, 0, t);
    else
    {
      ++c.usbAccepted;
      diagnostics(seq);
    }
  }
  else
    error(seq, E_UNSUPPORTED, 0, t);
}
void usbTask()
{
  while (Serial.available())
  {
    int x = Serial.read();
    if (x < 0)
      break;
    uint8_t z = uint8_t(x);
    if (!z)
    {
      if (!over && inN)
      {
        uint8_t b[UD];
        size_t n;
        if (dec(in, inN, b, &n))
          handle(b, n);
        else
          c.usbBad++;
      }
      inN = 0;
      over = false;
      continue;
    }
    if (over)
      continue;
    if (inN == sizeof(in))
    {
      over = true;
      c.usbBad++;
    }
    else
      in[inN++] = z;
  }
}
void setup()
{
  Serial.begin(115200);
  uint32_t d = millis() + 2000;
  while (!Serial && millis() < d)
    delay(10);
  boot = esp_random();
  if (!boot)
    boot = 1;
  airNext = esp_random();
  if (!airNext)
    airNext = 1;
  protocolSelfTestPassed = protocolSelfTest();
  if (!protocolSelfTestPassed)
  {
    ++c.radio;
    return;
  }
#if defined(MOWGLI_LORA_USB_RECOVERY)
  // Deliberately keep the RF side untouched. This image exists only to regain
  // deterministic USB access when live hardware cannot be power-cycled.
  ++c.radio;
  return;
#endif
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, NSS_PIN);
  // Never call radio.begin() from the USB-facing Arduino loop.  A BUSY-high
  // radio is retried in a separate bounded task while HELLO/INFO continue.
  // Arduino's loop task is on core 1 for this ESP32-S3 target; use core 0 so
  // an uncooperative radio probe cannot consume the USB protocol's core.
  radioInitResults = xQueueCreate(1, sizeof(RadioInitResult));
  if (!radioInitResults ||
      xTaskCreatePinnedToCore(radioInitTask, "radio-init", 4096, nullptr, 0, nullptr, 0) != pdPASS)
  {
    if (radioInitResults)
    {
      vQueueDelete(radioInitResults);
      radioInitResults = nullptr;
    }
    c.radio++;
  }
}
void loop()
{
  usbTask();
  publishRadioInitResult();
  radioTask();
  txWatchdog();
  flush();
}
