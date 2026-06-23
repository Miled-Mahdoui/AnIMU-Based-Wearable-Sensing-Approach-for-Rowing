#!/usr/bin/env python3
"""Live dashboard for the rowing IMU prototype.

The dashboard can read one IMU over USB/BLE or two IMUs over BLE. In dual mode
it builds a relative SEAT - BOAT stream, which is the main signal used by the
live graphs, stroke metrics, and voice feedback.
"""
import argparse
import asyncio
import csv
import glob
import json
import math
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean, pstdev
from urllib.parse import urlparse


DEFAULT_BAUD = 115200
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_WINDOW_S = 30.0
DEFAULT_RECORD_DIR = Path("/Users/mahdoui/Downloads")
NUS_TX_CHARACTERISTIC_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_SEAT_BLE_ADDRESS = "6044EEC2-62E9-A3B2-75AC-95D06200C12B"
DEFAULT_BOAT_BLE_ADDRESS = "863A5D89-2A83-9CD0-B789-16C335C872D2"
SMOOTHING_WINDOW = 9
MIN_STROKE_DISTANCE_S = 0.85
PEAK_STD_FACTOR = 0.35
MIN_PEAK_THRESHOLD = 0.8
CSV_FIELDS = (
    "device_id",
    "sequence",
    "time_us",
    "acc_x_ms2",
    "acc_y_ms2",
    "acc_z_ms2",
    "gyro_x_rads",
    "gyro_y_rads",
    "gyro_z_rads",
)


def import_serial():
    try:
        import serial
    except ImportError as error:
        raise SystemExit(
            "pyserial is missing. Install it with:\n"
            "  /usr/local/bin/python3 -m pip install pyserial"
        ) from error
    return serial


def import_bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise SystemExit(
            "bleak is missing. Install it with:\n"
            "  /usr/local/bin/python3 -m pip install bleak"
        ) from error

    return BleakClient, BleakScanner


def find_serial_ports():
    patterns = (
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
    )
    ports = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


def ble_device_label(device):
    name = device.name or "unknown"
    return f"{name} [{device.address}]"


async def scan_ble_devices(timeout_s):
    _, BleakScanner = import_bleak()
    devices = await BleakScanner.discover(timeout=timeout_s)
    return sorted(devices, key=lambda device: (device.name or "", device.address))


async def choose_ble_device(requested_name, requested_address, timeout_s):
    devices = await scan_ble_devices(timeout_s)

    if requested_address:
        for device in devices:
            if device.address.lower() == requested_address.lower():
                return device
        raise RuntimeError(f"No BLE device with address {requested_address} found")

    candidates = [
        device
        for device in devices
        if (device.name or "").startswith("Rowing-")
    ]

    if requested_name:
        candidates = [
            device
            for device in devices
            if requested_name.lower() in (device.name or "").lower()
        ]

    if not candidates:
        raise RuntimeError("No Rowing-* BLE device found")

    return candidates[0]


def parse_measurement(line):
    """Parse one firmware CSV line into the internal dashboard sample format.

    The current firmware sends 9 columns. Older test firmware sent 12 columns
    including magnetometer values; extra columns are ignored so old devices can
    still be used during demos.
    """
    line = line.strip()
    if not line or line.startswith("Rowing ") or line.startswith("Device:"):
        return None
    if line.startswith("IMU erkannt") or line.startswith("Logdatei:"):
        return None
    if line.startswith("Messung startet") or line.startswith("device_id,"):
        return None

    values = next(csv.reader([line]))
    if len(values) < len(CSV_FIELDS):
        return None
    values = values[:len(CSV_FIELDS)]

    row = dict(zip(CSV_FIELDS, values))
    try:
        return {
            "device_id": row["device_id"],
            "sequence": int(row["sequence"]),
            "time_us": int(row["time_us"]),
            "raw_csv": values,
            "seat_forward_acc": float(row["acc_x_ms2"]),
            "seat_lateral_acc": float(row["acc_y_ms2"]),
            "seat_vertical_acc": float(row["acc_z_ms2"]),
            "seat_roll_rate": float(row["gyro_x_rads"]),
            "seat_pitch_rate": float(row["gyro_y_rads"]),
            "seat_yaw_rate": float(row["gyro_z_rads"]),
        }
    except ValueError:
        return None


def moving_average(values, window):
    """Return a causal moving average over the latest `window` values."""
    if not values:
        return []
    window = max(1, min(window, len(values)))
    result = []
    queue = deque()
    running = 0.0
    for value in values:
        queue.append(value)
        running += value
        if len(queue) > window:
            running -= queue.popleft()
        result.append(running / len(queue))
    return result


def detect_strokes(samples, min_distance_s=MIN_STROKE_DISTANCE_S):
    """Detect candidate stroke boundaries from smoothed forward acceleration.

    Tuning notes:
    - MIN_STROKE_DISTANCE_S rejects peaks that are too close together.
    - PEAK_STD_FACTOR scales the threshold with movement intensity.
    - MIN_PEAK_THRESHOLD prevents noise from being counted while the sensor rests.
    """
    if len(samples) < 20:
        return []

    times = [(sample["time_us"] - samples[0]["time_us"]) / 1_000_000.0 for sample in samples]
    values = [sample["seat_forward_acc"] for sample in samples]
    baseline = mean(values[: min(100, len(values))])
    centered = [value - baseline for value in values]
    smoothed = moving_average(centered, SMOOTHING_WINDOW)
    signal_std = pstdev(smoothed) if len(smoothed) > 1 else 0.0
    threshold = max(PEAK_STD_FACTOR * signal_std, MIN_PEAK_THRESHOLD)
    peaks = []
    last_peak_time = -1e9

    for index in range(1, len(smoothed) - 1):
        value = smoothed[index]
        if value < threshold:
            continue
        if value < smoothed[index - 1] or value < smoothed[index + 1]:
            continue
        if times[index] - last_peak_time < min_distance_s:
            if peaks and value > smoothed[peaks[-1]]:
                peaks[-1] = index
                last_peak_time = times[index]
            continue
        peaks.append(index)
        last_peak_time = times[index]

    return peaks


def integrate_velocity(times, acceleration, correct_drift):
    """Integrate acceleration into a relative speed proxy.

    This is intentionally labelled as a proxy, not real m/s. IMU integration
    drifts quickly without a trusted position/force reference.
    """
    if not acceleration:
        return []

    velocity = [0.0]
    for index in range(1, len(acceleration)):
        dt = times[index] - times[index - 1]
        area = 0.5 * (acceleration[index] + acceleration[index - 1]) * dt
        velocity.append(velocity[-1] + area)

    if correct_drift and len(velocity) > 1:
        # Completed stroke segments should approximately start/end at rest.
        # Removing a straight-line end drift makes the shape easier to compare.
        drift_per_sample = velocity[-1] / (len(velocity) - 1)
        velocity = [value - drift_per_sample * index for index, value in enumerate(velocity)]

    return velocity


def speed_proxy_metrics(times, acceleration):
    """Calculate velocity-proxy, peak phase, and jerk-based smoothness."""
    smoothed = moving_average(
        [value - mean(acceleration[: min(50, len(acceleration))]) for value in acceleration],
        7,
    ) if acceleration else []
    velocity = integrate_velocity(times, smoothed, correct_drift=True)
    if not velocity or not times:
        return {
            "velocity": velocity,
            "speed_proxy": 0.0,
            "peak_time_s": 0.0,
            "peak_phase_pct": 0.0,
            "smoothness_jerk_rms": 0.0,
        }

    peak_index = max(range(len(velocity)), key=lambda index: abs(velocity[index]))
    duration_s = max(0.001, times[-1] - times[0])
    jerks = []
    for index in range(1, len(smoothed)):
        dt = times[index] - times[index - 1]
        if dt > 1e-9:
            jerks.append((smoothed[index] - smoothed[index - 1]) / dt)

    return {
        "velocity": velocity,
        "speed_proxy": abs(velocity[peak_index]),
        "peak_time_s": times[peak_index],
        "peak_phase_pct": (times[peak_index] - times[0]) / duration_s * 100.0,
        "smoothness_jerk_rms": rms(jerks),
    }


def resample_to_phase(values, phase_count=101):
    """Resample a stroke segment to 0-100 percent for comparison."""
    if not values:
        return []
    if len(values) == 1:
        return [values[0]] * phase_count

    result = []
    source_max = len(values) - 1
    for phase in range(phase_count):
        position = source_max * phase / (phase_count - 1)
        left = int(math.floor(position))
        right = min(source_max, left + 1)
        ratio = position - left
        result.append(values[left] + (values[right] - values[left]) * ratio)
    return result


def average_phase_series(series_list):
    if not series_list:
        return []
    length = len(series_list[0])
    return [mean(series[index] for series in series_list) for index in range(length)]


def rms(values):
    if not values:
        return 0.0
    return math.sqrt(mean(value * value for value in values))


