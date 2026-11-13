# PPG recorder run card

**Upload → Preview → Start → Hold → Release → Save**

1. Put PPGOnly.ino in a folder named PPGOnly, upload it to the Arduino UNO Q, and close Serial Monitor.
2. Install dependencies with `python3 -m pip install -r requirements.txt`. Rehearse with `python3 ppg_recorder.py --demo`. For the sensor, run `python3 ppg_recorder.py --port YOUR_PORT`.
3. Check the live IR trace. Press **Start recording** during baseline. Follow the lab-approved cuff protocol.
4. Press **Hold** at the designated plateau. For the single-target trial, mark the 180 mmHg plateau. For the stepped trial (40 → 110 → 180 mmHg, planned 20 s each), mark only the final plateau to match Living Optics.
5. Press **Release** at cuff release. Wait for **Saved** after 32 seconds. Reopen the recorder for the next trial.

The two plots show IR PPG and windowed amplitude. Find samples, amplitude estimates, and timestamped markers in the local recordings folder.
