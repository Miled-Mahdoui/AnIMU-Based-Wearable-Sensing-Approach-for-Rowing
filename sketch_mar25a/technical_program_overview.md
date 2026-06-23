# Rowing IMU Prototype - Program and Metric Overview

## Purpose

This document explains the complete prototype workflow, the live dashboard, the main metrics, and the meaning of each graph. It is written as a practical explanation for a demo or thesis discussion.

The prototype is a low-cost IMU-based rowing sensing system. It records acceleration and angular velocity from one or two devices:

- `SEAT`: mounted on or near the sliding seat.
- `BOAT`: mounted on the boat shell as a reference sensor.

The central idea is to avoid exact absolute seat position and instead use relative and stroke-level metrics. In dual-IMU mode, the main signal is:

`relative = SEAT - BOAT`

This means the boat reference motion is subtracted from the seat signal.

## End-to-End Workflow

1. The Feather/IMU device starts and initializes the IMU, SD card, USB serial, and BLE UART.
2. The firmware samples the IMU at approximately 100 Hz.
3. Each sample is written as one CSV row.
4. The same row is sent over USB serial and, when a BLE client is connected, over BLE UART.
5. The Python live dashboard receives the CSV stream.
6. In dual-BLE mode, the dashboard connects to both `SEAT` and `BOAT`.
7. The dashboard parses each CSV row into acceleration and gyroscope values.
8. For dual-IMU analysis, the dashboard pairs the newest SEAT sample with the newest BOAT sample.
9. It computes `SEAT - BOAT` relative acceleration and rotation-rate differences.
10. It updates rolling plots, stroke detection, velocity proxies, smoothness values, and voice feedback.
11. Optional recordings are saved as CSV files for offline analysis.

## CSV Format

The current expected CSV format is:

`device_id,sequence,time_us,acc_x_ms2,acc_y_ms2,acc_z_ms2,gyro_x_rads,gyro_y_rads,gyro_z_rads`

The parser is backward-compatible with older 12-column rows that still contain magnetometer fields. In that case, the extra fields are ignored.

## Synchronization

The system is not hardware-synchronized.

In dual-BLE mode, each device sends its own BLE stream. The dashboard receives packets on the computer and pairs recent SEAT and BOAT samples by host arrival time. The alignment value is:

`alignment = |SEAT packet host time - BOAT packet host time|`

This is useful for debugging BLE timing, but it is not hardware-level synchronization. For a first prototype this is acceptable, but a future version should use better synchronization if precise timing is required.

## Stroke Start Detection

Stroke detection is based on the forward acceleration candidate axis.

1. The forward signal is taken from the current analysis stream.
   - In single-sensor mode this is usually `SEAT acc_x`.
   - In dual-BLE mode this is usually `SEAT acc_x - BOAT acc_x`.
2. A baseline is subtracted to center the signal.
3. A moving average smooths the signal.
4. Positive peaks are detected.
5. Peaks must be separated by a minimum time distance.
6. A stroke segment is defined from one detected peak to the next detected peak.

In simplified form:

`stroke_start = detected positive peak in smoothed centered forward acceleration`

The method is a first-pass estimate. It should be validated with video, manual labels, or a rowing reference system.

## Meaning of 0-100% Stroke Phase

The 0-100% stroke phase is time-based.

- `0%` means the first detected peak of a stroke segment.
- `100%` means the next detected peak.
- Intermediate values are created by resampling the stroke segment in time.

It does not mean 100% force, 100% power, or 100% seat travel. It means normalized time between two detected stroke events.

## Velocity and Speed Proxies

Velocity proxies are calculated by integrating acceleration:

`v_i = v_(i-1) + 0.5 * (a_i + a_(i-1)) * dt`

Before integration, the acceleration is centered and smoothed. After integration, a simple linear drift correction is applied.

These values are called proxies because they are not calibrated physical speed in meters per second. Low-cost IMU integration drifts over time without a trusted reference.

## Smoothness

Smoothness is estimated with a jerk-based roughness value. Jerk is the rate of change of acceleration:

`j_i = (a_i - a_(i-1)) / (t_i - t_(i-1))`

The displayed smoothness value is:

`smoothness roughness = RMS(jerk)`

Lower values mean a smoother acceleration signal. Higher values mean more abrupt changes, vibration, or noise.

## Graph Explanations

### SEAT vs BOAT Forward Acceleration

Sensors used: `SEAT` and `BOAT`.

Calculation:

- Brown: `SEAT acc_x_ms2`
- Green: `BOAT acc_x_ms2`
- Blue: `SEAT acc_x_ms2 - BOAT acc_x_ms2`

This is a time-history plot. The blue line is the main relative forward signal after removing boat reference acceleration.

### Forward Diagnostic: Subtraction and Sum

Sensors used: `SEAT` and `BOAT`.

