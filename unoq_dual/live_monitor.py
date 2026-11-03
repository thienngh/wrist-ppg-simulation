"""Three live plots from UnoQDualSensors.ino. See README before hardware use.
python live_monitor.py --list-ports
python live_monitor.py --port /dev/cu.usbmodemXXXX --site dorsal
python live_monitor.py --demo               # synthetic protocol test
python live_monitor.py --demo --check       # headless end-to-end verification
"""
import argparse
import json
import math
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfiltfilt


def temperature_c(a0, a1, meta):
    """A0 reference=half excitation; A1 lower-NTC sensor branch."""
    full = (1 << int(meta['adc_bits']))-1
    if not (0 < a0 < full and 0 < a1 < full) or 2*a0 <= a1:
        return float('nan')
    r = float(meta['r_fixed'])*a1/(2*a0-a1)
    inv_t = 1/(float(meta['t0_c'])+273.15)+math.log(r/float(meta['r0']))/float(meta['beta'])
    return 1/inv_t-273.15 if inv_t > 0 else float('nan')


def validate_meta(m):
    if m.get('protocol') != 2:
        raise ValueError('Unsupported protocol')
    for key in ['ppg_hz', 'ppg_bits', 'temp_hz', 'adc_bits', 'r_fixed', 'r0', 'beta']:
        if not math.isfinite(float(m[key])) or float(m[key]) <= 0:
            raise ValueError(f'Invalid metadata: {key}')
    if m.get('sensor_pin') != 'A1' or m.get('reference_pin') != 'A0':
        raise ValueError('Wrong thermistor branch mapping')
    if not 8 <= int(m['adc_bits']) <= 16 or int(m['ppg_bits']) != 18:
        raise ValueError('Unexpected ADC resolution')
    if float(m['ppg_hz']) <= 20:
        raise ValueError('PPG rate too low for 0.5–8 Hz processing')


