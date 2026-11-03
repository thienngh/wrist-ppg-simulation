/* UNO Q: thermistor bridge + MAX30102. For verified, undamaged breakout only.
 * A0 = fixed reference midpoint; A1 = filtered lower-NTC branch midpoint.
 * NTC 4.7k@25C, B3950, upper fixed 4.7k; equal reference-leg resistors.
 * Bridge excitation 3.3V, bottom rail GND. A0/A1 must remain within 0..3.3V.
 * Ratiometric conversion uses A0=half excitation; ADC reference stays 3.3V.
 * PPG SDA=D20, SCL=D21 (Wire).
 * PPG VIN must follow the breakout supplier's specification. Common GND.
 * 200Hz red/IR ADC, 4-sample FIFO average ->50Hz output; 411us/18bit; NTC5Hz/14bit.
 */
#include <Arduino.h>
#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include <math.h>
#include <stdio.h>

const float BRIDGE_SUPPLY_V = 3.3f; // nominal excitation, not ADC reference
const float ADC_REFERENCE_V = 3.3f;
const uint8_t PPG_ADDRESS = 0x57;
const uint8_t LED_CODE = 0x08; // fixed modest starting drive; adjust only between runs
const uint32_t PPG_PERIOD_US = 20000, TEMP_PERIOD_US = 200000;
uint32_t lastMicros = 0, ppgSeq = 0, tempSeq = 0, segment = 0;
uint64_t highMicros = 0, sampleAnchor = 0, nextTemp = 0, lastService = 0, nextMeta = 0;
bool ppgReady = false;

uint64_t clockUs() {
  uint32_t now = micros();
  if (now < lastMicros) highMicros += (1ULL << 32);
  lastMicros = now;
  return highMicros + now;
}

void event(const char *name, unsigned long value = 0) {
  char line[160];
  snprintf(line, sizeof(line), "E,%llu,%s,%lu", (unsigned long long)clockUs(), name, value);
  Monitor.println(line);
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
  return writeReg(0x04, 0) && writeReg(0x05, 0) && writeReg(0x06, 0);
}

void disablePPG(const char *reason) {
  ppgReady = false;
  writeReg(0x09, 0x80); // best-effort shutdown; cannot fix electrical faults
  event(reason);
}

void metadata() {
  char line[400];
  snprintf(line, sizeof(line),
    "#META {\"protocol\":2,\"ppg_hz\":50,\"ppg_bits\":18,\"adc_sample_hz\":200,\"fifo_average\":4,\"temp_hz\":5,\"adc_bits\":14,\"bridge_supply_v\":%.2f,\"adc_reference_v\":%.2f,\"r_fixed\":4700,\"r0\":4700,\"beta\":3950,\"t0_c\":25,\"sensor_pin\":\"A1\",\"reference_pin\":\"A0\",\"led_code\":%u,\"ppg_ready\":%s}",
    (double)BRIDGE_SUPPLY_V, (double)ADC_REFERENCE_V, LED_CODE, ppgReady ? "true" : "false");
  Monitor.println(line);
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
        writeReg(0x0A, 0x2B) && writeReg(0x0C, LED_CODE) &&
        writeReg(0x0D, LED_CODE) && clearFifo())) {
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

void setup() {
  Monitor.begin(115200);
  analogReference(AR_DEFAULT); // UNO Q 3.3 V ADC range
  analogReadResolution(14);
  pinMode(A0, INPUT); pinMode(A1, INPUT);
  delay(1000);
  Monitor.println("#BOOT UNOQ_DUAL_V2 A0=reference A1=NTC");
  Wire.begin(); Wire.setClock(100000);
  ppgReady = setupPPG();
  if (!ppgReady) writeReg(0x09, 0x80);
  metadata();
  nextMeta = clockUs()+5000000;
  nextTemp = clockUs();
}

void loop() {
  uint64_t now = clockUs();
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
        char line[150];
        snprintf(line, sizeof(line), "P,%lu,%lu,%llu,%llu,%lu,%lu",
          (unsigned long)segment, (unsigned long)ppgSeq++, (unsigned long long)sampleTime,
          (unsigned long long)readTime, (unsigned long)red, (unsigned long)ir);
        Monitor.println(line);
      }
    }
  }
  now=clockUs();
  if (now>=nextTemp) {
    uint64_t missed=(now-nextTemp)/TEMP_PERIOD_US;
    nextTemp+=(missed+1)*TEMP_PERIOD_US;
    analogRead(A0);
    uint64_t t=clockUs();
    int a0=analogRead(A0);
    analogRead(A1); int a1=analogRead(A1);
    float temp=NAN;
    if (a0>0 && a0<16383 && a1>0 && a1<16383 && 2L*a0>a1) {
      float r=4700.0f*a1/(2.0f*a0-a1);
      float invT=1.0f/298.15f+log(r/4700.0f)/3950.0f;
      if (invT>0) temp=1.0f/invT-273.15f;
    }
    char line[160];
    snprintf(line, sizeof(line), "T,%lu,%llu,%d,%d,%.3f,%llu",
      (unsigned long)tempSeq++, (unsigned long long)t, a0, a1, (double)temp,
      (unsigned long long)missed);
    Monitor.println(line);
  }
  now=clockUs();
  if (now>=nextMeta) { metadata(); nextMeta=clockUs()+5000000; }
  delay(1);
}
