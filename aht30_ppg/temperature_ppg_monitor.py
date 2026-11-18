"""Classic UNO + AHT30 + MAX30102: temperature, workshop-smoothed IR PPG, and amplitude trend."""
import argparse
import csv
import math
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button
import numpy as np
from scipy.signal import butter, sosfiltfilt


class WorkshopSmoother:
    """Same 800 ms baseline tracker + inclusive 80 ms moving average as PPGRecorder."""
    def __init__(self, fs=50):
        self.weight = -math.expm1(-1 / (fs * .800))
        self.values = deque(maxlen=round(.080 * fs) + 1)
        self.baseline = None

    def reset(self):
        self.baseline = None
        self.values.clear()

    def apply(self, value):
        if self.baseline is None:
            self.baseline = float(value)
        self.baseline += self.weight * (value - self.baseline)
        self.values.append(value - self.baseline)
        return sum(self.values) / len(self.values)


class AmplitudeTracker:
    """Earlier recorder method: 6 s bandpass window, middle 4 s P95-P5."""
    def __init__(self, fs=50):
        self.fs = fs
        self.sos = butter(3, [.5, 8], fs=fs, btype='bandpass', output='sos')
        self.window = deque(maxlen=6*fs)
        self.next_update = 0.

    def reset(self):
        self.window.clear()
        self.next_update = 0.

    def add(self, t, ir):
        self.window.append((t, ir))
        if len(self.window) < self.window.maxlen or t < self.next_update:
            return None
        self.next_update = t+1.
        points = np.asarray(self.window)
        values = points[:, 1]
        amplitude = math.nan
        if np.all((values > 1) & (values < 262142)):
            filtered = sosfiltfilt(self.sos, values)[self.fs:-self.fs]
            low, high = np.percentile(filtered, [5, 95])
            amplitude = float(high-low)
        # Label the end of the analyzed middle interval, approximately 1 s delayed.
        return float(points[-self.fs-1, 0]), amplitude


def parse_record(line):
    fields = line.strip().split(',')
    if fields[0] == 'P' and len(fields) == 7:
        values = tuple(map(int, fields[1:]))
        if not all(0 <= x < 2**18 for x in values[-2:]):
            raise ValueError('PPG counts outside 18-bit range')
        return 'P', values
    if fields[0] == 'T' and len(fields) == 4:
        stamp, temp, rh = int(fields[1]), float(fields[2]), float(fields[3])
        if not (-50 <= temp <= 150 and 0 <= rh <= 100):
            raise ValueError('Invalid temperature/humidity')
        return 'T', (stamp, temp, rh)
    if fields[0] == 'E' and len(fields) >= 3:
        return 'E', fields[2]
    if line.startswith('#BOOT'):
        return 'B', line
    return None


