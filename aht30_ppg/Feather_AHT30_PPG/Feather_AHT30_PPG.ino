// Feather 32u4 Bluefruit LE: ATmega32u4, 8 MHz, 3.3 V logic.
// Shared I2C: SDA=2, SCL=3. USB Serial; BLE is not enabled by this sketch.
#if !defined(__AVR_ATmega32U4__) || F_CPU != 8000000UL
#error "Select Adafruit Feather 32u4 (8 MHz), not UNO or ESP32."
#endif
#include <Arduino.h>
#include <Wire.h>
#include <stdio.h>

const uint8_t PPG_ADDRESS = 0x57;
const uint8_t RED_LED_CODE = 0x08; // ~1.6 mA nominal peak
const uint8_t IR_LED_CODE = 0x10; // ~3.2 mA nominal peak; fix during each trial
const uint32_t PPG_PERIOD_US = 20000;
uint32_t lastMicros = 0, ppgSeq = 0, segment = 0;
uint64_t highMicros = 0, sampleAnchor = 0, lastService = 0, nextMeta = 0;
bool ppgReady = false;
uint64_t nextPpgRetry = 0;

uint64_t clockUs() {
  uint32_t now = micros();
  if (now < lastMicros) highMicros += (1ULL << 32);
  lastMicros = now;
  return highMicros + now;
}

void printTime(uint64_t value) {
  char digits[21];
  uint8_t n = 0;
  do { digits[n++] = '0' + value % 10; value /= 10; } while (value);
  while (n) Serial.write(digits[--n]);
}

void event(const char *name, unsigned long value = 0) {
  Serial.print(F("E,"));
  printTime(clockUs());
  Serial.print(',');
  Serial.print(name);
  Serial.print(',');
  Serial.println(value);
}

bool writeReg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(PPG_ADDRESS); Wire.write(reg); Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool readBytes(uint8_t reg, uint8_t *dest, uint8_t count) {
  Wire.beginTransmission(PPG_ADDRESS); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(PPG_ADDRESS, count) != count) {
    while (Wire.available()) Wire.read();
    return false;
  }
  for (uint8_t i=0; i<count; i++) dest[i] = Wire.read();
  return true;
}

bool clearFifo() {
  // Reading a complete sample advances the read pointer and clears overflow.
  uint8_t discarded[6];
  if (!readBytes(0x07, discarded, 6)) return false;
  return writeReg(0x04, 0) && writeReg(0x05, 0) && writeReg(0x06, 0);
}

void disablePPG(const char *reason) {
  ppgReady = false;
  nextPpgRetry = clockUs() + 2000000ULL;
  writeReg(0x09, 0x80); // best-effort shutdown; cannot fix electrical faults
  event(reason);
}

void metadata() {
  Serial.print(F("#META {\"protocol\":2,\"aht_hz\":1,\"ppg_hz\":50,\"ppg_bits\":18,\"adc_sample_hz\":200,\"fifo_average\":4,\"led_code\":"));
  Serial.print(IR_LED_CODE);
  Serial.print(F(",\"red_led_code\":"));
  Serial.print(RED_LED_CODE);
  Serial.print(F(",\"ppg_ready\":"));
  Serial.print(ppgReady ? F("true") : F("false"));
  Serial.println('}');
}

bool setupPPG() {
  uint8_t id;
  if (!readBytes(0xFF, &id, 1) || id != 0x15) { event("PPG_NOT_FOUND"); return false; }
  if (!writeReg(0x09, 0x40)) { event("PPG_RESET_WRITE_FAILED"); return false; }
  bool resetDone = false;
  for (uint8_t i=0; i<100; i++) {
    uint8_t mode;
    if (!readBytes(0x09, &mode, 1)) break;
    if (!(mode & 0x40)) { resetDone = true; break; }
    delay(1);
  }
  if (!resetDone) { event("PPG_RESET_TIMEOUT"); return false; }
  // FIFO average4, rollover off; ADC range4096nA, ADC rate200Hz, output50Hz, pulse411us.
  if (!(writeReg(0x02, 0) && writeReg(0x03, 0) && writeReg(0x08, 0x40) &&
        writeReg(0x0A, 0x2B) && writeReg(0x0C, RED_LED_CODE) &&
        writeReg(0x0D, IR_LED_CODE) && clearFifo())) {
    event("PPG_CONFIGURATION_FAILED"); return false;
  }
  uint8_t settings;
  if (!readBytes(0x0A, &settings, 1) || settings != 0x2B) {
    event("PPG_CONFIGURATION_READBACK_FAILED"); return false;
  }
  if (!readBytes(0x08, &settings, 1) || settings != 0x40) {
    event("PPG_AVERAGING_READBACK_FAILED"); return false;
  }
  sampleAnchor = clockUs();
  if (!writeReg(0x09, 0x03)) { event("PPG_START_FAILED"); return false; }
  event("PPG_STARTED");
  return true;
}

