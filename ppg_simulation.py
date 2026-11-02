"""Synthetic single-person wrist PPG; NOT a validated physiological prediction.

Run: python3 -m pip install numpy matplotlib
     python3 ppg_simulation.py
Export data + still: python3 ppg_simulation.py --export

Brachial plexus block, dorsal or volar wrist; no validated site scaling.
Assumed HR 72 bpm, BP 118/76 mmHg (scenario label, not inferred from PPG).
IR-channel AC is in arbitrary units, not calibrated MAX30102 ADC counts.
No diameter-to-PPG transfer law is asserted. Gain is an independent assumption.
Venous cohort SDs are not noise or individual confidence limits.
"""
from pathlib import Path
import argparse
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Slider, RadioButtons, Button

FS = 100
HR = 72
DURATION = 30 * 60
BLOCK = 60  # one minute baseline
TAU = 5 * 60  # assumed, not fitted
GAIN = 1.5  # arbitrary sensitivity scenario, not derived from vessel diameters


def response(t, model="Exponential", gain=GAIN):
    """Dimensionless pulsatile amplitude multiplier; block starts at 60 s.

    Transient is only a qualitative artery-paper-inspired alternative: peak
    at 10 min, back to baseline at 30 min. It is NOT measured wrist PPG.
    """
    u = np.maximum(np.asarray(t) - BLOCK, 0)
    if model == "Linear":
        r = np.minimum(u / (3 * TAU), 1)
    elif model == "Logarithmic":
        r = np.log1p(np.minimum(u, 3 * TAU) / 60) / np.log1p(3 * TAU / 60)
    elif model == "Transient":
        rise = (1 - np.exp(-u / TAU)) / (1 - np.exp(-600 / TAU))
        fall = (np.exp(-(u - 600) / TAU) - np.exp(-1200 / TAU)) / (1 - np.exp(-1200 / TAU))
        r = np.where(u <= 600, rise, np.maximum(fall, 0))
    else:
        r = 1 - np.exp(-u / TAU)
    return 1 + (gain - 1) * r


def pulse(t):
    """Periodic fast upstroke, decay, notch, secondary wave; fixed shared shape."""
    phase = np.mod(np.asarray(t) * HR / 60, 1)
    return np.interp(phase,
                     [0, .07, .16, .28, .40, .46, .55, .72, .90, 1],
                     [0, .35, 1, .62, .37, .25, .34, .16, .02, 0]) - .335


def signals(t, model="Exponential", gain=GAIN):
    # Same individual, same cardiac phase and respiratory modulation.
    t = np.asarray(t)
    ac = pulse(t) * (1 + .035 * np.sin(2 * np.pi * .20 * t))
    # Deterministic smooth interference so results are reproducible across windows.
    noise = .006 * np.sin(2 * np.pi * 13.7 * t) + .004 * np.sin(2 * np.pi * 19.1 * t)
    baseline_wander = .015 * np.sin(2 * np.pi * .08 * t)
    before = ac + baseline_wander + noise
    after = response(t, model, gain) * ac + baseline_wander + noise
    return before, after


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    fig, axes = plt.subplots(3, 2, figsize=(14, 10))
    fig.subplots_adjust(left=.08, right=.96, top=.87, bottom=.18, hspace=.62, wspace=.3)
    fig.suptitle("Synthetic wrist PPG after brachial plexus block\n"
                 "HR 72 bpm; assumed BP 118/76; unvalidated amplitude response", fontsize=13)
    lines = []
    for col, site in enumerate(["Back / dorsal wrist", "Front / volar wrist"]):
        for row, condition in enumerate(["Before (baseline reference)", "After (evolving response)", "Amplitude trend"]):
            ax = axes[row, col]
            ax.set_title(site + " · " + condition, fontsize=10)
            ax.grid(alpha=.2)
            if row < 2:
                ax.set(xlim=(-8, 0), ylim=(-2.2, 4.3), xlabel="Shared window (s)", ylabel="PPG AC (a.u.)")
            else:
                ax.set(xlim=(-1, 30), ylim=(.3, 6.2), xlabel="Time since block (min)", ylabel="Amplitude / baseline")
                ax.axhline(1, color="gray", linestyle="--")
            line, = ax.plot([], [], color="#2563eb" if row == 0 else "#d97706")
            lines.append(line)
    back = Slider(fig.add_axes([.12, .095, .28, .025]), "Back gain", .5, 6, valinit=1.5)
    front = Slider(fig.add_axes([.60, .095, .28, .025]), "Front gain", .5, 6, valinit=1.5)
    radio = RadioButtons(fig.add_axes([.38, .005, .24, .07]), ["Exponential", "Linear", "Logarithmic", "Transient"])
    pause = Button(fig.add_axes([.78, .025, .12, .04]), "Pause / play")
    status = fig.text(.08, .125, "Equal default gains are assumptions; no validated front/back scaling.", fontsize=10)
    state = {"t": 8., "paused": False}
    pause.on_clicked(lambda _: state.update(paused=not state["paused"]))

    def draw(now):
        t = np.arange(now-8, now, 1/FS)
        hist = np.linspace(0, now, 800)
        for col, gain in enumerate([back.val, front.val]):
            b, a = signals(t, radio.value_selected, gain)
            lines[col*3].set_data(t-now, b)
            lines[col*3+1].set_data(t-now, a)
            lines[col*3+2].set_data((hist-BLOCK)/60, response(hist, radio.value_selected, gain))
        status.set_text(f"60× playback | since block {(now-BLOCK)/60:.2f} min | assumed gains, no validated site scaling")
        return lines

    if args.export:
        target = Path(__file__).resolve().parent
        time = np.arange(0, DURATION+BLOCK+1/FS, 1/FS)
        b, a = signals(time)
        amp = response(time)
        np.savetxt(target / "ppg_simulated.csv", np.c_[time, (time-BLOCK)/60, b, a, amp, b, a, amp],
                   delimiter=",", header="time_s,time_since_block_min,back_before_ac_au,back_after_ac_au,back_amplitude_ratio,front_before_ac_au,front_after_ac_au,front_amplitude_ratio", comments="")
        draw(660)
        fig.savefig(target / "ppg_preview.png", dpi=160)
        plt.close(fig)
        print("Exported six-panel preview and both-site CSV")
        return

    def update(_):
        if not state["paused"]:
            state["t"] = min(state["t"]+3, DURATION+BLOCK)
        return draw(state["t"])
    animation = FuncAnimation(fig, update, interval=50, cache_frame_data=False)
    plt.show()
    return animation


if __name__ == "__main__":
    main()