class Processor:
    def __init__(self, smooth_ms=80., baseline_ms=800.):
        self.smooth_ms = float(smooth_ms)
        self.baseline_ms = float(baseline_ms)
        if self.smooth_ms < 0 or self.baseline_ms <= 0:
            raise ValueError('Smoothing must be nonnegative and baseline time constant positive')
        self.filtered = deque(maxlen=1000)
        self.smoothing_window = deque()
        self.baseline = None
        self.last_filter_t = None
        self.meta = None
        self.ppg = deque(maxlen=1000)
        self.temps = deque(maxlen=6000)
        self.amps = deque(maxlen=1200)
        self.last_segment = None
        self.last_seq = None
        self.last_amp_time = -math.inf
        self.bad_lines = 0
        self.gaps = 0
        self.message = 'Waiting for combined-sketch #META (announced every 5 s)'
        self.sos = None
        self.last_t = 0.

    def ingest(self, line):
        if line.startswith('#BOOT '):
            self.__init__(self.smooth_ms, self.baseline_ms)
            return
        if line.startswith('#META '):
            m = json.loads(line[6:]); validate_meta(m)
            if m == self.meta:
                return  # periodic configuration announcement, not a new run
            if self.meta is not None:  # changed settings invalidate comparisons
                self.ppg.clear(); self.temps.clear(); self.amps.clear(); self.reset_display_filter()
                self.last_segment = self.last_seq = None
                self.last_amp_time = -math.inf
            self.meta = m
            self.sos = butter(3, [.5, 8], fs=float(m['ppg_hz']), btype='bandpass', output='sos')
            self.message = 'Receiving data; amplitude requires 6 s of continuous PPG'
            return
        if line.startswith('#ERROR '):
            self.message = 'BOARD ERROR: '+line[7:]
            return
        if line.startswith('t='):
            self.message = 'Old temperature-only sketch detected; upload UnoQDualSensors.ino'
            return
        if not line or line.startswith('#'):
            return
        if self.meta is None:
            return
        row = line.split(',')
        if row[0] == 'E':
            self.gaps += 1
            self.message = 'Acquisition event: '+','.join(row[2:])
            if len(row) > 2 and row[2] in ('FIFO_OVERFLOW', 'SERVICE_GAP'):
                self.ppg.clear()
                self.reset_display_filter()
                self.last_segment = self.last_seq = None
                self.amps.append((int(row[1])/1e6, np.nan))
            return
        if row[0] == 'T':
            if len(row) != 7: raise ValueError('Invalid T record')
            _, seq, us, a0, a1, firmware_temp, missed = row
            t = int(us)/1e6
            value = temperature_c(int(a0), int(a1), self.meta)
            if self.temps and t < self.temps[-1][0]:
                meta = self.meta
                self.__init__(self.smooth_ms, self.baseline_ms)
                self.meta = meta
                self.sos = butter(3, [.5, 8], fs=float(meta['ppg_hz']), btype='bandpass', output='sos')
            self.temps.append((t, value))
            self.last_t = max(self.last_t, t)
            full = (1 << int(self.meta['adc_bits']))-1
            vref = float(self.meta.get('adc_reference_v', 3.3))
            supply = float(self.meta.get('bridge_supply_v', 3.3))
            self.message = (f'Bridge nominal {supply:.1f} V | A0 {int(a0)*vref/full:.3f} V'
                            f' | A1 {int(a1)*vref/full:.3f} V | expected A0 ~{supply/2:.2f} V')
            if not np.isfinite(value):
                self.message += ' | INVALID: ADC rail or bridge ratio; check wiring'
        elif row[0] == 'P':
            if len(row) != 7: raise ValueError('Invalid P record')
            _, segment, seq, us, read_us, red, ir = row
            segment, seq = int(segment), int(seq)
            t, ir, red = int(us)/1e6, int(ir), int(red)
            if not (0 <= ir < 2**18 and 0 <= red < 2**18):
                raise ValueError('PPG outside 18-bit range')
            if self.last_segment is not None and (segment != self.last_segment or seq != self.last_seq+1):
                self.ppg.clear(); self.reset_display_filter(); self.gaps += 1
                self.amps.append((t, np.nan))
            if self.ppg and t <= self.ppg[-1][0]:
                raise ValueError('Non-monotonic sample clock')
            self.last_segment, self.last_seq = segment, seq
            self.ppg.append((t, red, ir))
            self.update_display_filter(t, ir)
            self.last_t = max(self.last_t, t)
            self.update_amplitude(t)
        else:
            raise ValueError('Unknown record')

    def reset_display_filter(self):
        self.filtered.clear()
        self.smoothing_window.clear()
        self.baseline = None
        self.last_filter_t = None

    def update_display_filter(self, t, value):
        # Based on supplied index.html: causal one-pole DC tracker, then
        # trailing moving average. No normalization, spike replacement or beat inference.
        dt = 1/float(self.meta['ppg_hz']) if self.last_filter_t is None else t-self.last_filter_t
        if self.baseline is None:
            self.baseline = float(value)
        self.baseline += (1-math.exp(-dt/(self.baseline_ms/1000)))*(value-self.baseline)
        ac = float(value)-self.baseline
        taps = max(1, round(self.smooth_ms*float(self.meta['ppg_hz'])/1000))
        self.smoothing_window.append(ac)
        while len(self.smoothing_window) > taps:
            self.smoothing_window.popleft()
        smoothed = float(np.mean(self.smoothing_window))
        self.filtered.append((t, ac, smoothed))
        self.last_filter_t = t

    def update_amplitude(self, t):
        n = int(round(4*float(self.meta['ppg_hz'])))
        if len(self.ppg) < n or t-self.last_amp_time < .999:
            return
        data = np.asarray(list(self.ppg)[-n:])
        self.last_amp_time = t
        expected = 1/float(self.meta['ppg_hz'])
        if not np.allclose(np.diff(data[:, 0]), expected, atol=1e-5):
            self.amps.append((t-1, np.nan)); return
        if np.any((data[:, 2] <= 1) | (data[:, 2] >= 262142)):
            self.amps.append((t-1, np.nan))
            self.message = 'IR clipping: amplitude suppressed; adjust LED/range/contact'
            return
        # Reject filter edges: use a 4-second interior with 1 s padding when ready.
        if len(self.ppg) < n + int(2*float(self.meta['ppg_hz'])):
            return
        data = np.asarray(list(self.ppg)[-(n+int(2*float(self.meta['ppg_hz']))):])
        if np.any((data[:, 2] <= 1) | (data[:, 2] >= 262142)):
            self.amps.append((t-1, np.nan)); return
        filtered = sosfiltfilt(self.sos, data[:, 2])
        pad = int(float(self.meta['ppg_hz']))
        interior = filtered[pad:-pad]
        amplitude = float(np.percentile(interior, 95)-np.percentile(interior, 5))
        # Timestamp at the end of the interior: estimate is delayed by 1 s.
        self.amps.append((float(data[-pad-1, 0]), amplitude))
        self.message = 'Window AC amplitude (95th−5th percentile); motion can mimic perfusion changes'