def build_stroke_segment(samples, start_index, end_index, complete):
    """Build time-based and phase-normalized data for one stroke segment."""
    if end_index <= start_index:
        return None

    segment = samples[start_index:end_index + 1]
    times = [(sample["time_us"] - segment[0]["time_us"]) / 1_000_000.0 for sample in segment]
    forward = [sample["seat_forward_acc"] for sample in segment]
    baseline = mean(forward[: min(20, len(forward))])
    centered = [value - baseline for value in forward]
    smoothed = moving_average(centered, 7)
    velocity = integrate_velocity(times, smoothed, correct_drift=complete)

    # Relative power-transfer proxy: forward acceleration times relative speed.
    # Negative values are clipped because this panel is meant to highlight drive
    # contribution, not braking/recovery work.
    power = [max(0.0, acc * speed) for acc, speed in zip(smoothed, velocity)]

    return {
        "duration_s": times[-1] if times else 0.0,
        "time": times,
        "acc_time": smoothed,
        "velocity_time": velocity,
        "power_time": power,
        "acc": resample_to_phase(smoothed),
        "velocity": resample_to_phase(velocity),
        "power": resample_to_phase(power),
        "peak_power": max(power) if power else 0.0,
    }


def build_stroke_state(samples):
    """Create the live stroke state for current, recent, and average strokes.

    The 0-100 phase axis is normalized time between two detected peaks. It is
    not force, power, or seat travel percentage.
    """
    phase = list(range(101))
    empty_segment = {
        "duration_s": 0.0,
        "time": [],
        "acc_time": [],
        "velocity_time": [],
        "power_time": [],
        "acc": [],
        "velocity": [],
        "power": [],
        "peak_power": 0.0,
    }
    empty = {
        "phase": phase,
        "current": empty_segment,
        "last": empty_segment,
        "average": {"acc": [], "velocity": [], "power": []},
        "history": [],
        "metrics": {
            "current_phase": 0.0,
            "current_elapsed": 0.0,
            "last_duration": 0.0,
            "last_peak_power": 0.0,
            "avg_peak_power": 0.0,
        },
    }

    if len(samples) < 30:
        return empty

    peaks = detect_strokes(samples)
    if not peaks:
        return empty

    complete_segments = []
    for start, end in zip(peaks, peaks[1:]):
        segment = build_stroke_segment(samples, start, end, complete=True)
        if segment:
            complete_segments.append(segment)

    current = build_stroke_segment(samples, peaks[-1], len(samples) - 1, complete=False) or empty_segment
    last = complete_segments[-1] if complete_segments else empty_segment
    recent = complete_segments[-8:]

    avg_acc = average_phase_series([segment["acc"] for segment in recent])
    avg_velocity = average_phase_series([segment["velocity"] for segment in recent])
    avg_power = average_phase_series([segment["power"] for segment in recent])

    current_elapsed = current["duration_s"]
    if recent:
        avg_duration = mean(segment["duration_s"] for segment in recent)
        current_phase = min(100.0, current_elapsed / max(0.001, avg_duration) * 100.0)
    else:
        current_phase = 0.0

    return {
        "phase": phase,
        "current": current,
        "last": last,
        "average": {
            "acc": avg_acc,
            "velocity": avg_velocity,
            "power": avg_power,
        },
        "history": [
            {
                "acc": segment["acc"],
                "velocity": segment["velocity"],
                "power": segment["power"],
                "duration_s": segment["duration_s"],
            }
            for segment in recent[-5:]
        ],
        "metrics": {
            "current_phase": current_phase,
            "current_elapsed": current_elapsed,
            "last_duration": last["duration_s"],
            "last_peak_power": last["peak_power"],
            "avg_peak_power": max(avg_power) if avg_power else 0.0,
        },
    }


def next_capture_path(directory):
    """Choose the next non-existing live_capture_XXX.csv path."""
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, 10000):
        path = directory / f"live_capture_{index:03d}.csv"
        if not path.exists():
            return path
    raise RuntimeError("No free live_capture filename available")


