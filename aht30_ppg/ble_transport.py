"""Bluefruit BLE UART adapter for the existing acquisition monitor."""
import asyncio
import struct
import threading

from bleak import BleakClient, BleakScanner

SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
EVENTS = [
    "BLE_SENSOR_EVENT", "PPG_I2C_FAILED", "PPG_SAMPLE_READ_FAILED",
    "FIFO_OVERFLOW", "PPG_RECOVERED", "PPG_STARTED", "PPG_NOT_FOUND",
    "PPG_CONFIGURATION_FAILED", "AHT_NOT_FOUND_OR_WRITE_FAILED",
    "AHT_READ_FAILED", "AHT_CRC_FAILED", "SERVICE_GAP",
    "PPG_FIFO_RECOVERY_FAILED",
]


class BleTransport:
    def __init__(self, name="KO192-Sensors"):
        self.name = name
        self.buffer = bytearray()
        self.packets = bytearray()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.is_open = True
        self.status = "Scanning for Bluetooth sensor…"
        self.last_time = None
        self.epoch = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def emit(self, line):
        with self.lock:
            # Bound memory if the graph stops consuming notifications.
            if len(self.buffer) > 262144:
                self.buffer.clear()
                self.buffer.extend(b"E,0,BLE_HOST_BUFFER_OVERFLOW,0\n")
            self.buffer.extend((line + "\n").encode("ascii"))

    def notify(self, _sender, data):
        self.packets.extend(data)
        while len(self.packets) >= 24:
            if self.packets[:2] != b"\xa5\x5a" or self.packets[2] not in b"PTE":
                del self.packets[0]
                continue
            _, kind, segment, seq, stamp, a, b, read_stamp = struct.unpack(
                "<2sBBIIIII", self.packets[:24]
            )
            del self.packets[:24]
            if self.last_time is not None and stamp < self.last_time - 2**31:
                self.epoch += 2**32
            self.last_time = stamp
            stamp += self.epoch
            if kind == ord("P"):
                delta = ((read_stamp - (stamp % 2**32) + 2**31) % 2**32) - 2**31
                read_time = stamp + delta
                self.emit(f"P,{segment},{seq},{stamp},{read_time},{a},{b}")
            elif kind == ord("T"):
                signed = a if a < 2**31 else a - 2**32
                self.emit(f"T,{stamp},{signed/1000:.3f},{b/1000:.3f}")
            else:
                event = EVENTS[a] if a < len(EVENTS) else "BLE_SENSOR_EVENT"
                self.emit(f"E,{stamp},{event},{b}")

    async def acquire(self):
        while not self.stopped.is_set():
            try:
                self.status = "Scanning for Bluetooth sensor…"
                device = await BleakScanner.find_device_by_filter(
                    lambda device, advertisement: (
                        advertisement.local_name == self.name
                        or (device.name or "") == self.name
                    ),
                    timeout=8,
                )
                if device is None:
                    self.status = "Sensor not found. Retrying Bluetooth scan…"
                    continue
                self.status = "Connecting to " + self.name
                async with BleakClient(device, timeout=15) as client:
                    self.packets.clear()
                    self.last_time = None
                    self.epoch = 0
                    self.emit("#BOOT BLE_CONNECTION")
                    await client.start_notify(NOTIFY, self.notify)
                    self.status = "Bluetooth connected"
                    print("Bluetooth connected to " + self.name, flush=True)
                    while client.is_connected and not self.stopped.is_set():
                        await asyncio.sleep(.2)
                if not self.stopped.is_set():
                    self.emit("E,0,BLE_DISCONNECTED,0")
            except Exception as exc:
                self.status = f"Bluetooth retry: {exc}"
                print(self.status, flush=True)
                await asyncio.sleep(2)

    def _run(self):
        asyncio.run(self.acquire())

    def read(self, size):
        with self.lock:
            data = bytes(self.buffer[:size])
            del self.buffer[:size]
        return data

    def reset_input_buffer(self):
        with self.lock:
            self.buffer.clear()

    def close(self):
        self.stopped.set()
        self.is_open = False