// AHT30: 7-bit address 0x38 (datasheet write/read bytes 0x70/0x71).
// Trigger and collect in separate loop passes: never delay 80 ms during PPG.
const uint8_t AHT_ADDRESS = 0x38;
uint64_t nextAht = 0, ahtStarted = 0;
bool ahtPending = false;
uint8_t crc8(const uint8_t *data, uint8_t length) {
  uint8_t crc = 0xFF;
  for (uint8_t i=0; i<length; i++) {
    crc ^= data[i];
    for (uint8_t bit=0; bit<8; bit++)
      crc = (crc & 0x80) ? (uint8_t)((crc << 1)^0x31) : (uint8_t)(crc << 1);
  }
  return crc;
}
void serviceAht() {
  uint64_t now = clockUs();
  if (!ahtPending) {
    if (now < nextAht) return;
    nextAht = now + 1000000ULL; // 1 Hz trigger rate
    Wire.beginTransmission(AHT_ADDRESS);
    Wire.write(0xAC); Wire.write(0x33); Wire.write(0x00);
    if (Wire.endTransmission() != 0) { event("AHT_NOT_FOUND_OR_WRITE_FAILED"); return; }
    ahtStarted = clockUs(); ahtPending = true;
    return;
  }
  if (now-ahtStarted < 80000) return;
  // No data-register address: read the seven-byte status/data/CRC response.
  uint8_t b[7];
  if (Wire.requestFrom(AHT_ADDRESS, (uint8_t)7) != 7) {
    while (Wire.available()) Wire.read();
    ahtPending=false; event("AHT_READ_FAILED"); return;
  }
  for (uint8_t i=0; i<7; i++) b[i]=Wire.read();
  if (b[0] & 0x80) {
    if (now-ahtStarted > 200000) { ahtPending=false; event("AHT_BUSY_TIMEOUT"); }
    return;
  }
  ahtPending=false;
  if (crc8(b, 6) != b[6]) { event("AHT_CRC_FAILED"); return; }
  uint32_t rawH=((uint32_t)b[1]<<12)|((uint32_t)b[2]<<4)|(b[3]>>4);
  uint32_t rawT=((uint32_t)(b[3]&0x0F)<<16)|((uint32_t)b[4]<<8)|b[5];
  float humidity=rawH*(100.0/1048576.0);
  float temperature=rawT*(200.0/1048576.0)-50.0;
  Serial.print(F("T,")); printTime(clockUs()); Serial.print(',');
  Serial.print(temperature, 3); Serial.print(','); Serial.println(humidity, 3);
}

void setup() {
  Serial.begin(115200);
  // Native USB: wait briefly for the host, without requiring a permanent connection.
  uint32_t usbWait = millis();
  while (!Serial && millis()-usbWait < 3000) { delay(1); }
  delay(100);
  Serial.println("#BOOT FEATHER32U4_AHT30_PPG_V1");
  Wire.begin(); Wire.setClock(50000);
#if defined(WIRE_HAS_TIMEOUT)
  Wire.setWireTimeout(25000, true); // bound a stuck bus transaction to 25 ms
#endif
  ppgReady = setupPPG();
  if (!ppgReady) writeReg(0x09, 0x80);
  metadata();
  nextMeta = clockUs()+5000000;
}

void loop() {
  uint64_t now = clockUs();
  if (!ppgReady && now >= nextPpgRetry) {
    nextPpgRetry = now + 2000000ULL;
    ppgReady = setupPPG();
    if (ppgReady) {
      segment++;
      ppgSeq = 0;
      lastService = 0;
      event("PPG_RECOVERED");
    }
  }
  if (ppgReady) {
    if (lastService && now-lastService > 500000) {
      event("SERVICE_GAP");
      if (!clearFifo()) disablePPG("PPG_FIFO_RECOVERY_FAILED");
      segment++; ppgSeq=0; sampleAnchor=clockUs();
    }
    lastService = clockUs();
    uint8_t pointers[3];
    if (ppgReady && !readBytes(0x04, pointers, 3)) disablePPG("PPG_I2C_FAILED");
    else if (ppgReady && (pointers[1] & 0x1F)) {
      event("FIFO_OVERFLOW", pointers[1] & 0x1F); // lower-bound lost count
      if (!clearFifo()) disablePPG("PPG_FIFO_RECOVERY_FAILED");
      segment++; ppgSeq=0; sampleAnchor=clockUs();
    } else if (ppgReady) {
      uint8_t count = (pointers[0]-pointers[2]) & 0x1F;
      for (uint8_t i=0; i<count; i++) {
        uint8_t b[6];
        if (!readBytes(0x07, b, 6)) { disablePPG("PPG_SAMPLE_READ_FAILED"); break; }
        uint32_t red=(((uint32_t)b[0]<<16)|((uint32_t)b[1]<<8)|b[2]) & 0x3FFFF;
        uint32_t ir=(((uint32_t)b[3]<<16)|((uint32_t)b[4]<<8)|b[5]) & 0x3FFFF;
        uint64_t readTime=clockUs();
        uint64_t sampleTime=sampleAnchor+((uint64_t)ppgSeq+1)*PPG_PERIOD_US;
        Serial.print(F("P,"));
        Serial.print(segment);
        Serial.print(',');
        Serial.print(ppgSeq++);
        Serial.print(',');
        printTime(sampleTime);
        Serial.print(',');
        printTime(readTime);
        Serial.print(',');
        Serial.print(red);
        Serial.print(',');
        Serial.println(ir);
      }
    }
  }
  now=clockUs();
  if (now>=nextMeta) { metadata(); nextMeta=clockUs()+5000000; }
  serviceAht();
  delay(1);
}