class Monitor:
    def __init__(self, port=None, demo=False, output=None):
        self.port, self.demo = port, demo
        self.origin = time.monotonic()
        self.demo_index = 0
        self.buffer = b''
        self.smoother = WorkshopSmoother()
        self.amplitude_tracker = AmplitudeTracker()
        self.amplitudes = []
        self.previous = None
        self.board_origin = 0 if demo else None
        self.last_board = None
        self.board_offset = 0.
        self.wave = deque(maxlen=1000)
        self.temperatures = []
        self.last_ppg = self.last_temp = None
        self.temp_value = self.rh_value = None
        self.diagnostic = ''
        self.files, self.writers = [], {}
        self.event_file = None
        self.last_marker = ''
        self.hold_count = self.release_count = 0
        self.run = None
        self.output = output or Path(__file__).resolve().parent / 'recordings'
        self.fig, (self.temp_ax, self.ppg_ax, self.amp_ax) = plt.subplots(3, 1, figsize=(11, 10))
        self.fig.subplots_adjust(bottom=.20, top=.92, hspace=.65)
        self.fig.suptitle('SYNTHETIC DEMO' if demo else 'AHT30 temperature + MAX30102 infrared PPG')
        self.temp_ax.set(title='Temperature', ylabel='Temperature (°C)', xlabel='Session time (s)')
        self.ppg_ax.set(title='Current IR PPG — workshop smoothing', ylabel='IR AC counts', xlabel='Session time (s)')
        self.amp_ax.set(title='PPG amplitude trend (P95 − P5)', ylabel='Filtered IR counts', xlabel='Session time (s)')
        self.amp_line, = self.amp_ax.plot([], [], '.-', color='tab:green')
        self.temp_line, = self.temp_ax.plot([], [], color='tab:red')
        self.ppg_line, = self.ppg_ax.plot([], [], color='tab:blue')
        self.status = self.fig.text(.07, .035, 'Waiting for sensor data', fontsize=9)
        self.buttons = []
        for i, (label, callback) in enumerate((('Start recording', self.start), ('Hold', self.hold),
                                               ('Release', self.release), ('Stop recording', self.stop))):
            button = Button(self.fig.add_axes([.07+i*.23, .095, .21, .05]), label)
            button.on_clicked(callback)
            self.buttons.append(button)

    def elapsed(self):
        return time.monotonic()-self.origin

    def event(self, label):
        t = self.elapsed()
        if self.files:
            self.writers['events'].writerow((f'{t:.6f}', datetime.now().astimezone().isoformat(), label))
            # Persist each click now, including while the sensors are unavailable.
            self.event_file.flush()
            os.fsync(self.event_file.fileno())
            if label in ('Start', 'Hold', 'Release', 'Stop'):
                self.last_marker = f'SAVED {label} at {t:.2f} s'
                print(f'{self.last_marker} -> {self.run / "events.csv"}', flush=True)
                self.status.set_text(self.last_marker)
                self.fig.canvas.draw_idle()
        if label in ('Start', 'Hold', 'Release', 'Stop'):
            for axis in (self.temp_ax, self.ppg_ax, self.amp_ax):
                color = {'Start': 'tab:green', 'Hold': 'tab:orange', 'Release': 'tab:purple', 'Stop': '.4'}[label]
                axis.axvline(t, color=color, linestyle='--', linewidth=1)
                axis.text(t, .96, f'{label} {t:.1f}s', transform=axis.get_xaxis_transform(),
                          rotation=90, va='top', fontsize=8, clip_on=True, color=color)

    def start(self, _=None):
        if self.files:
            return
        self.run = self.output / (('demo_' if self.demo else 'run_') + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        self.run.mkdir(parents=True)
        for name, header in (
            ('ppg', ('session_s', 'host_received_s', 'segment', 'sequence', 'board_sample_us', 'board_readout_us', 'red_raw', 'ir_raw', 'ir_smoothed')),
            ('temperature', ('session_s', 'host_received_s', 'board_readout_us', 'temperature_C', 'humidity_percent')),
            ('events', ('session_s', 'local_time', 'event')),
            ('amplitude', ('session_s', 'amplitude_counts')),
        ):
            file = (self.run / f'{name}.csv').open('x', newline='', buffering=1)
            self.files.append(file)
            if name == 'events':
                self.event_file = file
            self.writers[name] = csv.writer(file)
            self.writers[name].writerow(header)
        # Each trial starts with fresh data, independent of the live preview.
        if self.port:
            self.port.reset_input_buffer()
        self.buffer = b''
        self.origin = time.monotonic()
        self.demo_index = 0
        self.board_origin = 0 if self.demo else None
        self.board_offset = 0.
        self.last_board = self.previous = None
        self.last_ppg = self.last_temp = None
        self.temp_value = self.rh_value = None
        self.diagnostic = ''
        self.last_marker = ''
        self.hold_count = self.release_count = 0
        self.buttons[1].label.set_text('Hold')
        self.buttons[2].label.set_text('Release')
        self.wave.clear()
        self.temperatures.clear()
        self.amplitudes.clear()
        self.smoother.reset()
        self.amplitude_tracker.reset()
        for axis, line in ((self.temp_ax, self.temp_line),
                           (self.ppg_ax, self.ppg_line), (self.amp_ax, self.amp_line)):
            for old_line in list(axis.lines):
                if old_line is not line:
                    old_line.remove()
            for text in list(axis.texts):
                text.remove()
            line.set_data([], [])
            axis.set_xlim(0, 8 if axis is self.ppg_ax else 10)
            axis.set_ylim((0, 1) if axis is self.amp_ax else (-1, 1))
            if axis is self.temp_ax:
                axis.set_autoscaley_on(True)
        self.status.set_text('Recording from 0 s — waiting for new sensor readings')
        self.event('Start')
        self.fig.canvas.draw_idle()
        self.buttons[0].label.set_text('Recording')

    def hold(self, _=None):
        if self.files:
            self.event('Hold')
            self.hold_count += 1
            self.buttons[1].label.set_text(f'Hold ({self.hold_count} saved)')
        else:
            self.last_marker = 'Click Start recording before adding Hold markers'

    def release(self, _=None):
        if self.files:
            self.event('Release')
            self.release_count += 1
            self.buttons[2].label.set_text(f'Release ({self.release_count} saved)')
        else:
            self.last_marker = 'Click Start recording before adding Release markers'

    def stop(self, _=None):
        if self.files:
            self.event('Stop')
        for file in self.files:
            file.close()
        self.files.clear()
        self.buttons[0].label.set_text('Start recording')

    def save_amplitude(self, point):
        self.amplitudes.append(point)
        if self.files:
            t, amplitude = point
            self.writers['amplitude'].writerow((f'{t:.6f}', amplitude if math.isfinite(amplitude) else ''))

    def board_time(self, us):
        # Both streams share the MCU clock. Use board time, not USB burst timing.
        if self.board_origin is None:
            self.board_origin = us
            self.board_offset = self.elapsed()
        self.last_board = us
        return self.board_offset + (us-self.board_origin)/1e6

    def ingest(self, line):
        try:
            record = parse_record(line)
            if record is None:
                return
            kind, values = record
            received = self.elapsed()
            if kind == 'B':
                self.event('Board restart')
                self.previous = self.board_origin = None
                self.smoother.reset()
                self.amplitude_tracker.reset()
                self.save_amplitude((received, math.nan))
                self.wave.append((received, math.nan))
                self.temperatures.append((received, math.nan))
                self.last_ppg = self.last_temp = None
            elif kind == 'E':
                self.diagnostic = values
                self.event(values)
            elif kind == 'P':
                seg, seq, us, read_us, red, ir = values
                t = self.board_time(us)
                if self.previous and (seg != self.previous[0] or seq != self.previous[1]+1
                                      or us-self.previous[2] != 20000):
                    self.smoother.reset()
                    self.amplitude_tracker.reset()
                    self.save_amplitude((t, math.nan))
                    self.wave.append((t, math.nan))
                    self.event('PPG sequence gap')
                self.previous = seg, seq, us
                smoothed = self.smoother.apply(ir)
                self.wave.append((t, smoothed))
                self.last_ppg = received
                amplitude = self.amplitude_tracker.add(t, ir)
                if amplitude is not None:
                    self.save_amplitude(amplitude)
                if self.files:
                    self.writers['ppg'].writerow((f'{t:.6f}', f'{received:.6f}', *values, f'{smoothed:.6f}'))
            elif kind == 'T':
                us, temp, rh = values
                t = self.board_time(us)
                self.temperatures.append((t, temp))
                self.temp_value, self.rh_value, self.last_temp = temp, rh, received
                if self.files:
                    self.writers['temperature'].writerow((f'{t:.6f}', f'{received:.6f}', *values))
        except (ValueError, OverflowError):
            self.diagnostic = 'Malformed serial record ignored'

    def update(self, _=None):
        now = self.elapsed()
        if self.demo:
            while self.demo_index/50 <= now:
                i = self.demo_index
                t = i/50
                us = round(t*1e6)
                ir = round(80000+800*math.sin(2*math.pi*1.2*t))
                self.ingest(f'P,0,{i},{us},{us},{ir-10000},{ir}')
                if i % 50 == 0:
                    self.ingest(f'T,{us},{25+.3*math.sin(t/40):.3f},{45+2*math.sin(t/50):.3f}')
                self.demo_index += 1
        elif self.port:
            import serial
            try:
                self.buffer += self.port.read(16384)
                *rows, self.buffer = self.buffer.split(b'\n')
                if len(self.buffer) > 4096:
                    self.buffer = b''
                for row in rows:
                    self.ingest(row.decode('ascii', errors='replace').strip())
            except (OSError, serial.SerialException) as exc:
                self.diagnostic = f'Serial disconnected: {exc}'
                self.event('Serial disconnected')
                self.stop()
                self.port.close()
                self.port = None
        if self.wave:
            self.ppg_line.set_data(*zip(*self.wave))
            right = max(now, self.wave[-1][0])
            left = max(0, right-8)
            self.ppg_ax.set_xlim(left, max(8, right))
            visible = [v for t, v in self.wave if t >= left and math.isfinite(v)]
            if visible:
                lo, hi = min(visible), max(visible)
                pad = max(5, (hi-lo)*.12)
                self.ppg_ax.set_ylim(lo-pad, hi+pad)
        if self.temperatures:
            self.temp_line.set_data(*zip(*self.temperatures))
            self.temp_ax.set_xlim(0, max(10, now))
            self.temp_ax.relim()
            self.temp_ax.autoscale_view(scalex=False)
        if self.amplitudes:
            self.amp_line.set_data(*zip(*self.amplitudes))
            self.amp_ax.relim()
            self.amp_ax.autoscale_view(scalex=False)
            valid = [v for _, v in self.amplitudes if math.isfinite(v)]
            if valid:
                self.amp_ax.set_ylim(0, max(1, max(valid)*1.10))
        self.amp_ax.set_xlim(0, max(10, now))
        temp = 'Waiting for AHT30' if self.temp_value is None else f'{self.temp_value:.2f} °C | RH {self.rh_value:.1f}%'
        ppg = 'PPG live' if self.last_ppg is not None and now-self.last_ppg < 1 else 'NO RECENT PPG'
        if self.last_temp is None or now-self.last_temp > 3:
            temp += ' | NO RECENT TEMPERATURE'
        state = 'RECORDING' if self.files else ('Saved '+self.run.name if self.run else 'Preview — click Start recording to save')
        self.status.set_text(f'{temp} | {ppg} | {state}\n{self.last_marker}  {self.diagnostic}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='Serial device; auto-selects when exactly one USB port exists')
    parser.add_argument('--demo', action='store_true', help='Synthetic signals; no hardware')
    parser.add_argument('--list-ports', action='store_true')
    args = parser.parse_args()
    if args.demo and args.port:
        parser.error('Choose --demo or --port')
    port = None
    if not args.demo or args.list_ports:
        import serial
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        if args.list_ports:
            for item in ports:
                print(f'{item.device}: {item.description}')
            return
        device = args.port
        if not device:
            usb = [item.device for item in ports if item.vid is not None]
            if len(usb) != 1:
                parser.error('Use --list-ports, then --port with your actual serial device')
            device = usb[0]
        try:
            port = serial.Serial(device, 115200, timeout=0)
            port.reset_input_buffer()
        except serial.SerialException as exc:
            parser.exit(1, f'{exc}\nClose Serial Monitor and check --list-ports.\n')
    monitor = Monitor(port=port, demo=args.demo)
    animation = FuncAnimation(monitor.fig, monitor.update, interval=50, cache_frame_data=False)
    try:
        plt.show()
    finally:
        monitor.stop()
        if port and port.is_open:
            port.close()
    return animation


if __name__ == '__main__':
    main()