class LiveState:
    """Shared state between the data-reader thread and the HTTP dashboard."""
    def __init__(
        self,
        source,
        requested_port,
        baud,
        window_s,
        record_dir,
        ble_name,
        ble_address,
        scan_time_s,
    ):
        self.source = source
        self.requested_port = requested_port
        self.baud = baud
        self.window_s = window_s
        self.record_dir = record_dir
        self.ble_name = ble_name
        self.ble_address = ble_address
        self.scan_time_s = scan_time_s
        self.samples = deque()
        self.status = "starting"
        self.active_connection = None
        self.last_line = ""
        self.last_error = ""
        self.recording = False
        self.record_path = None
        self.record_file = None
        self.record_writer = None
        self.marker_path = None
        self.marker_file = None
        self.marker_writer = None
        self.markers = []
        self.record_count = 0
        self.speech_process = None
        self.lock = threading.Lock()

    def add_sample(self, sample):
        with self.lock:
            self.samples.append(sample)
            newest_time = sample["time_us"]
            # Keep only a rolling time window so the browser remains responsive.
            while self.samples and (newest_time - self.samples[0]["time_us"]) / 1_000_000.0 > self.window_s:
                self.samples.popleft()
            self.status = "live"
            if self.recording and self.record_writer:
                self.record_writer.writerow(sample["raw_csv"])
                self.record_count += 1
                if self.record_count % 50 == 0:
                    self.record_file.flush()

    def set_status(self, status, error=""):
        with self.lock:
            self.status = status
            self.last_error = error

    def set_connection(self, connection):
        with self.lock:
            self.active_connection = connection

    def set_line(self, line):
        with self.lock:
            self.last_line = line

    def speak(self, text):
        clean_text = " ".join(str(text).split())[:240]
        if not clean_text:
            return False
        try:
            # Avoid a long speech queue: cancel the previous macOS say process
            # and speak only the newest feedback sentence.
            if self.speech_process and self.speech_process.poll() is None:
                self.speech_process.terminate()
            self.speech_process = subprocess.Popen(
                ["/usr/bin/say", "-r", "125", clean_text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError as error:
            self.set_status(self.status, f"Speech failed: {error}")
            return False

    def start_recording(self):
        with self.lock:
            if self.recording:
                return self.record_path

            self.record_path = next_capture_path(self.record_dir)
            self.marker_path = self.record_path.with_name(f"{self.record_path.stem}_markers.csv")
            self.record_file = self.record_path.open("w", newline="")
            self.record_writer = csv.writer(self.record_file)
            self.record_writer.writerow(CSV_FIELDS)
            self.marker_file = self.marker_path.open("w", newline="")
            self.marker_writer = csv.writer(self.marker_file)
            self.marker_writer.writerow(("host_time_s", "time_us", "sequence", "label"))
            self.markers = []
            self.record_count = 0
            self.recording = True
            return self.record_path

    def stop_recording(self):
        with self.lock:
            self.recording = False
            if self.record_file:
                self.record_file.flush()
                self.record_file.close()
            if self.marker_file:
                self.marker_file.flush()
                self.marker_file.close()
            self.record_file = None
            self.record_writer = None
            self.marker_file = None
            self.marker_writer = None

    def add_marker(self, label):
        with self.lock:
            if not self.recording or not self.marker_writer:
                return False

            latest = self.samples[-1] if self.samples else None
            marker = {
                "host_time_s": time.time(),
                "time_us": latest["time_us"] if latest else None,
                "sequence": latest["sequence"] if latest else None,
                "label": label,
            }
            self.markers.append(marker)
            self.marker_writer.writerow((
                f"{marker['host_time_s']:.3f}",
                marker["time_us"] if marker["time_us"] is not None else "",
                marker["sequence"] if marker["sequence"] is not None else "",
                marker["label"],
            ))
            self.marker_file.flush()
            return True

    def snapshot(self):
        with self.lock:
            samples = list(self.samples)
            status = self.status
            active_connection = self.active_connection
            last_line = self.last_line
            last_error = self.last_error
            recording = self.recording
            record_path = str(self.record_path) if self.record_path else ""
            marker_path = str(self.marker_path) if self.marker_path else ""
            record_count = self.record_count
            markers = list(self.markers)

        metrics = summarize_samples(samples)
        series = build_series(samples)
        stroke = build_stroke_state(samples)
        live_markers = build_live_markers(samples, markers)
        return {
            "status": status,
            "source": self.source,
            "port": active_connection,
            "baud": self.baud,
            "window_s": self.window_s,
            "last_line": last_line,
            "last_error": last_error,
            "recording": recording,
            "record_path": record_path,
            "marker_path": marker_path,
            "record_count": record_count,
            "metrics": metrics,
            "series": series,
            "stroke": stroke,
            "markers": live_markers,
        }


def choose_port(requested_port):
    if requested_port:
        return requested_port
    ports = find_serial_ports()
    return ports[0] if ports else None


def make_relative_sample(seat, boat, sequence, time_us):
    relative = {
        "device_id": "SEAT-BOAT",
        "sequence": sequence,
        "time_us": time_us,
        "seat_source_forward_acc": seat["seat_forward_acc"],
        "boat_source_forward_acc": boat["seat_forward_acc"],
        "seat_source_lateral_acc": seat["seat_lateral_acc"],
        "boat_source_lateral_acc": boat["seat_lateral_acc"],
        "seat_source_vertical_acc": seat["seat_vertical_acc"],
        "boat_source_vertical_acc": boat["seat_vertical_acc"],
        "seat_source_roll_rate": seat["seat_roll_rate"],
        "boat_source_roll_rate": boat["seat_roll_rate"],
        "seat_source_pitch_rate": seat["seat_pitch_rate"],
        "boat_source_pitch_rate": boat["seat_pitch_rate"],
        "seat_source_yaw_rate": seat["seat_yaw_rate"],
        "boat_source_yaw_rate": boat["seat_yaw_rate"],
        "seat_forward_acc": seat["seat_forward_acc"] - boat["seat_forward_acc"],
        "seat_lateral_acc": seat["seat_lateral_acc"] - boat["seat_lateral_acc"],
        "seat_vertical_acc": seat["seat_vertical_acc"] - boat["seat_vertical_acc"],
        "seat_roll_rate": seat["seat_roll_rate"] - boat["seat_roll_rate"],
        "seat_pitch_rate": seat["seat_pitch_rate"] - boat["seat_pitch_rate"],
        "seat_yaw_rate": seat["seat_yaw_rate"] - boat["seat_yaw_rate"],
    }
    relative["raw_csv"] = [
        relative["device_id"],
        str(relative["sequence"]),
        str(relative["time_us"]),
        f"{relative['seat_forward_acc']:.6f}",
        f"{relative['seat_lateral_acc']:.6f}",
        f"{relative['seat_vertical_acc']:.6f}",
        f"{relative['seat_roll_rate']:.6f}",
        f"{relative['seat_pitch_rate']:.6f}",
        f"{relative['seat_yaw_rate']:.6f}",
        "0.000000",
        "0.000000",
        "0.000000",
    ]
    return relative


def serial_worker(state):
    serial = import_serial()

    while True:
        port = choose_port(state.requested_port)
        if not port:
            state.set_connection(None)
            state.set_status("waiting", "No USB serial port found")
            time.sleep(1.0)
            continue

        state.set_connection(port)
        try:
            state.set_status("opening")
            with serial.Serial(port, baudrate=state.baud, timeout=1) as connection:
                connection.reset_input_buffer()
                state.set_status("live")
                while True:
                    raw_line = connection.readline()
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    sample = parse_measurement(line)
                    if sample is None:
                        if line:
                            state.set_line(line)
                        continue
                    state.set_line(line)
                    state.add_sample(sample)
        except Exception as error:
            state.set_status("error", str(error))
            time.sleep(1.0)


async def ble_worker_async(state):
    BleakClient, _ = import_bleak()

    while True:
        try:
            state.set_connection(None)
            state.set_status("scanning")
            device = await choose_ble_device(
                state.ble_name,
                state.ble_address,
                state.scan_time_s,
            )
            state.set_connection(ble_device_label(device))
            state.set_status("connecting")

            line_queue = asyncio.Queue()
            partial_line = ""

            def handle_notification(_sender, data):
                nonlocal partial_line
                partial_line += data.decode("utf-8", errors="replace")

                while "\n" in partial_line:
                    line, partial_line = partial_line.split("\n", 1)
                    line_queue.put_nowait(line.strip())

            async with BleakClient(device) as client:
                await client.start_notify(
                    NUS_TX_CHARACTERISTIC_UUID,
                    handle_notification,
                )
                state.set_status("live")

                while True:
                    line = await line_queue.get()
                    sample = parse_measurement(line)
                    if sample is None:
                        if line:
                            state.set_line(line)
                        continue
                    state.set_line(line)
                    state.add_sample(sample)
        except Exception as error:
            state.set_status("error", str(error))
            await asyncio.sleep(1.0)


def ble_worker(state):
    asyncio.run(ble_worker_async(state))


class DualBleState(LiveState):
    """State for two BLE devices and the derived SEAT - BOAT stream."""
    def __init__(
        self,
        window_s,
        record_dir,
        seat_address,
        boat_address,
        scan_time_s,
    ):
        super().__init__(
            "dual_ble",
            None,
            DEFAULT_BAUD,
            window_s,
            record_dir,
            None,
            None,
            scan_time_s,
        )
        self.seat_address = seat_address
        self.boat_address = boat_address
        self.device_samples = {"SEAT": deque(), "BOAT": deque()}
        self.device_connections = {"SEAT": None, "BOAT": None}
        self.device_status = {"SEAT": "starting", "BOAT": "starting"}
        self.relative_sequence = 0
        self.dual_start_host_s = None

    def set_device_connection(self, role, connection):
        with self.lock:
            self.device_connections[role] = connection
            self.active_connection = (
                f"SEAT: {self.device_connections['SEAT'] or '-'} | "
                f"BOAT: {self.device_connections['BOAT'] or '-'}"
            )

    def set_device_status(self, role, status, error=""):
        with self.lock:
            self.device_status[role] = status
            if error:
                self.last_error = f"{role}: {error}"
            if all(value == "live" for value in self.device_status.values()):
                self.status = "live"
            elif any(value == "error" for value in self.device_status.values()):
                self.status = "error"
            else:
                self.status = "connecting"

    def add_device_sample(self, role, sample):
        host_time_s = time.time()
        sample["_host_time_s"] = host_time_s

        with self.lock:
            if self.dual_start_host_s is None:
                self.dual_start_host_s = host_time_s

            device_queue = self.device_samples[role]
            device_queue.append(sample)

            while device_queue and host_time_s - device_queue[0]["_host_time_s"] > self.window_s:
                device_queue.popleft()

            other_queue = self.device_samples["BOAT"]
            if role == "SEAT" and other_queue:
                seat = sample
                boat = other_queue[-1]
                host_delta_s = abs(seat["_host_time_s"] - boat["_host_time_s"])

                if host_delta_s <= 0.25:
                    # Pair by computer receive time. This is not hardware-level
                    # synchronization, but it keeps the live prototype simple.
                    time_us = int((max(seat["_host_time_s"], boat["_host_time_s"]) - self.dual_start_host_s) * 1_000_000)
                    relative = make_relative_sample(
                        seat,
                        boat,
                        self.relative_sequence,
                        time_us,
                    )
                    relative["_host_delta_s"] = host_delta_s
                    self.relative_sequence += 1
                    self.samples.append(relative)

                    while self.samples and (time_us - self.samples[0]["time_us"]) / 1_000_000.0 > self.window_s:
                        self.samples.popleft()

                    if self.recording and self.record_writer:
                        self.record_writer.writerow(relative["raw_csv"])
                        self.record_count += 1
                        if self.record_count % 50 == 0:
                            self.record_file.flush()

            self.last_line = f"{role}: {sample['device_id']},{sample['sequence']},{sample['time_us']}"

    def snapshot(self):
        result = super().snapshot()
        with self.lock:
            relative_samples = list(self.samples)
            device_samples = {
                role: list(samples)
                for role, samples in self.device_samples.items()
            }
            result["connections"] = dict(self.device_connections)
            result["device_status"] = dict(self.device_status)

        result["device_metrics"] = {
            role: summarize_samples(samples)
            for role, samples in device_samples.items()
        }
        result["dual_comparison"] = build_dual_comparison(relative_samples)
        return result


async def dual_ble_device_worker(state, role, address, scan_lock):
    BleakClient, _ = import_bleak()

    while True:
        try:
            state.set_device_connection(role, None)
            state.set_device_status(role, "scanning")
            async with scan_lock:
                device = await choose_ble_device(None, address, state.scan_time_s)
            state.set_device_connection(role, ble_device_label(device))
            state.set_device_status(role, "connecting")

            line_queue = asyncio.Queue()
            partial_line = ""

            def handle_notification(_sender, data):
                nonlocal partial_line
                # BLE UART chunks do not necessarily match CSV rows, so keep a
                # text buffer until a newline completes one row.
                partial_line += data.decode("utf-8", errors="replace")

                while "\n" in partial_line:
                    line, partial_line = partial_line.split("\n", 1)
                    line_queue.put_nowait(line.strip())

            async with BleakClient(device) as client:
                await client.start_notify(
                    NUS_TX_CHARACTERISTIC_UUID,
                    handle_notification,
                )
                state.set_device_status(role, "live")

                while True:
                    line = await line_queue.get()
                    sample = parse_measurement(line)
                    if sample is None:
                        if line:
                            state.set_line(f"{role}: {line}")
                        continue
                    state.add_device_sample(role, sample)
        except Exception as error:
            state.set_device_status(role, "error", str(error))
            await asyncio.sleep(1.0)


async def dual_ble_worker_async(state):
    scan_lock = asyncio.Lock()
    await asyncio.gather(
        dual_ble_device_worker(state, "SEAT", state.seat_address, scan_lock),
        dual_ble_device_worker(state, "BOAT", state.boat_address, scan_lock),
    )


def dual_ble_worker(state):
    asyncio.run(dual_ble_worker_async(state))


def summarize_samples(samples):
    """Summarize one rolling sample window for dashboard counters."""
    if len(samples) < 2:
        return {
            "samples": len(samples),
            "duration_s": 0.0,
            "sample_rate": 0.0,
            "stroke_count": 0,
            "stroke_rate": 0.0,
            "forward_std": 0.0,
            "forward_rms": 0.0,
            "forward_min": 0.0,
            "forward_max": 0.0,
            "forward_range": 0.0,
            "pitch_std": 0.0,
            "yaw_std": 0.0,
            "sequence_gap": 0,
        }

    first = samples[0]
    last = samples[-1]
    duration_s = max(0.001, (last["time_us"] - first["time_us"]) / 1_000_000.0)
    forward = [sample["seat_forward_acc"] for sample in samples]
    pitch = [sample["seat_pitch_rate"] for sample in samples]
    yaw = [sample["seat_yaw_rate"] for sample in samples]
    peaks = detect_strokes(samples)
    stroke_count = max(0, len(peaks) - 1)

    return {
        "device_id": last["device_id"],
        "samples": len(samples),
        "duration_s": duration_s,
        "sample_rate": (len(samples) - 1) / duration_s,
        "stroke_count": stroke_count,
        "stroke_rate": stroke_count / duration_s * 60.0,
        "forward_std": pstdev(forward) if len(forward) > 1 else 0.0,
        "forward_rms": rms(forward),
        "forward_min": min(forward),
        "forward_max": max(forward),
        "forward_range": max(forward) - min(forward),
        "pitch_std": pstdev(pitch) if len(pitch) > 1 else 0.0,
        "yaw_std": pstdev(yaw) if len(yaw) > 1 else 0.0,
        "sequence_gap": last["sequence"] - first["sequence"] + 1 - len(samples),
    }


def build_series(samples, max_points=500):
    """Build downsampled time-series for fast browser rendering."""
    if not samples:
        return {
            "time": [],
            "forward": [],
            "pitch": [],
            "yaw": [],
        }

    step = max(1, math.ceil(len(samples) / max_points))
    selected = samples[::step]
    start = samples[0]["time_us"]
    forward = [sample["seat_forward_acc"] for sample in selected]
    baseline = mean(forward[: min(30, len(forward))]) if forward else 0.0
    centered_forward = [value - baseline for value in forward]
    smoothed_forward = moving_average(centered_forward, 5)

    return {
        "time": [(sample["time_us"] - start) / 1_000_000.0 for sample in selected],
        "forward": smoothed_forward,
        "pitch": [sample["seat_pitch_rate"] for sample in selected],
        "yaw": [sample["seat_yaw_rate"] for sample in selected],
    }


def build_dual_comparison(samples, max_points=500):
    """Calculate dual-IMU SEAT/BOAT/relative metrics and graph series."""
    if not samples:
        return {
            "metrics": {
                "aligned_samples": 0,
                "mean_relative_forward": 0.0,
                "std_relative_forward": 0.0,
                "rms_relative_forward": 0.0,
                "std_seat_forward": 0.0,
                "std_boat_forward": 0.0,
                "rms_seat_plus_boat_forward": 0.0,
                "subtraction_reduction_pct": 0.0,
                "relative_dominance_pct": 0.0,
                "boat_speed_proxy": 0.0,
                "boat_speed_peak_phase_pct": 0.0,
                "boat_smoothness_jerk_rms": 0.0,
                "relative_speed_proxy": 0.0,
                "relative_speed_peak_phase_pct": 0.0,
                "relative_smoothness_jerk_rms": 0.0,
                "mean_relative_pitch": 0.0,
                "mean_relative_yaw": 0.0,
                "mean_alignment_ms": 0.0,
                "max_alignment_ms": 0.0,
            },
            "series": {
                "time": [],
                "seat_forward": [],
                "boat_forward": [],
                "seat_plus_boat_forward": [],
                "relative_forward": [],
                "boat_velocity_proxy": [],
                "relative_velocity_proxy": [],
                "relative_lateral": [],
                "relative_vertical": [],
                "relative_roll": [],
                "relative_pitch": [],
                "relative_yaw": [],
            },
        }

    step = max(1, math.ceil(len(samples) / max_points))
    selected = samples[::step]
    start = samples[0]["time_us"]
    relative_forward = [sample["seat_forward_acc"] for sample in samples]
    seat_forward = [sample.get("seat_source_forward_acc", 0.0) for sample in samples]
    boat_forward = [sample.get("boat_source_forward_acc", 0.0) for sample in samples]
    seat_plus_boat = [seat + boat for seat, boat in zip(seat_forward, boat_forward)]
    times = [(sample["time_us"] - samples[0]["time_us"]) / 1_000_000.0 for sample in samples]
    boat_speed = speed_proxy_metrics(times, boat_forward)
    relative_speed = speed_proxy_metrics(times, relative_forward)
    relative_pitch = [sample["seat_pitch_rate"] for sample in samples]
    relative_yaw = [sample["seat_yaw_rate"] for sample in samples]
    alignment_ms = [
        sample.get("_host_delta_s", 0.0) * 1000.0
        for sample in samples
    ]
    seat_std = pstdev(seat_forward) if len(seat_forward) > 1 else 0.0
    boat_std = pstdev(boat_forward) if len(boat_forward) > 1 else 0.0
    rel_std = pstdev(relative_forward) if len(relative_forward) > 1 else 0.0
    subtraction_reduction_pct = (
        (1.0 - rel_std / seat_std) * 100.0
        if seat_std > 1e-9 else 0.0
    )
    relative_dominance_pct = (
        rel_std / (rel_std + boat_std) * 100.0
        if rel_std + boat_std > 1e-9 else 0.0
    )

    return {
        "metrics": {
            "aligned_samples": len(samples),
            "mean_relative_forward": mean(relative_forward),
            "std_relative_forward": rel_std,
            "rms_relative_forward": rms(relative_forward),
            "std_seat_forward": seat_std,
            "std_boat_forward": boat_std,
            "rms_seat_plus_boat_forward": rms(seat_plus_boat),
            "subtraction_reduction_pct": subtraction_reduction_pct,
            "relative_dominance_pct": relative_dominance_pct,
            "boat_speed_proxy": boat_speed["speed_proxy"],
            "boat_speed_peak_phase_pct": boat_speed["peak_phase_pct"],
            "boat_smoothness_jerk_rms": boat_speed["smoothness_jerk_rms"],
            "relative_speed_proxy": relative_speed["speed_proxy"],
            "relative_speed_peak_phase_pct": relative_speed["peak_phase_pct"],
            "relative_smoothness_jerk_rms": relative_speed["smoothness_jerk_rms"],
            "mean_relative_pitch": mean(relative_pitch),
            "mean_relative_yaw": mean(relative_yaw),
            "mean_alignment_ms": mean(alignment_ms) if alignment_ms else 0.0,
            "max_alignment_ms": max(alignment_ms) if alignment_ms else 0.0,
        },
        "series": {
            "time": [(sample["time_us"] - start) / 1_000_000.0 for sample in selected],
            "seat_forward": [sample.get("seat_source_forward_acc", 0.0) for sample in selected],
            "boat_forward": [sample.get("boat_source_forward_acc", 0.0) for sample in selected],
            "seat_plus_boat_forward": [
                sample.get("seat_source_forward_acc", 0.0) + sample.get("boat_source_forward_acc", 0.0)
                for sample in selected
            ],
            "relative_forward": [sample["seat_forward_acc"] for sample in selected],
            "boat_velocity_proxy": [boat_speed["velocity"][index] for index in range(0, len(samples), step)][:len(selected)],
            "relative_velocity_proxy": [relative_speed["velocity"][index] for index in range(0, len(samples), step)][:len(selected)],
            "relative_lateral": [sample["seat_lateral_acc"] for sample in selected],
            "relative_vertical": [sample["seat_vertical_acc"] for sample in selected],
            "relative_roll": [sample["seat_roll_rate"] for sample in selected],
            "relative_pitch": [sample["seat_pitch_rate"] for sample in selected],
            "relative_yaw": [sample["seat_yaw_rate"] for sample in selected],
        },
    }


def build_live_markers(samples, markers):
    if not samples:
        return []

    start_time_us = samples[0]["time_us"]
    end_time_us = samples[-1]["time_us"]
    visible = []

    for marker in markers:
        marker_time_us = marker.get("time_us")
        if marker_time_us is None:
            continue
        if start_time_us <= marker_time_us <= end_time_us:
            visible.append({
                "time": (marker_time_us - start_time_us) / 1_000_000.0,
                "label": marker.get("label", "marker"),
            })

    return visible


HTML = r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Rowing Live Dashboard</title>
  <style>
    :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f6f0; color: #202124; }
    body { margin: 0; padding: 22px; }
    main { max-width: 1180px; margin: 0 auto; }
    h1 { font-size: 28px; margin: 0 0 6px; }
    h2 { font-size: 18px; margin: 0 0 8px; }
    .sub, .note { color: #4f554b; line-height: 1.45; }
    .sub { margin: 0 0 18px; }
    .status, .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
    .pill { border: 1px solid #d7dccc; background: #fff; border-radius: 999px; padding: 6px 10px; font-size: 13px; }
    .live { color: #047857; font-weight: 700; }
    .error { color: #b91c1c; font-weight: 700; }
    button { border: 1px solid #bcc5b1; background: #fff; border-radius: 8px; padding: 9px 12px; cursor: pointer; font-weight: 650; }
    button:hover { background: #eef3e8; }
    .primary { background: #0f766e; color: #fff; border-color: #0f766e; }
    .danger { background: #b91c1c; color: #fff; border-color: #b91c1c; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .metric { background: #fff; border: 1px solid #d7dccc; border-radius: 8px; padding: 12px; }
    .metric span { display: block; color: #62675d; font-size: 12px; margin-bottom: 6px; }
    .metric strong { font-size: 22px; }
    .panel { background: #fff; border: 1px solid #d7dccc; border-radius: 8px; padding: 14px; margin-top: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
    th, td { border-bottom: 1px solid #e4e6df; padding: 8px; text-align: left; vertical-align: top; }
    th { color: #62675d; font-weight: 650; }
    canvas { width: 100%; height: 280px; display: block; }
    code { background: #eef0e8; border-radius: 4px; padding: 2px 4px; }
  </style>
</head>
<body>
<main>
  <h1>Rowing Live Dashboard</h1>
  <p class="sub">Live USB/BLE analysis using the mounting convention X = seat forward/back. In dual BLE mode, the graphs show SEAT minus BOAT.</p>
  <div class="status">
    <span class="pill">Source: <span id="source">-</span></span>
    <span class="pill">Status: <span id="status">starting</span></span>
    <span class="pill">Connection: <span id="port">-</span></span>
    <span class="pill">Window: <span id="window">-</span>s</span>
    <span class="pill">Recording: <span id="recording">off</span></span>
    <span class="pill">Rows: <span id="recordRows">0</span></span>
  </div>
  <div class="status">
    <span class="pill">SEAT: <span id="seatStatus">-</span></span>
    <span class="pill">BOAT: <span id="boatStatus">-</span></span>
    <span class="pill">SEAT Hz: <span id="seatHz">0.0</span></span>
    <span class="pill">BOAT Hz: <span id="boatHz">0.0</span></span>
  </div>
  <section class="metrics">
    <div class="metric"><span>SEAT Rows</span><strong id="seatRows">0</strong></div>
    <div class="metric"><span>BOAT Rows</span><strong id="boatRows">0</strong></div>
    <div class="metric"><span>SEAT Strokes</span><strong id="seatStrokes">0</strong></div>
    <div class="metric"><span>BOAT Strokes</span><strong id="boatStrokes">0</strong></div>
    <div class="metric"><span>SEAT Forward Std</span><strong id="seatForwardStd">0.00</strong></div>
    <div class="metric"><span>BOAT Forward Std</span><strong id="boatForwardStd">0.00</strong></div>
    <div class="metric"><span>SEAT Forward Range</span><strong id="seatForwardRange">0.00</strong></div>
    <div class="metric"><span>BOAT Forward Range</span><strong id="boatForwardRange">0.00</strong></div>
  </section>
  <p class="note">
    SEAT counters describe the moving seat IMU. BOAT counters describe the reference IMU.
    Ideally, the BOAT signal is smaller and smoother than SEAT during seat movement; if BOAT is large, the relative subtraction becomes more important.
  </p>
  <div class="controls">
    <button class="primary" id="startRecording">Start Recording</button>
    <button class="danger" id="stopRecording">Stop</button>
    <button id="markGood">Mark Good Stroke</button>
    <button id="markBad">Mark Bad Stroke</button>
    <span class="pill">File: <span id="recordFile">-</span></span>
  </div>
  <div class="controls">
    <button id="speechToggle">Enable Voice Feedback</button>
    <button id="speechTest">Test Voice</button>
    <span class="pill">Voice: <span id="speechStatus">off</span></span>
    <span class="pill">Every <input id="speechInterval" type="number" min="6" max="60" value="12" style="width:4.5em"> s</span>
  </div>
  <section class="metrics">
    <div class="metric"><span>Sample Rate</span><strong id="sampleRate">0.0 Hz</strong></div>
    <div class="metric"><span>Relative Samples</span><strong id="relativeSamples">0</strong></div>
    <div class="metric"><span>Window Duration</span><strong id="windowDuration">0.0s</strong></div>
    <div class="metric"><span>Detected Strokes</span><strong id="strokes">0</strong></div>
    <div class="metric"><span>Stroke Rate</span><strong id="strokeRate">0.0 spm</strong></div>
    <div class="metric"><span>Current Stroke Time</span><strong id="currentElapsed">0.00s</strong></div>
    <div class="metric"><span>Current Stroke Phase</span><strong id="currentPhase">0%</strong></div>
    <div class="metric"><span>Last Stroke</span><strong id="lastDuration">0.00s</strong></div>
    <div class="metric"><span>Power Transfer</span><strong id="powerProxy">0.00</strong></div>
    <div class="metric"><span>Forward Motion</span><strong id="forwardStd">0.00</strong></div>
    <div class="metric"><span>Forward RMS</span><strong id="forwardRms">0.00</strong></div>
    <div class="metric"><span>Forward Range</span><strong id="forwardRange">0.00</strong></div>
    <div class="metric"><span>Pitch Stability</span><strong id="pitchStd">0.00</strong></div>
    <div class="metric"><span>Yaw Stability</span><strong id="yawStd">0.00</strong></div>
    <div class="metric"><span>Sequence Gaps</span><strong id="gaps">0</strong></div>
  </section>
  <section class="panel">
    <h2>Two-IMU relative analysis</h2>
    <p class="note">
      Core calculation: <code>relative = SEAT - BOAT</code>. SEAT is the moving seat sensor;
      BOAT is the reference sensor on the shell. The relative signal removes boat/reference
      motion and leaves the seat motion that is more relevant for rowing phases.
    </p>
    <section class="metrics">
      <div class="metric"><span>Aligned Samples</span><strong id="relAligned">0</strong></div>
      <div class="metric"><span>Mean Relative Forward</span><strong id="relMeanForward">0.00</strong></div>
      <div class="metric"><span>Std Relative Forward</span><strong id="relStdForward">0.00</strong></div>
      <div class="metric"><span>RMS Relative Forward</span><strong id="relRmsForward">0.00</strong></div>
      <div class="metric"><span>Std SEAT Forward</span><strong id="seatStdForward">0.00</strong></div>
      <div class="metric"><span>Std BOAT Forward</span><strong id="boatStdForward">0.00</strong></div>
      <div class="metric"><span>SEAT + BOAT RMS</span><strong id="sumRmsForward">0.00</strong></div>
      <div class="metric"><span>Subtraction Effect</span><strong id="subtractEffect">0%</strong></div>
      <div class="metric"><span>Relative Dominance</span><strong id="relativeDominance">0%</strong></div>
      <div class="metric"><span>BOAT Speed Proxy</span><strong id="boatSpeedProxy">0.00</strong></div>
      <div class="metric"><span>BOAT Peak Phase</span><strong id="boatSpeedPhase">0%</strong></div>
      <div class="metric"><span>BOAT Smoothness</span><strong id="boatSmoothness">0.00</strong></div>
      <div class="metric"><span>Relative Speed Proxy</span><strong id="relativeSpeedProxy">0.00</strong></div>
      <div class="metric"><span>Relative Peak Phase</span><strong id="relativeSpeedPhase">0%</strong></div>
      <div class="metric"><span>Relative Smoothness</span><strong id="relativeSmoothness">0.00</strong></div>
      <div class="metric"><span>Mean Relative Pitch</span><strong id="relMeanPitch">0.00</strong></div>
      <div class="metric"><span>Mean Relative Yaw</span><strong id="relMeanYaw">0.00</strong></div>
      <div class="metric"><span>Mean Alignment</span><strong id="alignMean">0 ms</strong></div>
      <div class="metric"><span>Max Alignment</span><strong id="alignMax">0 ms</strong></div>
    </section>
    <table>
      <thead><tr><th>Value</th><th>Calculation</th><th>Meaning</th></tr></thead>
      <tbody>
        <tr><td>Relative forward</td><td><code>SEAT acc_x - BOAT acc_x</code></td><td>Forward/back seat acceleration after removing boat reference motion.</td></tr>
        <tr><td>SEAT + BOAT RMS</td><td><code>sqrt(mean((SEAT acc_x + BOAT acc_x)^2))</code></td><td>Diagnostic only: shows same-direction/shared movement or mounting drift. It is not the main rowing score.</td></tr>
        <tr><td>Subtraction effect</td><td><code>(1 - std(relative) / std(SEAT)) * 100</code></td><td>Positive means boat subtraction reduced the raw seat signal; negative means the relative signal is stronger than raw SEAT motion.</td></tr>
        <tr><td>Relative dominance</td><td><code>std(relative) / (std(relative) + std(BOAT)) * 100</code></td><td>Higher means the window is dominated by seat-relative movement rather than boat reference motion.</td></tr>
        <tr><td>BOAT speed proxy</td><td>Drift-corrected integration of centered, smoothed BOAT forward acceleration.</td><td>Approximate boat-only velocity shape. It is not calibrated boat speed in m/s.</td></tr>
        <tr><td>Relative speed proxy</td><td>Drift-corrected integration of centered, smoothed <code>SEAT - BOAT</code> forward acceleration.</td><td>Approximate seat-to-boat velocity shape.</td></tr>
        <tr><td>Smoothness</td><td><code>RMS(diff(smoothed acceleration) / diff(time))</code></td><td>Jerk-based roughness indicator. Lower is smoother.</td></tr>
        <tr><td>Alignment</td><td><code>|SEAT packet host time - BOAT packet host time|</code></td><td>Lower is better. It is current BLE packet timing, not hardware-level synchronization.</td></tr>
      </tbody>
    </table>
  </section>
  <section class="panel">
    <h2>SEAT vs BOAT forward acceleration</h2>
    <canvas id="dualForwardCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT and BOAT. Calculation: brown = <code>SEAT acc_x_ms2</code>, green = <code>BOAT acc_x_ms2</code>, blue = <code>SEAT acc_x_ms2 - BOAT acc_x_ms2</code>. This is a time-history plot, not a normalized stroke plot. The blue line is the main relative forward signal after removing boat reference acceleration.</p>
  </section>
  <section class="panel">
    <h2>Forward diagnostic: subtraction and sum</h2>
    <canvas id="dualSumCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT and BOAT. Calculation: blue = <code>SEAT acc_x_ms2 - BOAT acc_x_ms2</code>. Grey = <code>SEAT acc_x_ms2 + BOAT acc_x_ms2</code>. This is a time-history plot. The sum is only a diagnostic for shared/same-direction movement or mounting drift, not a rowing score.</p>
  </section>
  <section class="panel">
    <h2>BOAT and relative velocity proxies</h2>
    <canvas id="dualVelocityCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: BOAT for green, SEAT and BOAT for blue. Calculation: first center and smooth forward acceleration, then integrate with the trapezoid rule and simple linear drift correction. Green = <code>integral(smoothed BOAT acc_x)</code>. Blue = <code>integral(smoothed(SEAT acc_x - BOAT acc_x))</code>. These are time-history velocity proxies, not calibrated m/s.</p>
  </section>
  <section class="panel">
    <h2>Relative acceleration axes</h2>
    <canvas id="dualAccelerationCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT and BOAT. Calculation: each line is a relative acceleration axis. Forward = <code>SEAT acc_x - BOAT acc_x</code>, lateral = <code>SEAT acc_y - BOAT acc_y</code>, vertical = <code>SEAT acc_z - BOAT acc_z</code>. This is a time-history plot. Forward is the main rowing candidate; lateral and vertical show side movement and bounce.</p>
  </section>
  <section class="panel">
    <h2>Relative rotation rates</h2>
    <canvas id="dualRotationCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT and BOAT. Calculation: roll = <code>SEAT gyro_x - BOAT gyro_x</code>, pitch = <code>SEAT gyro_y - BOAT gyro_y</code>, yaw = <code>SEAT gyro_z - BOAT gyro_z</code>. This is a time-history plot. These are relative rotation-rate differences between seat and boat.</p>
  </section>
  <section class="panel">
    <h2>Current stroke timeline</h2>
    <canvas id="currentTimeCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: normally the current relative stream, which is <code>SEAT - BOAT</code> in dual BLE mode. Calculation: the current stroke starts at the latest detected peak in smoothed forward acceleration. Forward acceleration is centered and smoothed. Relative speed = drift-corrected integral of that acceleration. Power proxy = <code>max(0, forward acceleration * relative speed)</code>. X-axis is time in seconds since the current stroke start.</p>
  </section>
  <section class="panel">
    <h2>Stroke power transfer history</h2>
    <canvas id="strokePowerCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: normally the current relative stream, which is <code>SEAT - BOAT</code> in dual BLE mode. Calculation: each completed stroke is resampled to 0-100% phase by elapsed time between two detected stroke peaks. 100% means the next detected stroke peak, not 100% force. For each phase point, power proxy = <code>max(0, smoothed forward acceleration * relative speed proxy)</code>. Grey lines are recent completed strokes, blue is their phase-wise average, and brown is the current stroke. This is not calibrated watts.</p>
  </section>
  <section class="panel">
    <h2>Stroke shape history</h2>
    <canvas id="strokeShapeCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: normally the current relative stream, which is <code>SEAT - BOAT</code> in dual BLE mode. Calculation: each stroke is detected from peaks in smoothed forward acceleration and resampled to 0-100% phase by time. 100% means the time point of the next detected stroke peak, not maximum force. Brown = current centered/smoothed forward acceleration. Green = current drift-corrected relative speed proxy. Grey = recent completed speed proxies. Blue = phase-wise average of recent speed proxies.</p>
  </section>
  <section class="panel">
    <h2>SEAT forward acceleration history</h2>
    <canvas id="forwardCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT only. Calculation: raw <code>SEAT acc_x_ms2</code> is centered by subtracting a short baseline and smoothed with a moving average. This is a time-history plot and shows the seat-mounted forward/back acceleration used for first-pass stroke detection.</p>
  </section>
  <section class="panel">
    <h2>BOAT forward acceleration history</h2>
    <canvas id="boatForwardCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: BOAT only. Calculation: this is raw <code>BOAT acc_x_ms2</code> from the boat reference unit, plotted over time. It is not subtracted here. It helps check whether the BOAT unit is quiet and stable compared with the moving SEAT unit.</p>
  </section>
  <section class="panel">
    <h2>SEAT pitch and yaw rate history</h2>
    <canvas id="rotationCanvas" width="1100" height="300"></canvas>
    <p class="note">Sensors used: SEAT only. Calculation: pitch = <code>SEAT gyro_y_rads</code> and yaw = <code>SEAT gyro_z_rads</code> from the seat unit over time. These are angular velocity signals in rad/s. They help reveal body swing, twisting, or sensor mounting movement.</p>
  </section>
  <section class="panel">
    <h2>How the counters are calculated</h2>
    <table>
      <thead><tr><th>Counter</th><th>Calculation</th><th>Represents</th></tr></thead>
      <tbody>
        <tr><td>Detected strokes</td><td>Peaks in smoothed forward acceleration, separated by at least the tuning distance.</td><td>First-pass stroke count; fast strokes may need tuning.</td></tr>
        <tr><td>Stroke rate</td><td><code>detected strokes / window seconds * 60</code></td><td>Estimated strokes per minute in the visible rolling window.</td></tr>
        <tr><td>Current phase</td><td><code>current stroke elapsed / recent average stroke duration * 100</code></td><td>Where the current stroke is compared with recent completed strokes.</td></tr>
        <tr><td>Relative speed</td><td>Trapezoid integration of centered/smoothed forward acceleration with drift correction on completed strokes.</td><td>Proxy for seat speed shape; not calibrated to m/s.</td></tr>
        <tr><td>Power transfer</td><td><code>max(0, forward acceleration * relative speed)</code></td><td>Relative timing/intensity proxy for drive contribution; not watts.</td></tr>
        <tr><td>Sequence gaps</td><td><code>last sequence - first sequence + 1 - samples</code></td><td>Missing rows in the relative/live stream.</td></tr>
      </tbody>
    </table>
  </section>
</main>
<script>
const ids = ["source", "status", "port", "window", "recording", "recordRows", "recordFile", "speechStatus", "speechInterval", "seatStatus", "boatStatus", "seatHz", "boatHz", "seatRows", "boatRows", "seatStrokes", "boatStrokes", "seatForwardStd", "boatForwardStd", "seatForwardRange", "boatForwardRange", "sampleRate", "relativeSamples", "windowDuration", "strokes", "strokeRate", "currentElapsed", "currentPhase", "lastDuration", "powerProxy", "forwardStd", "forwardRms", "forwardRange", "pitchStd", "yawStd", "gaps", "relAligned", "relMeanForward", "relStdForward", "relRmsForward", "seatStdForward", "boatStdForward", "sumRmsForward", "subtractEffect", "relativeDominance", "boatSpeedProxy", "boatSpeedPhase", "boatSmoothness", "relativeSpeedProxy", "relativeSpeedPhase", "relativeSmoothness", "relMeanPitch", "relMeanYaw", "alignMean", "alignMax"];
const el = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
let speechEnabled = false;
let lastSpeechAt = 0;
let lastSpokenStrokeCount = -1;

function fmt(value, digits = 1) {
  return Number.isFinite(value) ? value.toFixed(digits) : "0.0";
}

async function post(path, payload = null) {
  const options = { method: "POST", cache: "no-store" };
  if (payload) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(payload);
  }
  await fetch(path, options);
  await tick();
}

document.getElementById("startRecording").onclick = () => post("/api/record/start");
document.getElementById("stopRecording").onclick = () => post("/api/record/stop");
document.getElementById("markGood").onclick = () => post("/api/marker/good_stroke");
document.getElementById("markBad").onclick = () => post("/api/marker/bad_stroke");
document.getElementById("speechToggle").onclick = () => {
  speechEnabled = !speechEnabled;
  el.speechStatus.textContent = speechEnabled ? "on" : "off";
  document.getElementById("speechToggle").textContent = speechEnabled ? "Disable Voice Feedback" : "Enable Voice Feedback";
  if (speechEnabled) speak("Voice feedback enabled.");
};
document.getElementById("speechTest").onclick = () => speak("Test. Rowing voice feedback is ready.");

async function speak(text) {
  try {
    const res = await fetch("/api/speak", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (res.ok) return;
  } catch (err) {
    // Fall back to browser speech below.
  }

  if (!("speechSynthesis" in window)) {
    el.speechStatus.textContent = "not supported";
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  utterance.rate = 0.72;
  utterance.pitch = 1.0;
  window.speechSynthesis.speak(utterance);
}

function buildVoiceMessage(data, m, sm, dualMetrics) {
  const parts = [];
  const strokeRate = Number(m.stroke_rate || 0);
  const relativeSpeed = Number(dualMetrics.relative_speed_proxy || 0);
  const relativePhase = Number(dualMetrics.relative_speed_peak_phase_pct || 0);
  const smoothness = Number(dualMetrics.relative_smoothness_jerk_rms || 0);

  parts.push(`Stroke rate ${strokeRate.toFixed(0)}`);
  if (relativeSpeed > 0) parts.push(`relative speed ${relativeSpeed.toFixed(1)}`);
  if (relativeSpeed > 0) parts.push(`peak at ${relativePhase.toFixed(0)} percent`);
  if (smoothness > 0) parts.push(`smoothness ${smoothness.toFixed(1)}`);
  return parts.join(". ") + ".";
}

function maybeSpeak(data, m, sm, dualMetrics) {
  if (!speechEnabled) return;
  const now = Date.now();
  const intervalS = Math.min(60, Math.max(6, Number(el.speechInterval.value || 12)));
  const strokeCount = Number(m.stroke_count || 0);
  if (now - lastSpeechAt < intervalS * 1000) return;
  lastSpokenStrokeCount = strokeCount;
  lastSpeechAt = now;
  speak(buildVoiceMessage(data, m, sm, dualMetrics));
}

function niceStep(range, targetTicks = 6) {
  if (!Number.isFinite(range) || range <= 0) return 1;
  const raw = range / targetTicks;
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalized = raw / power;
  if (normalized <= 1) return power;
  if (normalized <= 2) return 2 * power;
  if (normalized <= 5) return 5 * power;
  return 10 * power;
}

function drawSeries(canvas, xs, series, xLabel = "time", markers = []) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const padL = 72, padR = 22, padT = 24, padB = 42;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, w, h);
  const ys = series.flatMap(s => s.values || []).filter(Number.isFinite);
  if (!xs.length || !ys.length) return;
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  yMin = Math.min(yMin, 0);
  yMax = Math.max(yMax, 0);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const yPad = Math.max((yMax - yMin) * 0.08, 0.02);
  yMin -= yPad; yMax += yPad;
  const yStep = niceStep(yMax - yMin, 7);
  yMin = Math.floor(yMin / yStep) * yStep;
  yMax = Math.ceil(yMax / yStep) * yStep;
  const xStep = niceStep(Math.max(0.001, xMax - xMin), 9);
  const sx = x => padL + (x - xMin) / Math.max(0.001, xMax - xMin) * (w - padL - padR);
  const sy = y => h - padB - (y - yMin) / (yMax - yMin) * (h - padT - padB);

  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = "#e1e6dc";
  ctx.lineWidth = 1;
  for (let yValue = yMin; yValue <= yMax + yStep * 0.5; yValue += yStep) {
    const y = sy(yValue);
    const isZero = Math.abs(yValue) < yStep * 0.001;
    ctx.strokeStyle = isZero ? "#8d9787" : "#e1e6dc";
    ctx.lineWidth = isZero ? 1.8 : 1;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillStyle = isZero ? "#202124" : "#62675d";
    ctx.textAlign = "right";
    ctx.fillText(yValue.toFixed(Math.abs(yStep) < 1 ? 2 : 1), padL - 8, y);
  }
  const firstXTick = Math.ceil(xMin / xStep) * xStep;
  for (let xValue = firstXTick; xValue <= xMax + xStep * 0.5; xValue += xStep) {
    const x = sx(xValue);
    ctx.strokeStyle = "#eef1ea";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, h - padB); ctx.stroke();
    ctx.fillStyle = "#62675d";
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(xLabel === "seconds" ? `${xValue.toFixed(1)}s` : xValue.toFixed(0), x, h - 10);
  }
  ctx.strokeStyle = "#3f443c";
  ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, h - padB); ctx.lineTo(w - padR, h - padB); ctx.stroke();
  ctx.fillStyle = "#62675d";
  ctx.textAlign = "left";
  ctx.fillText(xLabel === "seconds" ? "time" : "phase", padL, h - 22);

  for (const marker of markers || []) {
    if (!Number.isFinite(marker.time) || marker.time < xMin || marker.time > xMax) continue;
    const x = sx(marker.time);
    const isBad = (marker.label || "").includes("bad");
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = isBad ? "#b91c1c" : "#0f766e";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, h - padB);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.save();
    ctx.translate(x + 5, padT + 64);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = isBad ? "#b91c1c" : "#0f766e";
    ctx.fillText(marker.label || "marker", 0, 0);
    ctx.restore();
  }

  for (const s of series) {
    const values = s.values || [];
    if (!values.length) continue;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 2;
    ctx.beginPath();
    values.forEach((value, i) => {
      const x = sx(xs[i]);
      const y = sy(value);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  let lx = padL + 8;
  for (const s of series) {
    if (!s.label) continue;
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, 10, 10, 10);
    ctx.fillStyle = "#202124";
    ctx.fillText(s.label, lx + 15, 20);
    lx += 150;
  }
}

function historySeries(history, key, color = "#9ca3af") {
  return (history || [])
    .map((segment, index) => ({
      label: index === 0 ? `recent ${key}` : "",
      values: segment[key] || [],
      color,
      width: 1,
    }))
    .filter(series => series.values.length);
}

async function tick() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    const data = await res.json();
    const m = data.metrics || {};
    const stroke = data.stroke || {};
    const sm = stroke.metrics || {};
    const ds = data.device_status || {};
    const dm = data.device_metrics || {};
    const dual = data.dual_comparison || {};
    const dualMetrics = dual.metrics || {};
    const dualSeries = dual.series || {};
    el.source.textContent = data.source || "-";
    el.status.textContent = data.status || "-";
    el.status.className = data.status === "live" ? "live" : (data.status === "error" ? "error" : "");
    el.port.textContent = data.port || "-";
    el.window.textContent = fmt(data.window_s, 0);
    el.recording.textContent = data.recording ? "on" : "off";
    el.recording.className = data.recording ? "live" : "";
    el.recordRows.textContent = data.record_count || 0;
    el.recordFile.textContent = data.record_path || "-";
    el.seatStatus.textContent = ds.SEAT || "-";
    el.boatStatus.textContent = ds.BOAT || "-";
    el.seatHz.textContent = fmt((dm.SEAT || {}).sample_rate, 1);
    el.boatHz.textContent = fmt((dm.BOAT || {}).sample_rate, 1);
    el.seatRows.textContent = (dm.SEAT || {}).samples || 0;
    el.boatRows.textContent = (dm.BOAT || {}).samples || 0;
    el.seatStrokes.textContent = (dm.SEAT || {}).stroke_count || 0;
    el.boatStrokes.textContent = (dm.BOAT || {}).stroke_count || 0;
    el.seatForwardStd.textContent = fmt((dm.SEAT || {}).forward_std, 2);
    el.boatForwardStd.textContent = fmt((dm.BOAT || {}).forward_std, 2);
    el.seatForwardRange.textContent = fmt((dm.SEAT || {}).forward_range, 2);
    el.boatForwardRange.textContent = fmt((dm.BOAT || {}).forward_range, 2);
    el.sampleRate.textContent = `${fmt(m.sample_rate, 1)} Hz`;
    el.relativeSamples.textContent = m.samples || 0;
    el.windowDuration.textContent = `${fmt(m.duration_s, 1)}s`;
    el.strokes.textContent = m.stroke_count || 0;
    el.strokeRate.textContent = `${fmt(m.stroke_rate, 1)} spm`;
    el.currentElapsed.textContent = `${fmt(sm.current_elapsed, 2)}s`;
    el.currentPhase.textContent = `${fmt(sm.current_phase, 0)}%`;
    el.lastDuration.textContent = `${fmt(sm.last_duration, 2)}s`;
    el.powerProxy.textContent = fmt(sm.avg_peak_power || sm.last_peak_power, 2);
    el.forwardStd.textContent = fmt(m.forward_std, 2);
    el.forwardRms.textContent = fmt(m.forward_rms, 2);
    el.forwardRange.textContent = fmt(m.forward_range, 2);
    el.pitchStd.textContent = fmt(m.pitch_std, 2);
    el.yawStd.textContent = fmt(m.yaw_std, 2);
    el.gaps.textContent = m.sequence_gap || 0;
    el.relAligned.textContent = dualMetrics.aligned_samples || 0;
    el.relMeanForward.textContent = `${fmt(dualMetrics.mean_relative_forward, 2)} m/s²`;
    el.relStdForward.textContent = `${fmt(dualMetrics.std_relative_forward, 2)} m/s²`;
    el.relRmsForward.textContent = `${fmt(dualMetrics.rms_relative_forward, 2)} m/s²`;
    el.seatStdForward.textContent = `${fmt(dualMetrics.std_seat_forward, 2)} m/s²`;
    el.boatStdForward.textContent = `${fmt(dualMetrics.std_boat_forward, 2)} m/s²`;
    el.sumRmsForward.textContent = `${fmt(dualMetrics.rms_seat_plus_boat_forward, 2)} m/s²`;
    el.subtractEffect.textContent = `${fmt(dualMetrics.subtraction_reduction_pct, 0)}%`;
    el.relativeDominance.textContent = `${fmt(dualMetrics.relative_dominance_pct, 0)}%`;
    el.boatSpeedProxy.textContent = fmt(dualMetrics.boat_speed_proxy, 2);
    el.boatSpeedPhase.textContent = `${fmt(dualMetrics.boat_speed_peak_phase_pct, 0)}%`;
    el.boatSmoothness.textContent = fmt(dualMetrics.boat_smoothness_jerk_rms, 2);
    el.relativeSpeedProxy.textContent = fmt(dualMetrics.relative_speed_proxy, 2);
    el.relativeSpeedPhase.textContent = `${fmt(dualMetrics.relative_speed_peak_phase_pct, 0)}%`;
    el.relativeSmoothness.textContent = fmt(dualMetrics.relative_smoothness_jerk_rms, 2);
    el.relMeanPitch.textContent = `${fmt(dualMetrics.mean_relative_pitch, 3)} rad/s`;
    el.relMeanYaw.textContent = `${fmt(dualMetrics.mean_relative_yaw, 3)} rad/s`;
    el.alignMean.textContent = `${fmt(dualMetrics.mean_alignment_ms, 0)} ms`;
    el.alignMax.textContent = `${fmt(dualMetrics.max_alignment_ms, 0)} ms`;

    const current = stroke.current || {};
    const last = stroke.last || {};
    const avg = stroke.average || {};
    const history = stroke.history || [];
    drawSeries(document.getElementById("dualForwardCanvas"), dualSeries.time || [], [
      { label: "seat_forward_acc", values: dualSeries.seat_forward || [], color: "#7c2d12" },
      { label: "boat_forward_acc", values: dualSeries.boat_forward || [], color: "#0f766e" },
      { label: "relative_forward_acc", values: dualSeries.relative_forward || [], color: "#1d4ed8", width: 2.5 },
    ], "seconds");
    drawSeries(document.getElementById("dualSumCanvas"), dualSeries.time || [], [
      { label: "relative_forward_acc", values: dualSeries.relative_forward || [], color: "#1d4ed8", width: 2.5 },
      { label: "seat_plus_boat_forward", values: dualSeries.seat_plus_boat_forward || [], color: "#64748b" },
    ], "seconds");
    drawSeries(document.getElementById("dualVelocityCanvas"), dualSeries.time || [], [
      { label: "boat_velocity_proxy", values: dualSeries.boat_velocity_proxy || [], color: "#0f766e" },
      { label: "relative_velocity_proxy", values: dualSeries.relative_velocity_proxy || [], color: "#1d4ed8", width: 2.5 },
    ], "seconds");
    drawSeries(document.getElementById("dualAccelerationCanvas"), dualSeries.time || [], [
      { label: "relative_forward_acc", values: dualSeries.relative_forward || [], color: "#7c2d12" },
      { label: "relative_lateral_acc", values: dualSeries.relative_lateral || [], color: "#0f766e" },
      { label: "relative_vertical_acc", values: dualSeries.relative_vertical || [], color: "#1d4ed8" },
    ], "seconds");
    drawSeries(document.getElementById("dualRotationCanvas"), dualSeries.time || [], [
      { label: "relative_roll_rate", values: dualSeries.relative_roll || [], color: "#0f766e" },
      { label: "relative_pitch_rate", values: dualSeries.relative_pitch || [], color: "#b91c1c" },
      { label: "relative_yaw_rate", values: dualSeries.relative_yaw || [], color: "#1d4ed8" },
    ], "seconds");
    drawSeries(document.getElementById("currentTimeCanvas"), current.time || [], [
      { label: "power proxy", values: current.power_time || [], color: "#7c2d12", width: 2.5 },
      { label: "forward acc", values: current.acc_time || [], color: "#0f766e" },
      { label: "relative speed", values: current.velocity_time || [], color: "#1d4ed8" },
    ], "seconds");
    drawSeries(document.getElementById("strokePowerCanvas"), stroke.phase || [], [
      ...historySeries(history, "power", "#cbd5e1"),
      { label: "current power", values: current.power || [], color: "#7c2d12", width: 2.5 },
      { label: "last power", values: last.power || [], color: "#0f766e" },
      { label: "recent avg power", values: avg.power || [], color: "#1d4ed8", width: 2.5 },
    ], "phase");
    drawSeries(document.getElementById("strokeShapeCanvas"), stroke.phase || [], [
      ...historySeries(history, "velocity", "#cbd5e1"),
      { label: "current forward acc", values: current.acc || [], color: "#7c2d12" },
      { label: "current relative speed", values: current.velocity || [], color: "#0f766e", width: 2.5 },
      { label: "recent avg speed", values: avg.velocity || [], color: "#1d4ed8", width: 2.5 },
    ], "phase");

    const s = data.series || {};
    const markers = data.markers || [];
    drawSeries(document.getElementById("forwardCanvas"), s.time || [], [
      { label: "seat_forward_acc", values: s.forward || [], color: "#7c2d12" },
    ], "seconds", markers);
    drawSeries(document.getElementById("boatForwardCanvas"), dualSeries.time || [], [
      { label: "boat_forward_acc", values: dualSeries.boat_forward || [], color: "#0f766e", width: 2.5 },
    ], "seconds");
    drawSeries(document.getElementById("rotationCanvas"), s.time || [], [
      { label: "pitch", values: s.pitch || [], color: "#b91c1c" },
      { label: "yaw", values: s.yaw || [], color: "#1d4ed8" },
    ], "seconds", markers);
    maybeSpeak(data, m, sm, dualMetrics);
  } catch (err) {
    el.status.textContent = "dashboard error";
    el.status.className = "error";
  }
}
setInterval(tick, 300);
tick();
</script>
</body>
</html>
"""


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/state":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            path = urlparse(self.path).path

            if path == "/api/record/start":
                record_path = state.start_recording()
                self.send_json({"ok": True, "record_path": str(record_path)})
                return

            if path == "/api/record/stop":
                state.stop_recording()
                self.send_json({"ok": True})
                return

            if path.startswith("/api/marker/"):
                label = path.rsplit("/", 1)[-1].replace("_", " ")
                ok = state.add_marker(label)
                self.send_json({"ok": ok, "label": label})
                return

            if path == "/api/speak":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}
                ok = state.speak(payload.get("text", ""))
                self.send_json({"ok": ok})
                return

            self.send_response(404)
            self.end_headers()

        def send_json(self, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Live web dashboard for rowing IMU USB Serial or BLE data.")
    parser.add_argument("--source", choices=("usb", "ble", "dual_ble"), default="usb", help="Data source, default usb")
    parser.add_argument("--port", help="Serial port, for example /dev/cu.usbmodem1401")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--ble-name", help="BLE name filter, for example Rowing-SEAT or Rowing-BOAT")
    parser.add_argument("--ble-address", help="BLE address to connect to directly")
    parser.add_argument("--seat-address", default=DEFAULT_SEAT_BLE_ADDRESS, help="BLE address for Rowing-SEAT in dual_ble mode")
    parser.add_argument("--boat-address", default=DEFAULT_BOAT_BLE_ADDRESS, help="BLE address for Rowing-BOAT in dual_ble mode")
    parser.add_argument("--scan-time", type=float, default=6.0, help="BLE scan time in seconds")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_S)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    args = parser.parse_args()

    if args.source == "dual_ble":
        state = DualBleState(
            args.window,
            args.record_dir,
            args.seat_address,
            args.boat_address,
            args.scan_time,
        )
    else:
        state = LiveState(
            args.source,
            args.port,
            args.baud,
            args.window,
            args.record_dir,
            args.ble_name,
            args.ble_address,
            args.scan_time,
        )

    server = ThreadingHTTPServer((args.host, args.http_port), make_handler(state))
    url = f"http://{args.host}:{args.http_port}"
    print(f"Dashboard: {url}")

    if args.source == "usb":
        thread = threading.Thread(target=serial_worker, args=(state,), daemon=True)
        thread.start()
        print("Close Arduino Serial Monitor and live_serial.py before using this dashboard.")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    elif args.source == "ble":
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print("Using BLE UART. The Feather should advertise as Rowing-SEAT or Rowing-BOAT.")
        print("Press Ctrl+C to stop.")
        try:
            ble_worker(state)
        except KeyboardInterrupt:
            server.shutdown()
            print("\nStopped.")
    else:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print("Using dual BLE UART.")
        print(f"SEAT address: {args.seat_address}")
        print(f"BOAT address: {args.boat_address}")
        print("The graphs show relative SEAT - BOAT motion.")
        print("Press Ctrl+C to stop.")
        try:
            dual_ble_worker(state)
        except KeyboardInterrupt:
            server.shutdown()
            print("\nStopped.")


if __name__ == "__main__":
    main()