def demo_lines():
    meta = dict(protocol=2, ppg_hz=50, ppg_bits=18, temp_hz=5, adc_bits=14,
                r_fixed=4700, r0=4700, beta=3950, t0_c=25,
                sensor_pin='A1', reference_pin='A0', led_code=8, ppg_ready=True, adc_sample_hz=200, fifo_average=4)
    yield '#META '+json.dumps(meta)
    rng = np.random.default_rng(192)
    for i in range(30000):
        t = (i+1)/50
        ac = (np.sin(2*np.pi*1.2*t)+.25*np.sin(4*np.pi*1.2*t))
        ac *= 700*(1+.4*(1-np.exp(-t/20)))
        ir = int(80000+ac+rng.normal(0, 12))
        red = int(65000+.7*ac+rng.normal(0, 12))
        yield f'P,0,{i},{round(t*1e6)},{round(t*1e6)+1000},{red},{ir}'
        if i % 10 == 0:
            temp = 32+t/600  # 1°C over 10 minutes, per the proposed experiment
            r = 4700*np.exp(3950*(1/(temp+273.15)-1/298.15))
            adc = round(16383*r/(4700+r))
            yield f'T,{i//10},{round(t*1e6)},8192,{adc},{temp:.3f},0'


def headless_check(target):
    p = Processor()
    for line in demo_lines(): p.ingest(line)
    assert len(p.temps) == 3000 and len(p.amps) > 40
    assert abs(temperature_c(8192, 8192, p.meta)-25) < 1e-10
    assert math.isnan(temperature_c(0, 8192, p.meta))
    assert math.isnan(temperature_c(8192, 16383, p.meta))
    assert p.temps[-1][1] > p.temps[0][1]+.8
    assert p.amps[-1][1] > p.amps[0][1]
    render_monitor(p, target=target)
    p.ingest('P,1,0,601000000,601001000,65000,80000')
    assert len(p.ppg) == 1 and p.gaps == 1
    print('PASS: protocol, NTC conversion, rail rejection, amplitude rise and gap reset')


