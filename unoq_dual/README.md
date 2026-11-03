# UNO Q — temperature + PPG + PPG amplitude

Three simultaneous live Python graphs:
1. Temperature (A1 is the thermistor, A0 is the reference).
2. Current IR PPG waveform, last8seconds:800ms causal baseline tracker followed by an80ms trailing moving average. Gray shows AC before the moving average; blue shows smoothed AC on the same scale.
3. IR pulsatile amplitude over time, in ADC counts.

For an undamaged, correctly powered MAX30102 breakout. Firmware cannot protect against a supply short or an electrically damaged module.

## Wiring

| Connection | UNO Q |
|---|---|
| Bridge fixed-reference midpoint | A0 |
| Filtered thermistor midpoint | A1 |
| Bridge excitation | 3.3V; A0 and A1 must each stay within 0–3.3V |
| Bridge bottom rail | GND |
| PPG SDA | dedicated SDA / D20 |
| PPG SCL | dedicated SCL / D21 |
| PPG GND | GND, not a bridge midpoint |
| PPG VIN | breakout-specified supply; use3.3V only if supported |

Leave PPG INT, RD, IRD and extra GND unconnected for this polling setup. This code uses Wire on D20/D21. A4/A5 and Qwiic are separate UNO Q buses and are not used by this sketch.

Thermistor model: upper fixed4.7k, lower4.7k@25°C NTC/B3950 to GND; reference leg two equal4.7k resistors. With the filter, thermistor midpoint ->160k ->A1 and10µF from A1 to A0/reference midpoint. Use a capacitor appropriate for the polarity across both midpoints. No differential amplifier assumed. The conversion uses:

R_NTC =4700*A1/(2*A0−A1)
T_C =1/(1/298.15 +ln(R_NTC/4700)/3950)−273.15

When A1 decreases due to warming, temperature increases. Actual calibration is still required; printed decimals do not establish accuracy.

## 1. Upload the NEW combined sketch

Close any Python graph that currently owns the USB port. In Arduino IDE open `UnoQDualSensors/UnoQDualSensors.ino`, select Arduino UNO Q and its port, then upload. Install `Arduino_RouterBridge` if missing. Uses core Wire directly, without a separate PPG library.

This replaces BOTH the thermistor-only and connection-test sketch. The old sketch cannot supply all three graphs. Serial Monitor should show `#BOOT UNOQ_DUAL_V2`, `#META` JSON, `P,...` optical records and `T,...` temperature records. If sensor setup fails, events explain the failure and temperature acquisition continues; no fake PPG is generated. A metadata message repeats every5seconds so starting Python later is supported.

Nominal settings:

| Measurement | Sampling | ADC |
|---|---|---|
| MAX30102 red |200Hz ADC /4 average ->50Hz output|18bit,411µs integration|
| MAX30102 IR |200Hz ADC /4 average ->50Hz output|18bit,411µs integration|
| Thermistor bridge |5 sequential A0/A1 pairs/s|14bit|
| Python display |10 refreshes/s|display only|
| Amplitude trend |1 estimate/s after6seconds|4second interior window|

The MAX30102 ADC acquires200samples/s per channel and averages four consecutive conversions into each FIFO sample. The transmitted FIFO output remains50samples/s per channel, with20ms reconstructed timestamp spacing. ADC range is4096nA. LED code0x08 is a modest fixed starting drive (~1.6mA nominal peak); it may need adjustment between runs for adequate wrist signal. There is no automatic current/gain adjustment that would mask or impose amplitude trends. Fixed settings, contact pressure and placement matter when comparing amplitude. No emitted-light visibility guarantee is made for IR.

## 2. Start Python

Close Serial Monitor / Arduino IDE to free the USB port. Keep the board powered over USB. In a terminal inside this extracted folder:

```sh
python3 -m pip install -r requirements.txt
python3 live_monitor.py
```

With exactly one UNO Q connected, port selection is automatic. Otherwise:

```sh
python3 live_monitor.py --list-ports
python3 live_monitor.py --port /dev/cu.usbmodemYOURPORT --site dorsal
```

On Windows use the listed COM port. `--site volar` labels palm-side placement; it does not change signal scaling.

Wait up to5seconds for metadata, then6seconds of continuous PPG for the amplitude estimate. Missing sensor data stays blank, with a status message. Keep the optical sensor still and shield ambient light. A loose sensor or motion can produce amplitude changes unrelated to perfusion. First confirm a repeatable waveform at a fingertip before evaluating wrist placement.

Close the graph window to stop. Raw protocol records—including both optical channels, both thermistor ADC counts, metadata and events—save as timestamped text in `recordings/` beside this script. Old temperature-only recordings remain untouched. This combined version currently logs the raw mixed protocol rather than separate CSV tables.

## Graph interpretation

The current PPG plot applies an800ms one-pole DC tracker, then a four-tap trailing average at50Hz (80ms of samples). The moving average adds30ms group delay; the baseline tracker has frequency-dependent phase response. It does not normalize amplitude. Both before/after moving-average traces remain visible; logging preserves FIFO output before Python filtering. The amplitude trend band-passes IR at0.5–8Hz using a third-order Butterworth filter applied forward/backward, then computes the95th−5th percentile over a4second interior with1second padding on either side. Estimates are delayed by1second and timestamped at the end of the interior. First estimate needs6seconds of contiguous data.

