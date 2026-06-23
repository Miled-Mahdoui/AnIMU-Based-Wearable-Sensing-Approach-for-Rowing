#!/usr/bin/env python3
import argparse
import csv
import glob
import math
import sys
import time
from collections import deque
from statistics import mean, pstdev


DEFAULT_BAUD = 115200
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


def choose_port(requested_port):
    if requested_port:
        return requested_port

    ports = find_serial_ports()
    if not ports:
        raise SystemExit(
            "No USB serial port found. Connect the Feather, close Arduino Serial "
            "Monitor, then run this script again. You can also pass --port manually."
        )

    if len(ports) > 1:
        print("Multiple USB serial ports found:")
        for index, port in enumerate(ports, start=1):
            print(f"  {index}. {port}")
        print(f"Using first port: {ports[0]}")

    return ports[0]


def parse_measurement(line):
    line = line.strip()
    if not line or line.startswith("Rowing ") or line.startswith("Device:"):
        return None
    if line.startswith("IMU erkannt") or line.startswith("Logdatei:"):
        return None
    if line.startswith("Messung startet"):
        return None
    if line.startswith("device_id,"):
        return None

    values = next(csv.reader([line]))
    if len(values) < len(CSV_FIELDS):
        return None
    values = values[:len(CSV_FIELDS)]

    row = dict(zip(CSV_FIELDS, values))
    try:
        parsed = {
            "device_id": row["device_id"],
            "sequence": int(row["sequence"]),
            "time_us": int(row["time_us"]),
            "seat_forward_acc": float(row["acc_x_ms2"]),
            "seat_lateral_acc": float(row["acc_y_ms2"]),
            "seat_vertical_acc": float(row["acc_z_ms2"]),
            "seat_roll_rate": float(row["gyro_x_rads"]),
            "seat_pitch_rate": float(row["gyro_y_rads"]),
            "seat_yaw_rate": float(row["gyro_z_rads"]),
        }
    except ValueError:
        return None

    return parsed


def moving_average(values, window):
    if not values:
        return []
    window = max(1, min(window, len(values)))
    result = []
    running = 0.0
    queue = deque()

    for value in values:
        queue.append(value)
        running += value
        if len(queue) > window:
            running -= queue.popleft()
        result.append(running / len(queue))

    return result


def detect_strokes(samples, min_distance_s=0.85):
    if len(samples) < 20:
        return []

    times = [(sample["time_us"] - samples[0]["time_us"]) / 1_000_000.0 for sample in samples]
    values = [sample["seat_forward_acc"] for sample in samples]
    baseline = mean(values[: min(100, len(values))])
    centered = [value - baseline for value in values]
    smoothed = moving_average(centered, 9)
    signal_std = pstdev(smoothed) if len(smoothed) > 1 else 0.0
    threshold = max(0.35 * signal_std, 0.8)

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


def summarize_window(samples):
    if len(samples) < 2:
        return None

    first = samples[0]
    last = samples[-1]
    duration_s = (last["time_us"] - first["time_us"]) / 1_000_000.0
    if duration_s <= 0:
        return None

    forward_values = [sample["seat_forward_acc"] for sample in samples]
    lateral_values = [sample["seat_lateral_acc"] for sample in samples]
    vertical_values = [sample["seat_vertical_acc"] for sample in samples]
    pitch_values = [sample["seat_pitch_rate"] for sample in samples]
    yaw_values = [sample["seat_yaw_rate"] for sample in samples]

    peaks = detect_strokes(samples)
    stroke_count = max(0, len(peaks) - 1)
    stroke_rate = (stroke_count / duration_s * 60.0) if duration_s > 0 else 0.0
    sequence_gap = last["sequence"] - first["sequence"] + 1 - len(samples)
    sample_rate = (len(samples) - 1) / duration_s

    return {
        "device_id": last["device_id"],
        "samples": len(samples),
        "duration_s": duration_s,
        "sample_rate": sample_rate,
        "sequence_gap": sequence_gap,
        "stroke_count": stroke_count,
        "stroke_rate": stroke_rate,
        "forward_mean": mean(forward_values),
        "forward_std": pstdev(forward_values) if len(forward_values) > 1 else 0.0,
        "forward_min": min(forward_values),
        "forward_max": max(forward_values),
        "lateral_std": pstdev(lateral_values) if len(lateral_values) > 1 else 0.0,
        "vertical_mean": mean(vertical_values),
        "pitch_std": pstdev(pitch_values) if len(pitch_values) > 1 else 0.0,
        "yaw_std": pstdev(yaw_values) if len(yaw_values) > 1 else 0.0,
    }


def print_summary(summary):
    print(
        "\r"
        f"{summary['device_id']} | "
        f"{summary['sample_rate']:5.1f} Hz | "
        f"strokes {summary['stroke_count']:2d} | "
        f"{summary['stroke_rate']:5.1f} spm | "
        f"forward std {summary['forward_std']:5.2f} | "
        f"range {summary['forward_min']:6.2f}..{summary['forward_max']:6.2f} | "
        f"pitch std {summary['pitch_std']:5.2f} | "
        f"yaw std {summary['yaw_std']:5.2f} | "
        f"gaps {summary['sequence_gap']:3d}",
        end="",
        flush=True,
    )


def run_live(port, baud, window_s):
    serial = import_serial()
    samples = deque()
    last_print = 0.0

    print(f"Opening {port} at {baud} baud")
    print("Close Arduino Serial Monitor first, because only one program can use the port.")
    print("Press Ctrl+C to stop.")

    with serial.Serial(port, baudrate=baud, timeout=1) as connection:
        connection.reset_input_buffer()

        while True:
            raw_line = connection.readline()
            if not raw_line:
                continue

            line = raw_line.decode("utf-8", errors="replace").strip()
            sample = parse_measurement(line)
            if sample is None:
                if line:
                    print(f"\n{line}")
                continue

            samples.append(sample)
            newest_time = sample["time_us"]

            while samples and (newest_time - samples[0]["time_us"]) / 1_000_000.0 > window_s:
                samples.popleft()

            now = time.monotonic()
            if now - last_print >= 0.5:
                summary = summarize_window(list(samples))
                if summary:
                    print_summary(summary)
                last_print = now


def main():
    parser = argparse.ArgumentParser(
        description="Read rowing IMU CSV data live from USB Serial."
    )
    parser.add_argument("--port", help="Serial port, for example /dev/cu.usbmodem1401")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate, default {DEFAULT_BAUD}")
    parser.add_argument("--window", type=float, default=30.0, help="Rolling analysis window in seconds")
    parser.add_argument("--list-ports", action="store_true", help="List detected USB serial ports")
    args = parser.parse_args()

    if args.list_ports:
        ports = find_serial_ports()
        if ports:
            print("\n".join(ports))
        else:
            print("No USB serial ports found.")
        return

    port = choose_port(args.port)

    try:
        run_live(port, args.baud, args.window)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
