# Wrist PPG simulation

Six synchronized synthetic PPG graphs: baseline, post-block response, and amplitude trend for dorsal (back) and volar (front) wrist placement after a brachial plexus block.

## Run

Open `ppg_simulation.html` in a browser. No server or external dependencies are required. Controls include independent assumed gains, response curve, pause/restart, playback speed, and CSV export for both placements.

For Python:

```sh
python3 -m pip install -r requirements.txt
python3 ppg_simulation.py
# Export both-site CSV and a six-panel preview:
python3 ppg_simulation.py --export
```

## Assumptions

- Synthetic adult scenario: 72 bpm and nominal BP 118/76 mmHg. BP is a label, not inferred from PPG or used in a pressure-to-optics model.
- 100 samples/second; arbitrary IR AC units, not calibrated MAX30102 ADC counts.
- One-minute baseline followed by 30 minutes after the block.
- Default amplitude gain 1.5× and exponential time constant 5 minutes are illustrative, not estimated from patient data.
- Linear and logarithmic alternatives reach their plateau at 15 minutes. A transient alternative peaks at 10 minutes and returns to baseline at 30 minutes. These are sensitivity scenarios, not optimized fits.
- Both wrist sites use the same baseline template and default gain because a validated site-specific transfer model is unavailable here. Their adjustable gains explore hypotheses rather than establish placement superiority.
- Signal polarity displays upward pulses. The model includes small respiratory modulation and deterministic interference. It omits optical DC changes, motion, changing contact pressure, vascular mechanics, SpO₂ calibration and automatic gain control.
- Playback speed is separate from sampling frequency. The before trace is a continuing baseline reference, not a simultaneous pre-block measurement at the same site.

## Evidence and limits

1. [Eguchi and Honma: superficial venous dilatation after axillary block](https://pmc.ncbi.nlm.nih.gov/articles/PMC6326044/): basilic vein near the elbow in vascular-access surgery patients; diameter 3.0 ± 1.1 to 4.1 ± 1.2 mm. The increase in group means is 36.7%; the reported mean individual post/pre ratio is 143 ± 34%, corresponding to a mean individual increase of 43%. These are different statistics. This is not wrist-vein evidence or a healthy person's paired trajectory.
2. [Median nerve block and radial artery cannulation](https://pmc.ncbi.nlm.nih.gov/articles/PMC9354365/): radial artery near the wrist in women with gestational hypertension. Table 3 reports median diameter changes of +22% at 10 minutes and 0% at 30 minutes, and median CSA changes of +50% and 0%. The diameter table's label (mm) and values (0.25 to 0.30) are inconsistent with its CSA values; absolute diameters should not be silently corrected or treated as verified. This study used median nerve block, not the same protocol/population as the proposed brachial plexus block scenario.
3. [Perfusion index after infraclavicular block](https://pubmed.ncbi.nlm.nih.gov/23397432/): supports increased finger AC/DC after successful blocks, but does not calibrate MAX30102 wrist amplitude or establish a front/back wrist ratio.
4. [Contact force and wrist PPG](https://pubmed.ncbi.nlm.nih.gov/15032494/): sensor contact conditions affect optical measurements and must be controlled in a real experiment.

Vessel enlargement does not imply proportional PPG amplitude enlargement. Cohort standard deviations are not time-series noise or individual confidence bounds. Normal blood pressure does not uniquely determine PPG morphology. This is an educational simulation, not a validated predictor of nerve block success or a medical measurement device.

The large generated CSV is excluded from source control; regenerate it with Python or the browser download control.

## Live Arduino UNO Q acquisition

See [unoq_dual/README.md](unoq_dual/README.md) for the 3.3 V thermistor bridge and MAX30102 firmware, Python installation, wiring, sampling settings, and three live plots (temperature, smoothed PPG, and PPG amplitude). Recorded sensor data is excluded.
