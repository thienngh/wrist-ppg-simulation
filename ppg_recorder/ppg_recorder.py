"""Record UNO Q PPG and timestamp operator events. Use --demo to rehearse."""
import argparse
import csv
import json
import math
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button
import numpy as np
from scipy.signal import butter, sosfiltfilt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--port', help='UNO Q serial port')
    source.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    if args.demo:
        serial_port = None
    else:
        import serial
        serial_port = serial.Serial(args.port, 115200, timeout=0)
        serial_port.reset_input_buffer()

    fs = 50
    filter_sos = butter(3, [.5, 8], fs=fs, btype='bandpass', output='sos')
    fig, (wave_ax, amp_ax) = plt.subplots(2, 1, figsize=(11, 7))
    fig.subplots_adjust(bottom=.23, hspace=.45)
    fig.suptitle('Synthetic PPG rehearsal' if args.demo else 'MAX30102 PPG recorder')
    wave_ax.set(title='Current infrared PPG', ylabel='IR ADC counts', xlabel='Recording time (s)')
    amp_ax.set(title='PPG amplitude (P95 − P5)', ylabel='Filtered ADC counts', xlabel='Recording time (s)')
    wave_line, = wave_ax.plot([], [])
    amp_line, = amp_ax.plot([], [], '.-')
    status = fig.text(.1, .025, 'Waiting for PPG samples')
    buttons = [Button(fig.add_axes([.12 + .26*i, .08, .22, .055]), label)
               for i, label in enumerate(('Start recording', 'Hold', 'Release'))]

    start = release = hold = latest = None
    files = []
    writers = {}
    wave = deque(maxlen=600)
    window = deque(maxlen=6*fs)
    amplitudes = []
    previous = None
    next_amp = 0.
    buffer = b''
    demo_start = time.monotonic()
    demo_index = 0
    sensor_ready = args.demo
    diagnostic = ''
    run = None
    receive_clock = None

    def elapsed():
        return time.monotonic() - start

    def finish():
        for file in files:
            file.close()
        files.clear()

    def log_event(label):
        t = elapsed()
        writers['events'].writerow((label, f'{t:.6f}', datetime.now().astimezone().isoformat()))
        files[1].flush()
        for axis in (wave_ax, amp_ax):
            axis.axvline(t, color='0.4', linestyle='--')
            axis.text(t, .96, f'{label} {t:.1f}s', transform=axis.get_xaxis_transform(),
                      rotation=90, va='top', ha='right', fontsize=8, clip_on=True)
        return t

    def start_recording(_):
        nonlocal start, run, previous, next_amp, buffer
        if start is not None or not sensor_ready or latest is None or time.monotonic()-latest > 1:
            return
        run = Path(__file__).resolve().parent / 'recordings' / (
            ('demo_' if args.demo else 'ppg_') + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        run.mkdir(parents=True)
        for name, header in (
            ('samples', ('elapsed_s', 'segment', 'sequence', 'board_sample_us', 'board_readout_us', 'red', 'ir')),
            ('events', ('event', 'elapsed_s', 'local_time')),
            ('amplitude', ('elapsed_s', 'amplitude_counts')),
        ):
            file = (run/f'{name}.csv').open('x', newline='', buffering=1)
            files.append(file)
            writers[name] = csv.writer(file)
            writers[name].writerow(header)
        if serial_port:
            serial_port.reset_input_buffer()
            buffer = b''
        start = time.monotonic()
        window.clear()
        wave.clear()
        previous = None
        next_amp = 0.
        log_event('Start')
        buttons[0].label.set_text('Recording')

    def mark_hold(_):
        nonlocal hold
        if start is not None and files and hold is None:
            hold = log_event('Hold')

    def mark_release(_):
        nonlocal release
        if start is not None and files and hold is not None and release is None:
            release = log_event('Release')

    buttons[0].on_clicked(start_recording)
    buttons[1].on_clicked(mark_hold)
    buttons[2].on_clicked(mark_release)

    def sample(segment, seq, board_us, read_us, red, ir, received):
        nonlocal latest, previous, next_amp
        if not (0 <= ir < 2**18 and 0 <= red < 2**18):
            return
        latest = received
        if not files:
            if start is None:
                wave.append((received-demo_start, ir))
            return
        t = received-start
        if t < 0:
            return
        writers['samples'].writerow((f'{t:.6f}', segment, seq, board_us, read_us, red, ir))
        if previous is not None and (segment != previous[0] or seq != previous[1]+1
                                     or abs(board_us-previous[2]-20000) > 10):
            window.clear()
            wave.append((t, math.nan))
            amplitudes.append((t, math.nan))
            writers['amplitude'].writerow((f'{t:.6f}', ''))
        previous = segment, seq, board_us
        wave.append((t, ir))
        window.append((t, ir))
        if len(window) == window.maxlen and t >= next_amp:
            next_amp = t+1
            points = np.asarray(window)
            values = points[:, 1]
            timestamp = points[-fs-1, 0]
            amplitude = math.nan
            if np.all((values > 1) & (values < 262142)):
                middle = sosfiltfilt(filter_sos, values)[fs:-fs]
                low, high = np.percentile(middle, [5, 95])
                amplitude = high-low
            amplitudes.append((timestamp, amplitude))
            writers['amplitude'].writerow((f'{timestamp:.6f}', amplitude))

    def update(_):
        nonlocal buffer, demo_index, sensor_ready, diagnostic, previous, receive_clock, latest
        now = time.monotonic()
        if args.demo:
            while demo_index/fs <= now-demo_start:
                t = demo_index/fs
                gain = .25 if hold is not None and release is None else 1.
                ir = round(80000+800*gain*math.sin(2*math.pi*1.2*t))
                sample(0, demo_index, round(t*1e6), round(t*1e6), ir-10000, ir, demo_start+t)
                demo_index += 1
        else:
            import serial
            try:
                buffer += serial_port.read(65536)
            except (OSError, serial.SerialException) as exc:
                if files:
                    log_event('Serial disconnect')
                finish()
                animation.event_source.stop()
                status.set_text(f'Serial connection lost. Saved data retained. {exc}')
                return
            *rows, buffer = buffer.split(b'\n')
            for raw in rows:
                line = raw.decode('ascii', errors='replace').strip()
                try:
                    if line.startswith('#META '):
                        meta = json.loads(line[6:])
                        sensor_ready = (meta['protocol'] == 2 and meta['ppg_hz'] == fs
                                        and meta['ppg_bits'] == 18 and meta['ppg_ready'] is True)
                        diagnostic = '' if sensor_ready else 'PPG offline or incompatible firmware'
                    elif line.startswith('#BOOT'):
                        sensor_ready = False
                        receive_clock = None
                        window.clear()
                        previous = None
                        if files:
                            log_event('Board restart')
                    elif line.startswith('E,'):
                        diagnostic = line.split(',')[2]
                    elif sensor_ready and line.startswith('P,'):
                        record = tuple(map(int, line.split(',')[1:]))
                        _, _, _, read_us, _, _ = record
                        received = time.monotonic()
                        if receive_clock is None or read_us < receive_clock[0]:
                            receive_clock = read_us, received
                        board_delta = (read_us-receive_clock[0])/1e6
                        host_delta = received-receive_clock[1]
                        if board_delta > host_delta+.5 or host_delta > board_delta+2:
                            # Drop a burst of historical RouterBridge records before arming.
                            receive_clock = read_us, received
                            if start is None:
                                diagnostic = 'Waiting for live data after serial backlog'
                                latest = None
                                continue
                        if start is None and received-receive_clock[1] < 1:
                            continue
                        if diagnostic == 'Waiting for live data after serial backlog':
                            diagnostic = ''
                        sample(*record, received)
                except (ValueError, TypeError, KeyError, IndexError):
                    diagnostic = 'Malformed serial record'
        t = elapsed() if start is not None else now-demo_start
        if files and release is not None and t-release >= 32:
            log_event('End')
            finish()
            buttons[0].label.set_text('Saved')
        if wave:
            wave_line.set_data(*zip(*wave))
            wave_ax.set_xlim(max(0, t-8), max(8, t))
            wave_ax.relim()
            wave_ax.autoscale_view(scalex=False)
        if amplitudes:
            amp_line.set_data(*zip(*amplitudes))
            amp_ax.relim()
            amp_ax.autoscale_view(scalex=False)
        amp_ax.set_xlim(0, max(10, t+1))
        if start is None:
            message = 'Ready. Click Start recording.' if sensor_ready and latest and now-latest < 1 else 'Waiting for a live PPG stream'
        elif not files:
            message = f'Saved {run.name}. Close and reopen the recorder for the next trial.'
        elif release is not None:
            message = f'Post-release capture {max(0, t-release):.1f} s / 32 s'
        else:
            message = f'Recording {t:.1f} s. ' + ('Click Hold at the designated plateau.' if hold is None else 'Click Release at cuff release.')
        if diagnostic:
            message += ' | '+diagnostic
        if files and (latest is None or now-latest > 1):
            message += ' | NO RECENT PPG SAMPLES'
        status.set_text(message)

    animation = FuncAnimation(fig, update, interval=50, cache_frame_data=False)
    try:
        plt.show()
    finally:
        if files:
            log_event('Stopped early')
        finish()
        if serial_port:
            serial_port.close()
    return animation


if __name__ == '__main__':
    main()