This is a robust window AC amplitude—not exact beat peak-to-peak, perfusion index, heart rate, SpO2, blood pressure or a nerve-block success score. Clipped values suppress estimates; motion and poor contact are not fully classified. There is no fitted nerve-block response. Y axes auto-scale for readability; compare numeric amplitude values, not apparent screen pulse height.

History plots retain/display up to10minutes of the run; the current waveform shows8seconds. The raw log retains the full received run.

## Timing and data integrity

`P,segment,sequence,estimated_sample_us,readout_us,red,ir`

`T,sequence,adc_start_us,a0_reference,a1_ntc,firmware_temp_c,missed_deadlines`

`E,time_us,event,value`

Python independently recalculates temperature from A0/A1. The PPG sample clock is reconstructed from nominal50Hz and startup timing; hardware oscillator drift means it is approximate, not a precision optical timestamp. Red/IR and A0/A1 are sequential, not truly simultaneous.

FIFO overflow or a service gap clears the FIFO and starts a segment; Python discards the affected filter buffer rather than interpolating. A sensor I2C error disables PPG; re-upload/reset only after checking hardware. Raw events are logged. Periodic metadata does not reset the graph. Startup historical USB backlog is discarded where detectable. If no new records arrive for3seconds after data starts, the plot shows a stale-data warning.

## Offline demonstration

```sh
python3 live_monitor.py --demo
MPLBACKEND=Agg python3 live_monitor.py --demo --check
```

Demo is explicitly synthetic. `--check` verifies temperature direction/conversion, synthetic amplitude increase and gap handling and writes `monitor_demo.png`. It does not connect to hardware. The synthetic preview is not evidence about the connected PPG.

The ADC/rate/register settings follow the [MAX30102 manufacturer datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/max30102.pdf). UNO Q Wire mapping was checked against the installed board core. No physiological accuracy or clinical validation is claimed.

## Local verification result

Compiled successfully against the UNO Q Zephyr 1.1.0 core (96,688 bytes flash, 40,628 bytes RAM). Python checks cover the thermistor conversion, ADC rail rejection, amplitude response and sample-gap reset. Hardware signal quality and temperature calibration still require verification.

## Review of supplied lab files and smoothing update

- Acquisition.h and Acquisition(1).h are identical transport helpers. Separation of acquisition/transport from display processing is retained; no new SparkFun dependency is required in this direct-register UNO Q firmware.
- Lab1_FirstSignal.ino uses0x58; the MAX30102 address is0x57. That value was not copied.
- Lab2_PPG_Logger.ino correctly accounts for200Hz ADC /4 averaging =50Hz FIFO output, with20ms timestamps. This setting is implemented; LED code remains0x08 to isolate averaging changes from illumination changes.
- Lab3_PPG_Logger.ino has the same ADC/averaging settings but uses5ms timestamps. That would make derived rates four times too high. Our timestamp spacing remains20ms.
- index.html uses an800ms one-pole baseline tracker and an80ms trailing average. Those ideas are implemented in Python. At50Hz our80ms average uses exactly4taps; the browser's inclusive endpoint window can contain5samples over an80ms timestamp span, so the implementations are intentionally not bit-for-bit identical.
- The supplied sketches target Uno R3. Wire timeout calls, pin assumptions and the SparkFun-specific four-entry software queue logic were not copied into the UNO Q direct-FIFO implementation. The FIFO overflow logic is retained conservatively; we do not assume a nonzero hardware overflow counter is harmless.
- The teaching page's peak thresholds,350ms refractory period and ML classification code are not adopted as a validated heart-rate estimator. The requested amplitude metric remains independent of the display-only moving average:0.5–8Hz Butterworth processing of FIFO output, Q95−Q5 over4seconds with1second padding each side.

Changing hardware averaging starts a new recording; do not directly interpret an old-vs-new amplitude difference as physiological. Raw here means the sensor's50Hz FIFO output AFTER its internal four-conversion average; individual200Hz conversions are not exported. No outliers are silently replaced and ADC clipping remains visible/flagged. Smooth noise can still be noise; a stable pulse waveform must be demonstrated physically.

Optional display parameters:
python3 live_monitor.py --smooth-ms 80 --baseline-ms 800

Verification: UNO Q core compilation passed. A synthetic1.2Hz sinusoid retained98.62% RMS amplitude through the moving-average step; added white-noise residual SD decreased from29.90 to15.34counts. Gap reset, rail rejection, metadata handling and temperature conversion were checked. These are software tests, not validation of the observed fingertip signal.

### 3.3 V supply configuration

Both the thermistor bridge and PPG breakout are supplied from 3.3 V with common ground. The UNO Q ADC reference remains 3.3 V. The conversion is ratiometric: `R_NTC = 4700 * A1 / (2*A0 - A1)`. With equal reference resistors, A0 should measure approximately 1.65 V (8192 counts at 14 bits). Python displays both ADC voltages and the expected reference voltage. ADC rail readings remain invalid rather than being converted into a temperature.
