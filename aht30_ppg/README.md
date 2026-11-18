# AHT30 + MAX30102 — classic Arduino UNO

Three live panels: AHT30 temperature (top), workshop-smoothed infrared PPG (middle),
and PPG amplitude trend (bottom).
Humidity is displayed numerically and saved with temperature. No NTC, bridge,
analog input, or external amplifier is used by this version.

## Wiring

For the classic UNO R3: both sensor SDA pins -> A4/SDA; both SCL pins -> A5/SCL;
both GND pins -> UNO GND. The duplicate SDA/SCL header is the same bus.
AHT30 address is 0x38; MAX30102 address is 0x57.

AHT30 supply supports 2.2–5.5 V. User reports both breakout modules operate at
5 V. That is a hardware property, not a software setting: MAX30102 VIN must be
rated for 5 V by the breakout manufacturer; the bare chip requires 1.8 V VDD.
Confirm the breakout I2C level/pull-up arrangement is compatible with the shared
UNO bus. Do not assume VIN labelling alone establishes 5 V compatibility.

## Run

1. Open AHT30_PPG/AHT30_PPG.ino in Arduino IDE.
2. Select Arduino Uno (classic R3, NOT UNO Q), select its port, Upload.
   Only built-in Arduino/Wire libraries are needed.
3. Close Serial Monitor (baud 115200 if using it for diagnostics).
4. In Terminal, open this folder (after cloning, use `cd wrist-ppg-simulation/aht30_ppg`).
   For the original local project:

   cd "$HOME/Documents/BioE192_CAPSTONE_ TEAM K.O./AHT30_PPG"
   python3 -m pip install -r requirements.txt
   python3 temperature_ppg_monitor.py

The program selects the device automatically when exactly one USB serial port
is present. Otherwise:

   python3 temperature_ppg_monitor.py --list-ports
   python3 temperature_ppg_monitor.py --port /dev/cu.usbmodem101

Use your actual listed device if different. Synthetic rehearsal:

   python3 temperature_ppg_monitor.py --demo

## Controls and saved data

Live preview starts immediately. Place the sensors and settle your hand, then
click Start recording: all three panels, old markers, and processing windows
clear, the clock resets to 0 s, and CSV saving begins;
Hold and Release add repeatable, timestamped marks to all three panels.
Stop recording saves/closes the current trial while preview continues.
Start recording again clears the panels and creates a new folder. Closing the window also saves.
There is no automatic cutoff. Files go to recordings/run_<date_time>/:
ppg.csv (raw red/IR and smoothed IR), temperature.csv (C and %RH), events.csv,
and amplitude.csv (session_s, amplitude_counts).
Demo recordings use demo_ prefixes. Session time resets to zero at each Start;
preview data is excluded from each new trial. CSV session time uses the MCU timestamps;
host receipt time is separately retained. AHT temperature timestamp is its readout
completion after an approximately 80 ms conversion, not an instantaneous sample.

## Acquisition and display

- PPG: ADC 200 samples/s, FIFO averaging 4 -> 50 output readings/s/channel.
  411 microsecond integration, 18-bit output (0–262143), 4096 nA ADC range.
  IR LED code 0x10 (~3.2 mA peak); red code 0x08 (~1.6 mA peak).
  Both channels are acquired; the plot uses IR. Keep LED settings fixed per trial.
- PPG display matches the previous workshop recorder: causal 800 ms exponential
  baseline tracker followed by inclusive 80 ms moving average (5 points at 50 Hz).
  Raw samples are saved unchanged. Signal gaps reset display smoothing.
  The lower panel shows the latest 8 s; large motion spikes remain possible.
- AHT30: 1 measurement/s; 20-bit encoded temperature/humidity fields with CRC-8
  (initial 0xFF, polynomial 0x31). Temperature = raw*200/2^20 - 50;
  humidity = raw*100/2^20. Encoded bit count is not effective accuracy:
  manufacturer's typical temperature resolution is 0.01 C and accuracy ±0.5 C.
  Manufacturer temperature response is approximately 5–30 s; installation matters.
  This measures the sensor's local temperature; it is not a core-temperature reading.
- AHT conversion is asynchronous: the PPG FIFO is serviced during its 80 ms wait.
  Missing sensors/CRC failures are reported rather than inventing readings.
  AHT retries each second; after a PPG I2C fault reset the UNO after fixing wiring.
  A physically stuck shared I2C bus can affect both sensors.
- Amplitude uses the earlier recorder method: third-order 0.5–8 Hz Butterworth
  bandpass on a rolling 6 s raw IR window, applied forward/backward. Discard the
  first/last 1 s and calculate P95 minus P5 across the middle 4 s, once per second.
  First value needs 6 s of continuous data. Its timestamp labels the end of that
  middle interval, about 1 s before the latest sample. Gaps, restarts, and Start
  reset the window; clipped windows produce blank CSV values and plot gaps.
  The trend is amplitude in counts, not percent change, blood pressure, or SpO2.
  Percentiles describe a window distribution; they do not match individual beats.
  Motion can inflate amplitude. No automatic motion rejection is claimed.

## Sources and verification

Aosong AHT30 manufacturer datasheet:
https://www.aosong.com/userfiles/files/media/AHT30%E6%B8%A9%E6%B9%BF%E5%BA%A6%E4%BC%A0%E6%84%9F%E5%99%A8%E8%AF%B4%E6%98%8E%E4%B9%A6%20A2-1012.pdf
Analog Devices MAX30102 datasheet:
https://www.analog.com/media/en/technical-documentation/data-sheets/max30102.pdf

Compiled for arduino:avr:uno: 10028 bytes flash, 844 bytes static RAM.
Python synthetic checks: parsing, both CSV streams, repeated Hold/Release,
Start/Stop saving, restart handling, and interface rendering passed.
Live hardware and electrical compatibility have not been verified by these tests.


## Marker confirmation

Start, every Hold, every Release, and Stop are written to events.csv immediately,
then flushed and synced to disk. Each marker includes seconds since Start and an
ISO local timestamp. No live PPG/temperature reading is required to save markers.
The status line and Terminal say SAVED, and Hold/Release buttons show saved counts.
If you click Hold/Release before Start, the program asks you to start recording.
Repeated Hold/Release clicks are all retained; each marks all three panels.
Starting the next trial clears the display but preserves the previous CSV folder.

For analysis, retain a stable baseline before inflating the cuff; Hold denotes the
reached pressure plateau and Release denotes deflation. Start and Hold alone do
not record exactly when inflation began: keep a known baseline interval or note
inflation time separately. Never reconstruct a missing marker as a known event.
