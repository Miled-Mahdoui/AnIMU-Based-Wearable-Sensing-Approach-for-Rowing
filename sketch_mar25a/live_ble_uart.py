#!/usr/bin/env python3
import argparse
import asyncio
import time
from collections import deque

from live_serial import parse_measurement, print_summary, summarize_window


NUS_TX_CHARACTERISTIC_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def import_bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise SystemExit(
            "bleak is missing. Install it with:\n"
            "  /usr/local/bin/python3 -m pip install bleak"
        ) from error

    return BleakClient, BleakScanner


def device_label(device):
    name = device.name or "unknown"
    return f"{name} [{device.address}]"


async def scan_devices(timeout_s):
    _, BleakScanner = import_bleak()
    devices = await BleakScanner.discover(timeout=timeout_s)
    return sorted(devices, key=lambda device: (device.name or "", device.address))


async def choose_device(requested_name, requested_address, timeout_s):
    devices = await scan_devices(timeout_s)

    if requested_address:
        for device in devices:
            if device.address.lower() == requested_address.lower():
                return device
        raise SystemExit(f"No BLE device with address {requested_address} found.")

    rowing_devices = [
        device
        for device in devices
        if (device.name or "").startswith("Rowing-")
    ]

    if requested_name:
        matches = [
            device
            for device in devices
            if requested_name.lower() in (device.name or "").lower()
        ]
        if not matches:
            raise SystemExit(f"No BLE device matching name '{requested_name}' found.")
        rowing_devices = matches

    if not rowing_devices:
        raise SystemExit(
            "No Rowing-* BLE device found. Flash the Arduino sketch, power the "
            "Feather, and make sure no other app is already connected."
        )

    if len(rowing_devices) > 1:
        print("Multiple rowing BLE devices found:")
        for index, device in enumerate(rowing_devices, start=1):
            print(f"  {index}. {device_label(device)}")
        print(f"Using first device: {device_label(rowing_devices[0])}")

    return rowing_devices[0]


async def list_devices(timeout_s):
    devices = await scan_devices(timeout_s)
    if not devices:
        print("No BLE devices found.")
        return

    for device in devices:
        print(device_label(device))


async def run_live_ble(args):
    BleakClient, _ = import_bleak()
    device = await choose_device(args.name, args.address, args.scan_time)
    samples = deque()
    line_queue = asyncio.Queue()
    partial_line = ""
    last_print = 0.0

    def handle_notification(_sender, data):
        nonlocal partial_line

        text = data.decode("utf-8", errors="replace")
        partial_line += text

        while "\n" in partial_line:
            line, partial_line = partial_line.split("\n", 1)
            line_queue.put_nowait(line.strip())

    print(f"Connecting to {device_label(device)}")
    print("Press Ctrl+C to stop.")

    async with BleakClient(device) as client:
        await client.start_notify(NUS_TX_CHARACTERISTIC_UUID, handle_notification)
        print("Connected. Waiting for CSV data...")

        while True:
            line = await line_queue.get()
            sample = parse_measurement(line)
            if sample is None:
                if line:
                    print(f"\n{line}")
                continue

            samples.append(sample)
            newest_time = sample["time_us"]

            while samples and (newest_time - samples[0]["time_us"]) / 1_000_000.0 > args.window:
                samples.popleft()

            now = time.monotonic()
            if now - last_print >= 0.5:
                summary = summarize_window(list(samples))
                if summary:
                    print_summary(summary)
                last_print = now


def main():
    parser = argparse.ArgumentParser(
        description="Read rowing IMU CSV data live over Bluetooth LE UART."
    )
    parser.add_argument("--name", help="BLE name filter, for example Rowing-SEAT or Rowing-BOAT")
    parser.add_argument("--address", help="BLE address to connect to directly")
    parser.add_argument("--window", type=float, default=30.0, help="Rolling analysis window in seconds")
    parser.add_argument("--scan-time", type=float, default=6.0, help="BLE scan time in seconds")
    parser.add_argument("--list", action="store_true", help="List BLE devices and exit")
    args = parser.parse_args()

    try:
        if args.list:
            asyncio.run(list_devices(args.scan_time))
        else:
            asyncio.run(run_live_ble(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