def render_monitor(processor, args=None, incoming=None, stop=None, target=None):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    fig, axes = plt.subplots(3, 1, figsize=(11, 9))
    fig.subplots_adjust(left=.12, right=.97, top=.89, bottom=.13, hspace=.55)
    site = args.site if args else 'demo'
    fig.suptitle(f'Wrist sensor acquisition · {site}\n'+('SYNTHETIC DEMO' if not args or args.demo else 'Measured data'), fontsize=13)
    titles = ['Temperature · A1=NTC, A0=reference',
              f'IR PPG · {processor.baseline_ms:g} ms baseline tracking + {processor.smooth_ms:g} ms smoothing',
              'PPG amplitude change · 95th−5th percentile, 4-second interior']
    units = ['Temperature (°C)', 'IR AC (counts)', 'IR AC amplitude (counts)']
    lines = []
    for ax, title, unit in zip(axes, titles, units):
        ax.set(title=title, xlabel='Board time (s)', ylabel=unit)
        ax.grid(alpha=.2)
        line, = ax.plot([], [], linewidth=1.2)
        lines.append(line)
    raw_line, = axes[1].plot([], [], color='0.65', alpha=.65, linewidth=.8, label='Before moving average')
    lines[1].set_label(f'{processor.smooth_ms:g} ms moving average')
    lines[1].set_zorder(3)
    axes[1].legend(loc='upper left', fontsize=8)
    note = fig.text(.12, .035, '', fontsize=9, wrap=True)

    def update(_):
        if incoming:
            for i in range(5000):
                try: line = incoming.get_nowait()
                except queue.Empty: break
                if line.startswith('!HOST '):
                    processor.message = line[6:]; continue
                processor.last_host_rx = time.monotonic()
                try: processor.ingest(line)
                except (ValueError, KeyError, TypeError, OverflowError):
                    processor.bad_lines += 1
        now = processor.last_t
        for ax, line, values in zip([axes[0], axes[2]], [lines[0], lines[2]], [processor.temps, processor.amps]):
            data = np.asarray(values)
            if data.size:
                line.set_data(data[:, 0], data[:, 1])
                ax.relim(); ax.autoscale_view(scalex=False)
            first = float(data[0, 0]) if data.size else now
            ax.set_xlim(max(first, now-600), max(first+10, now))
        data = np.asarray(processor.filtered)
        if data.size:
            selected = data[data[:, 0] >= now-8]
            if selected.size:
                lines[1].set_data(selected[:, 0], selected[:, 2])
                raw_line.set_data(selected[:, 0], selected[:, 1])
                axes[1].relim(); axes[1].autoscale_view(scalex=False)
        else:
            lines[1].set_data([], []); raw_line.set_data([], [])
        axes[1].set_xlim(max(0, now-8), max(8, now))
        m = processor.meta
        label = (f"PPG {m['ppg_hz']} Hz / {m['ppg_bits']} bit; NTC {m['temp_hz']} Hz / {m['adc_bits']} bit"
                 if m else 'Metadata pending')
        if processor.ppg:
            label += f' | latest IR {int(processor.ppg[-1][2])} counts'
        if m and not m.get('ppg_ready', False):
            label += ' | PPG OFFLINE: check board events / wiring'
        if args and hasattr(processor, 'last_host_rx') and time.monotonic()-processor.last_host_rx > 3:
            label += ' | NO NEW DATA >3 s'
        note.set_text(label+f' | events {processor.gaps}, malformed {processor.bad_lines}\n'+processor.message)
        return lines

    if target:
        update(0); fig.savefig(target, dpi=150); plt.close(fig); return
    fig.canvas.mpl_connect('close_event', lambda _: stop.set())
    ani = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
    plt.show()
    return ani


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--site', choices=['dorsal', 'volar'], default='dorsal')
    parser.add_argument('--smooth-ms', type=float, default=80., help='Display moving-average span in milliseconds')
    parser.add_argument('--baseline-ms', type=float, default=800., help='Display DC-tracker time constant in milliseconds')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--list-ports', action='store_true')
    parser.add_argument('--log-dir', type=Path, default=Path(__file__).resolve().parent/'recordings')
    args = parser.parse_args()
    if args.list_ports:
        from serial.tools import list_ports
        for port in list_ports.comports(): print(port.device, port.description)
        return
    if args.check:
        headless_check(Path(__file__).resolve().parent/'monitor_demo.png'); return
    if not args.demo and not args.port:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports() if 'uno q' in (p.description or '').lower()]
        if len(ports) != 1: parser.error('Use --list-ports, then --port PORT to select the UNO Q')
        args.port = ports[0]
    incoming = queue.Queue(maxsize=10000)
    stop = threading.Event()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log = args.log_dir/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+args.site+'.txt')
    processor = Processor(args.smooth_ms, args.baseline_ms)

    def reader():
        connection = None
        try:
            if not args.demo:
                import serial
                connection = serial.Serial(args.port, args.baud, timeout=.05)
                connection.reset_input_buffer()
                # The UNO Q router can retain whole runs upstream of USB. Detect
                # catch-up from readout-clock progress vs wall time, rather than
                # requiring silence on a continuously transmitting sensor.
                deadline = time.monotonic()+20
                window_start = time.monotonic()
                first_board = last_board = None
                cached_meta = None
                while time.monotonic() < deadline:
                    startup = connection.readline().decode('ascii', errors='replace').strip()
                    if startup.startswith('#META '): cached_meta = startup
                    if not startup:
                        break
                    fields = startup.split(',')
                    try:
                        board = (int(fields[4]) if fields[0] == 'P' else
                                 int(fields[2]) if fields[0] == 'T' else None)
                    except (ValueError, IndexError): board = None
                    if board is not None:
                        if first_board is None or (last_board is not None and board < last_board-1000000):
                            first_board = board
                            window_start = time.monotonic()
                        last_board = board
                        elapsed = time.monotonic()-window_start
                        if elapsed >= .5:
                            if (last_board-first_board)/1e6 <= 1.5*elapsed:
                                break
                            first_board = last_board
                            window_start = time.monotonic()
                else: raise RuntimeError('Historical output still arriving too fast after20s; reconnect/reset board')
                connection.timeout = .2
            with log.open('w', buffering=1) as f:
                f.write('#HOST '+json.dumps(dict(site=args.site, demo=args.demo, wall_time=datetime.now().isoformat(), smooth_ms=args.smooth_ms, baseline_ms=args.baseline_ms, amplitude_filter='Butterworth3_0.5-8Hz_zero_phase_Q95-Q5'))+'\n')
                if not args.demo and cached_meta:
                    f.write(cached_meta+'\n'); incoming.put(cached_meta)
                start = time.monotonic()
                for_demo = iter(demo_lines()) if args.demo else None
                demo_time = 0.
                while not stop.is_set():
                    if args.demo:
                        try: line = next(for_demo)
                        except StopIteration:
                            incoming.put('!HOST Demo complete; close the plot window'); break
                        if line.startswith('P,'):
                            demo_time = int(line.split(',')[3])/1e6
                            if stop.wait(max(0, start+demo_time-time.monotonic())): break
                    else:
                        data = connection.readline()
                        if not data: continue
                        line = data.decode('ascii', errors='replace').strip()
                    f.write(line+'\n')  # raw metadata, errors and both optical channels retained
                    try: incoming.put(line, timeout=.5)
                    except queue.Full:
                        # Raw file remains complete; do not silently show a discontinuous signal.
                        stop.set()
                        print('Display queue full. Raw log retained; close and restart monitor.')
                        break
        except Exception as exc:
            try: incoming.put('!HOST Acquisition stopped: '+str(exc), timeout=.1)
            except queue.Full: pass
        finally:
            if connection: connection.close()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    print('Raw recording:', log.resolve())
    try: render_monitor(processor, args, incoming, stop)
    finally:
        stop.set(); thread.join(timeout=3)


if __name__ == '__main__':
    main()