Calculation:

- Blue: `SEAT acc_x_ms2 - BOAT acc_x_ms2`
- Grey: `SEAT acc_x_ms2 + BOAT acc_x_ms2`

The sum is only a diagnostic for shared movement, same-direction motion, or mounting drift. It is not a rowing score.

### BOAT and Relative Velocity Proxies

Sensors used:

- Green uses `BOAT`.
- Blue uses `SEAT - BOAT`.

Calculation:

- Green: `integral(smoothed BOAT acc_x)`
- Blue: `integral(smoothed(SEAT acc_x - BOAT acc_x))`

Both signals are centered, smoothed, integrated with the trapezoid rule, and drift-corrected. They are velocity proxies, not calibrated m/s.

### Relative Acceleration Axes

Sensors used: `SEAT` and `BOAT`.

Calculation:

- Forward: `SEAT acc_x - BOAT acc_x`
- Lateral: `SEAT acc_y - BOAT acc_y`
- Vertical: `SEAT acc_z - BOAT acc_z`

This shows which movement remains after subtracting the boat reference acceleration.

### Relative Rotation Rates

Sensors used: `SEAT` and `BOAT`.

Calculation:

- Roll: `SEAT gyro_x - BOAT gyro_x`
- Pitch: `SEAT gyro_y - BOAT gyro_y`
- Yaw: `SEAT gyro_z - BOAT gyro_z`

These are relative angular velocity differences between seat and boat.

### Current Stroke Timeline

Sensors used: normally the current relative stream, which is `SEAT - BOAT` in dual-BLE mode.

Calculation:

- The current stroke starts at the latest detected peak.
- Forward acceleration is centered and smoothed.
- Relative speed is the drift-corrected integral of that acceleration.
- Power proxy is `max(0, forward acceleration * relative speed)`.

The x-axis is seconds inside the current stroke.

### Stroke Power Transfer History

Sensors used: normally the current relative stream, which is `SEAT - BOAT` in dual-BLE mode.

Calculation:

- Each completed stroke is resampled to 0-100% phase.
- 100% is the next detected stroke peak.
- At each phase point:

`power proxy = max(0, smoothed forward acceleration * relative speed proxy)`

Grey lines are recent completed strokes. Blue is their phase-wise average. Brown is the current stroke. The value is not calibrated watts.

### Stroke Shape History

Sensors used: normally the current relative stream, which is `SEAT - BOAT` in dual-BLE mode.

Calculation:

- Each stroke is detected from peaks in smoothed forward acceleration.
- Each stroke is resampled to 0-100% time phase.
- Brown: current centered and smoothed forward acceleration.
- Green: current drift-corrected relative speed proxy.
- Grey: recent completed speed proxies.
- Blue: phase-wise average of recent speed proxies.

The 0-100% axis is normalized time, not force.

### SEAT Forward Acceleration History

Sensors used: `SEAT` only.

Calculation:

- Raw `SEAT acc_x_ms2`
- Baseline-centered
- Smoothed with a moving average

This is the seat-mounted forward/back acceleration history used for first-pass stroke detection.

### BOAT Forward Acceleration History

Sensors used: `BOAT` only.

Calculation:

- Raw `BOAT acc_x_ms2`

This is not subtracted. It helps check whether the boat reference unit is quiet and stable compared with the moving seat unit.

### SEAT Pitch and Yaw Rate History

Sensors used: `SEAT` only.

Calculation:

- Pitch: `SEAT gyro_y_rads`
- Yaw: `SEAT gyro_z_rads`

These are angular velocity signals in rad/s. They can show body swing, twisting, or sensor mounting movement.

## Voice Feedback

Voice feedback is intentionally short. It says:

- Stroke rate
- Relative speed
- Peak phase
- Smoothness

The feedback is spaced by a time interval to avoid a long speech queue. The macOS `say` process is stopped before a new sentence starts, so the newest feedback is prioritized.

## Important Limitations

- The system is not hardware-synchronized.
- Velocity values are proxies, not calibrated m/s.
- Power transfer is a proxy, not watts.
- 0-100% stroke phase is normalized time between detected peaks, not force.
- Stroke detection is peak-based and should be validated.
- Mounting quality strongly affects the signal.
- The enclosure must keep the sensor orientation stable.

## Short Demo Explanation

The prototype records IMU data from a seat-mounted unit and optionally a boat-mounted reference unit. The dashboard subtracts the boat signal from the seat signal to estimate relative seat-to-boat movement. Stroke starts are detected from peaks in smoothed forward acceleration. Completed strokes are normalized from 0 to 100 percent by time between detected peaks. The system then shows acceleration, rotation, velocity proxies, smoothness, and power-transfer proxies. The values are useful for comparing movement patterns, but they are not yet validated as exact biomechanical measurements.
