# Rowing IMU Prototype

Low-cost IMU-based rowing sensing prototype for logging, live feedback, and offline analysis of rowing-related movement signals.

The project supports:

- SD card logging on the microcontroller
- USB serial streaming
- Bluetooth LE UART streaming
- single-IMU analysis
- dual-IMU `SEAT - BOAT` relative analysis
- live browser dashboard
- optional voice feedback on macOS
- offline HTML reports from CSV files

The system is designed as a research prototype. It focuses on relative and stroke-level indicators, not exact absolute seat position or calibrated boat speed.

## Repository Contents

| File | Purpose |
| `sketch_mar25a.ino` | Arduino firmware for the Feather/IMU logger |
| `live_dashboard.py` | Live web dashboard for USB, BLE, or dual BLE |
| `live_serial.py` | Terminal live monitor for USB serial |
| `live_ble_uart.py` | Terminal live monitor for BLE UART |
| `analyze_log.py` | Offline CSV analyzer and HTML report generator |
| `technical_program_overview.pdf` | PDF version of the technical overview |

## Hardware

The prototype was developed around an Adafruit Feather-style nRF52 board with:

- 6-axis IMU acceleration + gyroscope
- SD card logging
- USB serial
- Bluetooth LE UART

Two devices can be used:

- `SEAT`: mounted on or near the sliding seat
- `BOAT`: mounted on the boat shell as a reference

The same firmware is used for both units. Change the firmware constant before flashing:

```cpp
const char DEVICE_ID[] = "SEAT";
```

or:

```cpp
const char DEVICE_ID[] = "BOAT";
```

## Data Format

The current CSV format is:

```csv
device_id,sequence,time_us,acc_x_ms2,acc_y_ms2,acc_z_ms2,gyro_x_rads,gyro_y_rads,gyro_z_rads
```

## Firmware Setup

Install the Arduino IDE and the Adafruit nRF52 board support package.

Required Arduino libraries:

- `Adafruit LSM6DS`
- `Adafruit Sensor`
- `SdFat` / `SD`
- `Adafruit Bluefruit nRF52`

Compile example:

```bash
'/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli' compile \
  --fqbn adafruit:nrf52:feather52840sense \
  --libraries /Users/mahdoui/Library/Arduino15/libraries \
  /Users/mahdoui/Desktop/Arduino/AnIMU-Based-Wearable-Sensing-Approach-for-Rowing/sketch_mar25a
```

Upload example:

```bash
'/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli' upload \
  -p /dev/cu.usbmodemXXXX \
  --fqbn adafruit:nrf52:feather52840sense \
  /Users/mahdoui/Desktop/Arduino/AnIMU-Based-Wearable-Sensing-Approach-for-Rowing/sketch_mar25a
```

Replace `/dev/cu.usbmodemXXXX` with the actual port.

To find serial ports on macOS:

```bash
ls /dev/cu.usbmodem*
```

## Python Setup

Recommended Python packages:

```bash
/usr/local/bin/python3 -m pip install pyserial bleak
```

Optional packages may already be present in your environment.

## Running the Live Dashboard

The dashboard starts a local web server. Open the printed URL in a browser.

### USB Mode

Use this when one device is connected by USB:

```bash
cd /Users/mahdoui/Desktop/Arduino/AnIMU-Based-Wearable-Sensing-Approach-for-Rowing/sketch_mar25a
/usr/local/bin/python3 live_dashboard.py --source usb --http-port 8010
```

Open:

```text
http://127.0.0.1:8010
```

### Single BLE Mode

Use this when one BLE device is connected:

```bash
/usr/local/bin/python3 live_dashboard.py --source ble --ble-name Rowing-SEAT --http-port 8010
```

### Dual BLE Mode

Use this for one `SEAT` unit and one `BOAT` unit:

```bash
/usr/local/bin/python3 live_dashboard.py \
  --source dual_ble \
  --seat-address 6044EEC2-62E9-A3B2-75AC-95D06200C12B \
  --boat-address 863A5D89-2A83-9CD0-B789-16C335C872D2 \
  --http-port 8010
```

The dashboard computes the live relative stream:

```text
relative = SEAT - BOAT
```

## Live Recording

In the dashboard:

1. Click `Start Recording`.
2. Perform the test movement.
3. Optionally mark good or bad strokes.
4. Click `Stop`.

Recordings are written to:

```text
/Users/mahdoui/Downloads/live_capture_XXX.csv
```

Markers are written to:

```text
/Users/mahdoui/Downloads/live_capture_XXX_markers.csv
```

## Voice Feedback

The dashboard can speak short feedback using macOS `say`.

It currently speaks:

- stroke rate
- relative speed
- peak phase
- smoothness

Use the `Enable Voice Feedback` button in the dashboard. If Bluetooth headphones are connected as the Mac audio output, the voice feedback should play through them.

Test macOS speech manually:

```bash
/usr/bin/say "Rowing voice feedback test"
```

## Terminal Live Tools

USB serial terminal view:

```bash
/usr/local/bin/python3 live_serial.py
```

BLE terminal view:

```bash
/usr/local/bin/python3 live_ble_uart.py --name Rowing-SEAT
```

List BLE devices:

```bash
/usr/local/bin/python3 live_ble_uart.py --list
```

## Offline Analysis

Run offline analysis on a CSV file:

```bash
/usr/local/bin/python3 analyze_log.py /Users/mahdoui/Downloads/LOG002.CSV \
  --output /Users/mahdoui/Downloads/imu_report.html
```

Run offline analysis with a BOAT/reference file:

```bash
/usr/local/bin/python3 analyze_log.py /Users/mahdoui/Downloads/SEAT_LOG.csv \
  --reference /Users/mahdoui/Downloads/BOAT_LOG.csv \
  --output /Users/mahdoui/Downloads/imu_report.html
```

Open the generated HTML report in a browser.

The offline report includes:

- sample count
- sampling rate
- sequence gaps
- axis statistics
- stroke estimates
- stroke-shape plots
- optional `SEAT - BOAT` relative plots
- velocity proxies
- smoothness indicators

## How the Main Signals Are Interpreted

The default mounting convention is:

- `acc_x_ms2`: forward/back movement candidate
- `acc_y_ms2`: lateral movement candidate
- `acc_z_ms2`: vertical movement candidate
- `gyro_x_rads`: roll rate candidate
- `gyro_y_rads`: pitch rate candidate
- `gyro_z_rads`: yaw rate candidate

This convention depends on mounting. For useful results, mount both boxes consistently and mark their axes on the enclosure.

## Synchronization Note

Dual BLE mode is not hardware-synchronized. The dashboard pairs recent SEAT and BOAT samples by computer receive time.

This is sufficient for live prototype feedback, but a future validation setup should use tighter synchronization or a reference system.

## Troubleshooting

### Dashboard connects but shows zero samples

The firmware may be sending an older CSV format. The current parser accepts old extra columns, but restart the dashboard after pulling changes.

### BLE device is busy

Close other scripts and browser tabs using BLE. Power-cycle the Feather devices if needed.

### No Bluetooth audio feedback

Check that macOS output is set to your headphones, then test:

```bash
/usr/bin/say "test voice"
```

### USB serial does not connect

Close Arduino Serial Monitor and any running Python scripts. Then retry.

### SD card file is missing

Check that the SD card is inserted before boot and that the firmware prints a `LOGxxx.CSV` filename on startup.

