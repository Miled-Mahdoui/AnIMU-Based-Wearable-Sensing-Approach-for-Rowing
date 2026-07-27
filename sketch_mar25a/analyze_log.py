#!/usr/bin/env python3
"""Offline rowing IMU analysis for one SEAT file and optional BOAT reference.

The main offline signal is:

    relative = SEAT - BOAT

The report is self-contained HTML so it can be sent to another person without
Python, the CSV files, or the dashboard running.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev


DEFAULT_SEAT_CSV_FILE = Path("/Users/mahdoui/Downloads/LOG002.CSV")
DEFAULT_BOAT_CSV_FILE = Path("/Users/mahdoui/Downloads/BOAT.CSV")
DEFAULT_REPORT_FILE = Path("/Users/mahdoui/Downloads/imu_dual_report.html")
DEFAULT_FORWARD_COLUMN = "acc_y_ms2"
CSV_GLOBS = ("*.CSV", "*.csv")

AXES = (
    ("forward_acc", "acc_x_ms2", "m/s^2", "Longitudinal acceleration"),
    ("lateral_acc", "acc_y_ms2", "m/s^2", "Lateral acceleration"),
    ("vertical_acc", "acc_z_ms2", "m/s^2", "Vertical acceleration"),
    ("roll_rate", "gyro_x_rads", "rad/s", "Roll rate"),
    ("pitch_rate", "gyro_y_rads", "rad/s", "Pitch rate"),
    ("yaw_rate", "gyro_z_rads", "rad/s", "Yaw rate"),
)

PLOT_COLORS = {
    "seat": "#7c2d12",
    "boat": "#0f766e",
    "relative": "#1d4ed8",
    "velocity": "#4338ca",
    "power": "#b91c1c",
    "phase": "#64748b",
}


@dataclass(frozen=True)
class Tuning:
    baseline_samples: int
    smooth_window: int
    min_peak_distance_s: float


@dataclass(frozen=True)
class SegmentSpec:
    label: str
    start_s: float
    end_s: float


def parse_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def csv_files_in_folder(folder: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in CSV_GLOBS:
        files.extend(folder.glob(pattern))
    return sorted(
        {path.resolve() for path in files if path.is_file()},
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def sniff_device_id(path: Path) -> str:
    try:
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            first = next(reader, None)
            if first:
                return first.get("device_id", "").strip().upper()
    except (OSError, csv.Error, UnicodeDecodeError):
        return ""
    return ""


def discover_input_files(input_path: Path, optional_second: Path | None) -> tuple[Path, Path | None, str]:
    """Resolve CLI input to a SEAT file and optional BOAT file.

    Supported forms:
    - analyze_log.py /path/to/folder
    - analyze_log.py /path/to/seat.csv
    - analyze_log.py /path/to/seat.csv /path/to/boat.csv
    """
    if optional_second:
        return input_path, optional_second, "Using the two CSV files provided on the command line."

    if input_path.is_dir():
        candidates = csv_files_in_folder(input_path)
        if not candidates:
            raise SystemExit(f"No CSV files found in folder: {input_path}")
        seat_candidates = [path for path in candidates if sniff_device_id(path) == "SEAT"]
        boat_candidates = [path for path in candidates if sniff_device_id(path) == "BOAT"]
        seat_file = seat_candidates[0] if seat_candidates else candidates[0]
        boat_file = boat_candidates[0] if boat_candidates else None
        if boat_file and boat_file.resolve() == seat_file.resolve():
            boat_file = None
        note = (
            f"Folder mode: selected SEAT={seat_file.name}"
            + (f", BOAT={boat_file.name}." if boat_file else ". No BOAT CSV detected.")
        )
        return seat_file, boat_file, note

    if input_path.is_file():
        return input_path, None, "Single-file mode: one SEAT CSV was provided."

    raise SystemExit(f"Input path not found: {input_path}")


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = dict(raw)
            row["device_id"] = row.get("device_id", "").strip() or path.stem
            row["sequence"] = int(parse_float(row.get("sequence", "0")))
            row["time_us"] = int(parse_float(row.get("time_us", "0")))
            for _, column, _, _ in AXES:
                row[column] = parse_float(row.get(column, "nan"))
            rows.append(row)
    if not rows:
        raise ValueError(f"No CSV rows found in {path}")
    return rows


def normalize_times(rows: list[dict]) -> list[float]:
    start = rows[0]["time_us"]
    return [(row["time_us"] - start) / 1_000_000.0 for row in rows]


def values_for(rows: list[dict], column: str) -> list[float]:
    return [float(row[column]) for row in rows if math.isfinite(float(row[column]))]


def safe_mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def safe_std(values: list[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0


def rms(values: list[float]) -> float:
    return math.sqrt(mean(value * value for value in values)) if values else 0.0


def summarize_values(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "rms": 0.0,
            "range": 0.0,
        }
    low = min(values)
    high = max(values)
    return {
        "min": low,
        "max": high,
        "mean": mean(values),
        "std": safe_std(values),
        "rms": rms(values),
        "range": high - low,
    }


def moving_average(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    window = max(1, window)
    result: list[float] = []
    queue: list[float] = []
    running = 0.0
    for value in values:
        queue.append(value)
        running += value
        if len(queue) > window:
            running -= queue.pop(0)
        result.append(running / len(queue))
    return result


def centered(values: list[float], baseline_samples: int) -> list[float]:
    if not values:
        return []
    count = max(1, min(baseline_samples, len(values)))
    baseline = mean(values[:count])
    return [value - baseline for value in values]


def integrate_velocity(times: list[float], acceleration: list[float], correct_drift: bool = True) -> list[float]:
    if not acceleration:
        return []
    velocity = [0.0]
    for index in range(1, len(acceleration)):
        dt = max(0.0, times[index] - times[index - 1])
        area = 0.5 * (acceleration[index] + acceleration[index - 1]) * dt
        velocity.append(velocity[-1] + area)
    if correct_drift and len(velocity) > 1:
        drift_per_sample = velocity[-1] / (len(velocity) - 1)
        velocity = [value - drift_per_sample * index for index, value in enumerate(velocity)]
    return velocity


def jerk_rms(times: list[float], smoothed_acc: list[float]) -> float:
    jerks: list[float] = []
    for index in range(1, len(smoothed_acc)):
        dt = times[index] - times[index - 1]
        if dt > 1e-9:
            jerks.append((smoothed_acc[index] - smoothed_acc[index - 1]) / dt)
    return rms(jerks)


def positive_peak_index(values: list[float]) -> int:
    if not values:
        return 0
    return max(range(len(values)), key=lambda index: values[index])


def positive_peak_value(values: list[float]) -> float:
    if not values:
        return 0.0
    return max(0.0, max(values))


def strongest_drive_acceleration(acceleration: list[float], velocity: list[float]) -> float:
    drive_values = [abs(acc) for acc, vel in zip(acceleration, velocity) if vel > 0.0]
    if drive_values:
        return max(drive_values)
    return max((abs(acc) for acc in acceleration), default=0.0)


def local_speed_curve(velocity: list[float], tuning: Tuning) -> list[float]:
    if not velocity:
        return []
    window = max(15, tuning.smooth_window * 3)
    local_baseline = moving_average(velocity, window)
    return [value - baseline for value, baseline in zip(velocity, local_baseline)]


def detect_drive_starts(times: list[float], velocity: list[float], tuning: Tuning) -> tuple[list[int], float, list[float]]:
    """Detect drive starts from the direction of the seat-relative velocity curve.

    A drive start is the moment where the internal velocity curve enters the
    positive drive direction after it has returned through recovery direction.
    """
    if len(velocity) < 3:
        return [], 0.0, []
    drive_velocity = local_speed_curve(velocity, tuning)
    max_abs = max(abs(value) for value in drive_velocity)
    threshold = max(0.12 * safe_std(drive_velocity), 0.04 * max_abs, 1e-6)
    starts: list[int] = []
    last_start_time = -1e9
    for index in range(1, len(drive_velocity)):
        if drive_velocity[index - 1] <= threshold < drive_velocity[index]:
            if times[index] - last_start_time >= tuning.min_peak_distance_s:
                starts.append(index)
                last_start_time = times[index]
    return starts, threshold, drive_velocity


def interpolate_at(source_times: list[float], source_values: list[float], target_times: list[float]) -> list[float]:
    if not source_times or not source_values:
        return [0.0 for _ in target_times]
    result: list[float] = []
    cursor = 0
    last_index = len(source_times) - 1
    for target in target_times:
        while cursor < last_index - 1 and source_times[cursor + 1] < target:
            cursor += 1
        if target <= source_times[0]:
            result.append(source_values[0])
            continue
        if target >= source_times[-1]:
            result.append(source_values[-1])
            continue
        left_t = source_times[cursor]
        right_t = source_times[cursor + 1]
        left_v = source_values[cursor]
        right_v = source_values[cursor + 1]
        ratio = (target - left_t) / max(1e-12, right_t - left_t)
        result.append(left_v + ratio * (right_v - left_v))
    return result


def interpolate_segment(times: list[float], values: list[float], start_index: int, end_index: int, count: int = 101) -> list[float]:
    if end_index <= start_index:
        return [values[start_index] if values else 0.0] * count
    start_t = times[start_index]
    end_t = times[end_index]
    segment_times = times[start_index : end_index + 1]
    segment_values = values[start_index : end_index + 1]
    targets = [start_t + (end_t - start_t) * phase / (count - 1) for phase in range(count)]
    return interpolate_at(segment_times, segment_values, targets)


def average_series(series: list[list[float]]) -> list[float]:
    if not series:
        return []
    length = min(len(item) for item in series)
    return [mean(item[index] for item in series) for index in range(length)]


def sequence_gap_summary(rows: list[dict]) -> dict[str, int]:
    if not rows:
        return {"first": 0, "last": 0, "samples": 0, "gaps": 0}
    first = int(rows[0]["sequence"])
    last = int(rows[-1]["sequence"])
    samples = len(rows)
    return {
        "first": first,
        "last": last,
        "samples": samples,
        "gaps": max(0, last - first + 1 - samples),
    }


def sampling_summary(times: list[float]) -> dict[str, float]:
    intervals = [(times[i] - times[i - 1]) for i in range(1, len(times))]
    duration = times[-1] - times[0] if len(times) > 1 else 0.0
    return {
        "duration_s": duration,
        "rate_hz": (len(times) - 1) / duration if duration > 0 else 0.0,
        "mean_ms": mean(intervals) * 1000.0 if intervals else 0.0,
        "std_ms": safe_std(intervals) * 1000.0 if intervals else 0.0,
        "min_ms": min(intervals) * 1000.0 if intervals else 0.0,
        "max_ms": max(intervals) * 1000.0 if intervals else 0.0,
    }


def build_dual_samples(seat_rows: list[dict], boat_rows: list[dict] | None) -> dict:
    seat_times_all = normalize_times(seat_rows)
    if not boat_rows:
        return {
            "times": seat_times_all,
            "seat": {column: values_for(seat_rows, column) for _, column, _, _ in AXES},
            "boat": None,
            "relative": None,
            "alignment_note": "Single SEAT file only; no BOAT reference was used.",
        }

    boat_times = normalize_times(boat_rows)
    overlap_start = max(seat_times_all[0], boat_times[0])
    overlap_end = min(seat_times_all[-1], boat_times[-1])
    indices = [
        index
        for index, time_s in enumerate(seat_times_all)
        if overlap_start <= time_s <= overlap_end
    ]
    if len(indices) < 3:
        raise ValueError("The SEAT and BOAT recordings do not overlap enough in time.")

    times = [seat_times_all[index] for index in indices]
    seat = {
        column: [float(seat_rows[index][column]) for index in indices]
        for _, column, _, _ in AXES
    }
    boat = {
        column: interpolate_at(boat_times, values_for(boat_rows, column), times)
        for _, column, _, _ in AXES
    }
    relative = {
        column: [seat[column][i] - boat[column][i] for i in range(len(times))]
        for _, column, _, _ in AXES
    }
    return {
        "times": times,
        "seat": seat,
        "boat": boat,
        "relative": relative,
        "alignment_note": (
            "BOAT values are linearly interpolated to SEAT timestamps after both "
            "files are normalized to their own first timestamp."
        ),
    }


def analyze_signal(times: list[float], forward: list[float], tuning: Tuning) -> dict:
    centered_forward = centered(forward, tuning.baseline_samples)
    smoothed_forward = moving_average(centered_forward, tuning.smooth_window)
    velocity = integrate_velocity(times, smoothed_forward, correct_drift=True)
    drive_starts, threshold, drive_velocity = detect_drive_starts(times, velocity, tuning)
    segments = list(zip(drive_starts, drive_starts[1:]))
    durations = [times[end] - times[start] for start, end in segments]
    speed_peak_index = positive_peak_index(drive_velocity)
    duration_s = max(0.001, times[-1] - times[0]) if times else 0.001
    power = [max(0.0, acc * vel) for acc, vel in zip(smoothed_forward, drive_velocity)]

    phase_acc_segments: list[list[float]] = []
    phase_velocity_segments: list[list[float]] = []
    phase_power_segments: list[list[float]] = []
    per_stroke: list[dict[str, float]] = []
    for stroke_number, (start, end) in enumerate(segments, start=1):
        local_times = [time_s - times[start] for time_s in times[start : end + 1]]
        acc_segment = smoothed_forward[start : end + 1]
        vel_segment = drive_velocity[start : end + 1]
        power_segment = [max(0.0, acc * vel) for acc, vel in zip(acc_segment, vel_segment)]
        phase_acc = interpolate_segment(times, smoothed_forward, start, end)
        phase_vel = interpolate_at(local_times, vel_segment, [local_times[-1] * phase / 100.0 for phase in range(101)])
        phase_power = interpolate_at(local_times, power_segment, [local_times[-1] * phase / 100.0 for phase in range(101)])
        phase_acc_segments.append(phase_acc)
        phase_velocity_segments.append(phase_vel)
        phase_power_segments.append(phase_power)
        local_peak = positive_peak_index(vel_segment)
        per_stroke.append(
            {
                "stroke": float(stroke_number),
                "start_s": times[start],
                "end_s": times[end],
                "duration_s": times[end] - times[start],
                "spm": 60.0 / (times[end] - times[start]) if times[end] > times[start] else 0.0,
                "peak_acc": strongest_drive_acceleration(acc_segment, vel_segment),
                "speed_proxy": positive_peak_value(vel_segment),
                "speed_peak_phase_pct": (local_times[local_peak] / max(0.001, local_times[-1]) * 100.0) if local_times else 0.0,
                "peak_power_proxy": max(power_segment) if power_segment else 0.0,
                "smoothness_jerk_rms": jerk_rms(local_times, acc_segment),
            }
        )

    rhythm_std = safe_std(durations)
    rhythm_mean = safe_mean(durations)
    rhythm_cv_pct = (rhythm_std / rhythm_mean * 100.0) if rhythm_mean > 1e-9 else 0.0
    rhythm_score = max(0.0, 100.0 - rhythm_cv_pct)
    avg_speed_peak_phase = safe_mean([item["speed_peak_phase_pct"] for item in per_stroke])

    return {
        "raw_forward": forward,
        "centered_forward": centered_forward,
        "smoothed_forward": smoothed_forward,
        "velocity_proxy": drive_velocity,
        "power_proxy": power,
        "peaks": drive_starts,
        "threshold": threshold,
        "segments": segments,
        "stroke_count": len(segments),
        "stroke_rate_spm": len(segments) / duration_s * 60.0,
        "avg_stroke_duration_s": rhythm_mean,
        "min_stroke_duration_s": min(durations) if durations else 0.0,
        "max_stroke_duration_s": max(durations) if durations else 0.0,
        "rhythm_std_s": rhythm_std,
        "rhythm_cv_pct": rhythm_cv_pct,
        "rhythm_consistency_score": rhythm_score,
        "speed_proxy": positive_peak_value(drive_velocity),
        "speed_peak_time_s": times[speed_peak_index] if drive_velocity else 0.0,
        "speed_peak_phase_pct": avg_speed_peak_phase,
        "peak_force_proxy": strongest_drive_acceleration(smoothed_forward, drive_velocity),
        "peak_power_proxy": max(power) if power else 0.0,
        "smoothness_jerk_rms": jerk_rms(times, smoothed_forward),
        "phase": list(range(101)),
        "avg_phase_acc": average_series(phase_acc_segments),
        "avg_phase_velocity": average_series(phase_velocity_segments),
        "avg_phase_power": average_series(phase_power_segments),
        "per_stroke": per_stroke,
    }


def slice_dual_data(dual: dict, start_s: float | None, end_s: float | None) -> dict:
    times = dual["times"]
    if start_s is None:
        start_s = times[0]
    if end_s is None:
        end_s = times[-1]
    indices = [index for index, value in enumerate(times) if start_s <= value <= end_s]
    if len(indices) < 3:
        raise ValueError(f"Segment {start_s:.2f}-{end_s:.2f}s has too few samples.")
    sliced = {
        "times": [times[index] for index in indices],
        "seat": {
            column: [values[index] for index in indices]
            for column, values in dual["seat"].items()
        },
        "boat": None,
        "relative": None,
        "alignment_note": dual["alignment_note"],
    }
    if dual["boat"]:
        sliced["boat"] = {
            column: [values[index] for index in indices]
            for column, values in dual["boat"].items()
        }
    if dual["relative"]:
        sliced["relative"] = {
            column: [values[index] for index in indices]
            for column, values in dual["relative"].items()
        }
    return sliced


def downsample(times: list[float], values: list[float], max_points: int = 900) -> tuple[list[float], list[float]]:
    if len(times) <= max_points:
        return times, values
    step = math.ceil(len(times) / max_points)
    return times[::step], values[::step]


def make_svg(title: str, series: list[tuple[str, list[float], list[float], str]], unit: str = "", x_label: str = "time (s)") -> str:
    width = 980
    height = 320
    left = 56
    right = 18
    top = 38
    bottom = 42
    all_x = [x for _, xs, _, _ in series for x in xs]
    all_y = [y for _, _, ys, _ in series for y in ys if math.isfinite(y)]
    if not all_x or not all_y:
        return f"<p class='note'>No plot data for {html.escape(title)}.</p>"
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    if math.isclose(min_x, max_x):
        max_x = min_x + 1.0
    if math.isclose(min_y, max_y):
        min_y -= 1.0
        max_y += 1.0
    pad_y = (max_y - min_y) * 0.08
    min_y -= pad_y
    max_y += pad_y

    plot_w = width - left - right
    plot_h = height - top - bottom

    def sx(value: float) -> float:
        return left + (value - min_x) / (max_x - min_x) * plot_w

    def sy(value: float) -> float:
        return top + (max_y - value) / (max_y - min_y) * plot_h

    parts = [
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)}'>",
        f"<text x='{left}' y='22' class='svg-title'>{html.escape(title)}</text>",
        f"<line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/>",
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in range(6):
        ratio = tick / 5
        x = left + ratio * plot_w
        value = min_x + ratio * (max_x - min_x)
        parts.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_h}' class='grid'/>")
        parts.append(f"<text x='{x:.1f}' y='{height - 16}' class='tick'>{value:.1f}</text>")
    for tick in range(5):
        ratio = tick / 4
        y = top + ratio * plot_h
        value = max_y - ratio * (max_y - min_y)
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{left - 8}' y='{y + 4:.1f}' class='tick right'>{value:.2f}</text>")

    legend_x = left + 8
    for label, xs_raw, ys_raw, color in series:
        xs, ys = downsample(xs_raw, ys_raw)
        points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys) if math.isfinite(y))
        if points:
            parts.append(f"<polyline points='{points}' fill='none' stroke='{color}' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>")
        parts.append(f"<rect x='{legend_x}' y='{height - 34}' width='10' height='10' fill='{color}'/>")
        parts.append(f"<text x='{legend_x + 15}' y='{height - 25}' class='legend'>{html.escape(label)}</text>")
        legend_x += 180
    parts.append(f"<text x='{left + plot_w / 2}' y='{height - 2}' class='tick'>{html.escape(x_label)}</text>")
    if unit:
        parts.append(f"<text x='12' y='{top + 12}' class='tick'>{html.escape(unit)}</text>")
    parts.append("</svg>")
    return "\n".join(parts)


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if math.isfinite(value) else "-"


def table_rows(items: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{value}</td><td>{html.escape(note)}</td></tr>"
        for name, value, note in items
    )


def metric_card(label: str, value: str, note: str = "") -> str:
    return (
        "<div class='metric'>"
        f"<span>{html.escape(label)}</span>"
        f"<strong>{value}</strong>"
        f"<small>{html.escape(note)}</small>"
        "</div>"
    )


def make_interactive_dashboard(dual: dict, tuning: Tuning) -> str:
    payload = {
        "times": dual["times"],
        "seat": dual["seat"],
        "boat": dual["boat"],
        "relative": dual["relative"],
        "hasBoat": bool(dual["boat"]),
        "tuning": {
            "baselineSamples": tuning.baseline_samples,
            "smoothWindow": tuning.smooth_window,
            "minPeakMs": tuning.min_peak_distance_s * 1000.0,
        },
    }
    data_json = json.dumps(payload, separators=(",", ":"))
    return f"""
<section id="interactiveDashboard" class="interactive">
  <div class="controls">
    <label><span data-i18n="language">Language</span>
      <select id="languageSelect">
        <option value="en">English</option>
        <option value="pl">Polski</option>
      </select>
    </label>
  </div>
  <h2 data-i18n="coachDashboard">Coach dashboard</h2>
  <p class="note" data-i18n="coachDashboardNote">Use this section to choose the forward axis, split one recording into athletes, inspect a single stroke, and export separate HTML reports.</p>
  <h3 data-i18n="sensorAlignment">Sensor alignment</h3>
  <div class="controls">
    <label><span data-i18n="forwardAxis">Forward axis</span>
      <select id="axisSelect">
        <option value="acc_x_ms2" data-i18n="xLongitudinal">X longitudinal</option>
        <option value="acc_y_ms2" selected data-i18n="yLateralForward">Y lateral as forward</option>
        <option value="acc_z_ms2" data-i18n="zVerticalForward">Z vertical as forward</option>
      </select>
    </label>
    <label><input id="invertAxis" type="checkbox"> <span data-i18n="invertForwardSign">invert forward sign</span></label>
    <label><input id="invertSeatOnly" type="checkbox"> <span data-i18n="invertSeatOnly">invert SEAT only</span></label>
    <label><input id="swapSeatBoat" type="checkbox"> <span data-i18n="swapSeatBoat">swap SEAT/BOAT data</span></label>
  </div>
  <h3 data-i18n="strokeDetectionTuning">Stroke detection tuning</h3>
  <p class="note" data-i18n="strokeDetectionNote">For a new setup, count the strokes manually in the first minute. Then adjust these two controls until the dashboard stroke count matches the manual count as closely as possible.</p>
  <div class="controls">
    <label><span data-i18n="minimumStrokeGap">Stroke detection: minimum gap ms</span> <input id="minPeakMs" type="number" step="10"></label>
    <label><span data-i18n="smoothingSamples">Stroke detection: smoothing samples</span> <input id="smoothWindow" type="number" step="1" min="1"></label>
  </div>
  <p class="note" data-i18n="smoothingNote">Minimum gap controls how soon the next stroke is allowed to start. Smoothing calms the acceleration before velocity is estimated, so small vibrations are less likely to become false strokes.</p>
  <h3 data-i18n="timeGraph">Time graph</h3>
  <div class="controls">
    <label><input class="timeMetric" value="relativeAcc" type="checkbox"> <span data-i18n="relativeAcceleration">SEAT-only acceleration</span></label>
    <label><input class="timeMetric" value="seatAcc" type="checkbox" checked> <span data-i18n="seatAcceleration">SEAT-only acceleration</span></label>
    <label><input class="timeMetric" value="boatAcc" type="checkbox" checked> <span data-i18n="boatAcceleration">BOAT-only acceleration</span></label>
    <label><input id="normalizeTimeGraph" type="checkbox" checked> <span data-i18n="normalizeGraphLines">normalize graph lines</span></label>
    <label><input id="showStrokeEvents" type="checkbox" checked> <span data-i18n="strokeStartEndLines">stroke start/end lines</span></label>
    <label><span data-i18n="timeLabelDetail">Time label detail</span>
      <select id="timePrecision">
        <option value="1" data-i18n="simple">simple</option>
        <option value="2" data-i18n="moreExact">more exact</option>
        <option value="0" data-i18n="rough">rough</option>
      </select>
    </label>
    <label><span data-i18n="zoomStart">Zoom start s</span> <input id="zoomStart" type="number" step="0.1"></label>
    <label><span data-i18n="zoomEnd">Zoom end s</span> <input id="zoomEnd" type="number" step="0.1"></label>
    <button id="applyZoom" type="button" data-i18n="applyZoom">Apply zoom</button>
    <button id="resetZoom" type="button" data-i18n="resetZoom">Reset zoom</button>
    <button id="addSegment" type="button" data-i18n="saveVisibleRange">Save visible time range as athlete</button>
  </div>
  <div id="interactiveMetrics" class="grid"></div>
  <h3 data-i18n="segmentsAthletes">Segments / athletes</h3>
  <p class="note" data-i18n="segmentsNote">Set a visible time range with the graph zoom or zoom fields, then save it as an athlete. Rename athletes, add coach notes, and export each row as HTML.</p>
  <table id="segmentTable">
    <thead><tr><th data-i18n="use">Use</th><th data-i18n="name">Name</th><th data-i18n="time">Time</th><th data-i18n="strokes">Strokes</th><th data-i18n="strokeRate">Stroke rate</th><th data-i18n="rhythmConsistency">Rhythm consistency</th><th data-i18n="strongestSeatDriveSpeed">Strongest seat drive speed</th><th data-i18n="smoothness">Smoothness</th><th data-i18n="coachMessageBeforeExport">Coach message before export</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
  <canvas id="timeCanvas" width="1100" height="520"></canvas>
  <p class="note" data-i18n="timeGraphNote">Time graph: shows the first enabled segment. Blue lines mark detected stroke starts, red lines mark detected stroke ends. Drag across the graph to zoom; double click to reset.</p>
  <h3 data-i18n="selectedStroke">Selected stroke</h3>
  <div class="controls">
    <label><span data-i18n="strokeToInspect">Stroke to inspect</span>
      <select id="strokeSelect"></select>
    </label>
    <label><input class="strokeMetric" value="acc" type="checkbox" checked> <span data-i18n="relativeAcceleration">SEAT-only acceleration</span></label>
    <label><input class="strokeMetric" value="seatAcc" type="checkbox"> <span data-i18n="seatAcceleration">SEAT-only acceleration</span></label>
    <label><input class="strokeMetric" value="boatAcc" type="checkbox"> <span data-i18n="boatAcceleration">BOAT-only acceleration</span></label>
    <label><input class="strokeMetric" value="velocity" type="checkbox"> <span data-i18n="relativeVelocityProxy">SEAT movement speed proxy</span></label>
    <label><input class="strokeMetric" value="seatVelocity" type="checkbox"> <span data-i18n="seatVelocityProxy">SEAT velocity proxy</span></label>
    <label><input class="strokeMetric" value="boatVelocity" type="checkbox"> <span data-i18n="boatVelocityProxy">BOAT velocity proxy</span></label>
    <label><input id="showAverageStroke" type="checkbox" checked> <span data-i18n="showSegmentAverage">show segment average</span></label>
    <label><span data-i18n="phaseLabels">Phase labels</span>
      <select id="phaseTickStep">
        <option value="10">10%</option>
        <option value="5">5%</option>
        <option value="20">20%</option>
      </select>
    </label>
  </div>
  <div id="strokeMetrics" class="grid"></div>
  <canvas id="strokeCanvas" width="1100" height="440"></canvas>
  <p class="note" data-i18n="singleStrokeGraphNote">Single-stroke graph: solid lines show the selected stroke; dashed lines show the segment average. The purple vertical line marks where the seat moves fastest in the stroke. Drag to zoom inside the phase graph; double click to reset.</p>
  <canvas id="strokeVelocityCanvas" width="1100" height="360"></canvas>
  <p class="note" data-i18n="selectedStrokeVelocityNote">Selected-stroke velocity view: same selected stroke, focused on velocity proxies. The purple line marks the strongest seat drive speed.</p>
  <h3 data-i18n="strokeOverlay">Stroke overlay</h3>
  <div class="controls">
    <label><span data-i18n="overlayMetric">Overlay metric</span>
      <select id="overlayMetric">
        <option value="velocity" data-i18n="relativeVelocityProxy">SEAT movement speed proxy</option>
        <option value="boatVelocity" data-i18n="boatVelocityProxy">BOAT velocity proxy</option>
        <option value="seatVelocity" data-i18n="seatVelocityProxy">SEAT velocity proxy</option>
        <option value="acc" data-i18n="relativeAcceleration">SEAT-only acceleration</option>
        <option value="seatAcc" data-i18n="seatAcceleration">SEAT-only acceleration</option>
        <option value="boatAcc" data-i18n="boatAcceleration">BOAT-only acceleration</option>
      </select>
    </label>
    <span id="strokeOverlayControls"></span>
  </div>
  <canvas id="strokeOverlayCanvas" width="1100" height="400"></canvas>
  <p class="note" data-i18n="strokeOverlayNote">Stroke overlay: selected strokes start at 0s and keep their real duration, so timing differences stay visible.</p>
  <h3 data-i18n="strokeByStrokeValues">Stroke-by-stroke values</h3>
  <table id="allStrokeTable">
    <thead><tr><th>#</th><th data-i18n="athlete">Athlete</th><th data-i18n="time">Time</th><th data-i18n="strokeTime">Stroke time</th><th data-i18n="strokeRate">Stroke rate</th><th data-i18n="strongestSeatDriveSpeed">Strongest seat drive speed</th><th data-i18n="peakAcceleration">Strongest SEAT drive acceleration</th><th data-i18n="boatVelocityChange">BOAT velocity change</th><th data-i18n="smoothness">Smoothness</th></tr></thead>
    <tbody></tbody>
  </table>
</section>
<script>
const IMU_DATA = {data_json};
const I18N = {{
  en: {{}},
  pl: {{
    language: "Język",
    coachDashboard: "Panel trenera",
    athleteDashboard: "Panel zawodnika",
    coachDashboardNote: "W tej sekcji można wybrać oś ruchu, podzielić nagranie na zawodników, obejrzeć pojedyncze pociągnięcie i wyeksportować osobne raporty HTML.",
    forwardAxis: "Oś kierunku ruchu",
    xLongitudinal: "X wzdłużna",
    yLateralForward: "Y boczna jako kierunek jazdy",
    zVerticalForward: "Z pionowa jako kierunek jazdy",
    invertForwardSign: "odwróć znak kierunku",
    invertSeatOnly: "odwróć tylko SEAT",
    swapSeatBoat: "zamień dane SEAT/BOAT",
    sensorAlignment: "Ustawienie czujników",
    strokeDetectionTuning: "Dostrajanie wykrywania pociągnięć",
    strokeDetectionNote: "Przy nowym ustawieniu policz ręcznie pociągnięcia w pierwszej minucie. Potem dostosuj te dwa ustawienia, aż liczba pociągnięć w panelu będzie możliwie bliska ręcznemu liczeniu.",
    minimumStrokeGap: "Wykrywanie pociągnięć: minimalny odstęp ms",
    smoothingSamples: "Wykrywanie pociągnięć: wygładzanie próbek",
    smoothingNote: "Minimalny odstęp określa, jak szybko może rozpocząć się kolejne pociągnięcie. Wygładzanie uspokaja przyspieszenie przed estymacją prędkości, dzięki czemu małe drgania rzadziej tworzą fałszywe pociągnięcia.",
    relativeAcceleration: "Przyspieszenie tylko SEAT",
    seatAcceleration: "Przyspieszenie tylko SEAT",
    boatAcceleration: "Przyspieszenie tylko BOAT",
    normalizeGraphLines: "normalizuj linie wykresu",
    strokeStartEndLines: "linie początku/końca pociągnięcia",
    timeLabelDetail: "Dokładność etykiet czasu",
    simple: "prosto",
    moreExact: "dokładniej",
    rough: "zgrubnie",
    zoomStart: "Początek zoomu s",
    zoomEnd: "Koniec zoomu s",
    applyZoom: "Zastosuj zoom",
    resetZoom: "Resetuj zoom",
    saveVisibleRange: "Zapisz widoczny zakres jako zawodnika",
    segmentsAthletes: "Segmenty / zawodnicy",
    segmentsNote: "Ustaw widoczny zakres czasu przez zoom lub pola czasu, a potem zapisz go jako zawodnika. Możesz zmienić nazwy, dodać notatki trenera i wyeksportować każdy wiersz jako HTML.",
    use: "Użyj",
    name: "Nazwa",
    time: "Czas",
    strokes: "Pociągnięcia",
    strokeRate: "Tempo pociągnięć",
    rhythmConsistency: "Regularność rytmu",
    strongestSeatDriveSpeed: "Największa prędkość siedziska w napędzie",
    smoothness: "Płynność",
    coachMessageBeforeExport: "Komentarz trenera przed eksportem",
    timeGraphNote: "Wykres czasu pokazuje pierwszy aktywny segment. Niebieskie linie oznaczają początki pociągnięć, czerwone końce. Przeciągnij po wykresie, aby powiększyć; kliknij dwa razy, aby zresetować.",
    timeGraph: "Wykres czasu",
    strokeToInspect: "Pociągnięcie do analizy",
    relativeVelocityProxy: "Proxy prędkości ruchu SEAT",
    seatVelocityProxy: "Proxy prędkości SEAT",
    boatVelocityProxy: "Proxy prędkości BOAT",
    showSegmentAverage: "pokaż średnią segmentu",
    phaseLabels: "Etykiety fazy",
    singleStrokeGraphNote: "Wykres pojedynczego pociągnięcia: linie ciągłe pokazują wybrane pociągnięcie, przerywane średnią segmentu. Fioletowa linia pokazuje miejsce największej prędkości siedziska. Przeciągnij, aby powiększyć; kliknij dwa razy, aby zresetować.",
    selectedStrokeVelocityNote: "Widok prędkości wybranego pociągnięcia: skupia się na proxy prędkości. Fioletowa linia oznacza największą prędkość siedziska w napędzie.",
    overlayMetric: "Metryka nakładania",
    strokeOverlay: "Nakładanie pociągnięć",
    strokeOverlayNote: "Nakładanie pociągnięć: wybrane pociągnięcia zaczynają się od 0 s i zachowują rzeczywisty czas trwania, więc różnice czasowe pozostają widoczne.",
    strokeByStrokeValues: "Wartości pociągnięcie po pociągnięciu",
    strokeTime: "Czas pociągnięcia",
    peakAcceleration: "Największe przyspieszenie SEAT w napędzie",
    boatVelocityGain: "Zmiana proxy prędkości BOAT",
    boatVelocityChange: "Zmiana proxy prędkości BOAT",
    axis: "Oś",
    detectedStrokes: "Wykryte pociągnięcia",
    inSelectedRecording: "W wybranym nagraniu",
    averagePace: "Średnie tempo",
    higherRegularTiming: "Wyżej oznacza bardziej regularny rytm",
    lowerLessJerky: "Niżej oznacza mniej szarpany ruch siedziska",
    fullRecording: "Całe nagranie",
    shortNoteForAthlete: "Krótka notatka dla zawodnika",
    remove: "Usuń",
    noCompleteStroke: "Brak pełnego pociągnięcia w tym segmencie.",
    selectedStroke: "Wybrane pociągnięcie",
    timeDriveChange: "Czas od jednej zmiany kierunku napędu do następnej",
    paceForStroke: "Tempo dla tego pociągnięcia",
    whereSeatFastest: "Miejsce, w którym siedzisko porusza się najszybciej",
    strongestSeatAcceleration: "Największa siła przyspieszenia siedziska podczas ruchu napędowego",
    boatVelocityGainNote: "Zmiana proxy prędkości BOAT od początku do końca pociągnięcia; może być dodatnia albo ujemna",
    boatVelocityChangeNote: "Zmiana proxy prędkości BOAT od początku do końca pociągnięcia; może być dodatnia albo ujemna",
    lowerSmoother: "Niżej oznacza płynniejszy ruch siedziska",
    strongestSpeed: "największa prędkość",
    selectedStrokeSpeedCurve: "krzywa prędkości wybranego pociągnięcia",
    avgSpeedCurve: "średnia krzywa prędkości",
    timeInSelectedSegment: "czas w wybranym segmencie (s)",
    strokePhase: "faza pociągnięcia (%)",
    strokeTimeAxis: "czas pociągnięcia (s)",
    noData: "Brak danych w tym wykresie/zakresie zoomu",
    byStrokeTime: "według czasu pociągnięcia (s)",
    start: "początek",
    end: "koniec",
    athlete: "Zawodnik",
    globalSignInverted: "odwrócony znak globalny",
    seatSignInverted: "odwrócony znak SEAT",
    seatBoatSwapped: "SEAT/BOAT zamienione",
    seatBoatMode: "tryb dwóch czujników",
    seatOnlyMode: "tylko SEAT",
  }}
}};
let currentLanguage = "en";
function tr(key) {{
  return (I18N[currentLanguage] && I18N[currentLanguage][key]) || key;
}}
function text(key, fallback) {{
  return currentLanguage === "pl" ? tr(key) : fallback;
}}
function applyLanguage() {{
  document.querySelectorAll("[data-i18n]").forEach(element => {{
    const key = element.dataset.i18n;
    if (currentLanguage === "pl" && I18N.pl[key]) element.textContent = I18N.pl[key];
    else if (element.dataset.en) element.textContent = element.dataset.en;
  }});
}}
function initLanguage() {{
  document.querySelectorAll("[data-i18n]").forEach(element => {{
    if (!element.dataset.en) element.dataset.en = element.textContent;
  }});
  applyLanguage();
}}
const axisNames = {{
  acc_x_ms2: "X / longitudinal",
  acc_y_ms2: "Y / lateral",
  acc_z_ms2: "Z / vertical",
}};
const palette = ["#1d4ed8", "#b91c1c", "#0f766e", "#7c2d12", "#6d28d9", "#be123c", "#0369a1", "#4d7c0f"];
let segments = [];
let zoomRange = null;
let strokeZoomRange = null;
let strokeVelocityZoomRange = null;
let strokeOverlayZoomRange = null;
let selectedOverlayStrokes = new Set();
let selectedStrokeIndex = 0;

function mean(values) {{
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
}}
function std(values) {{
  if (values.length < 2) return 0;
  const m = mean(values);
  return Math.sqrt(mean(values.map(v => (v - m) * (v - m))));
}}
function rms(values) {{
  return values.length ? Math.sqrt(mean(values.map(v => v * v))) : 0;
}}
function movingAverage(values, window) {{
  const result = [];
  const queue = [];
  let running = 0;
  for (const value of values) {{
    queue.push(value);
    running += value;
    if (queue.length > window) running -= queue.shift();
    result.push(running / queue.length);
  }}
  return result;
}}
function centered(values, baselineSamples) {{
  const count = Math.max(1, Math.min(baselineSamples, values.length));
  const baseline = mean(values.slice(0, count));
  return values.map(value => value - baseline);
}}
function integrateVelocity(times, acc, correctDrift = true) {{
  if (!acc.length) return [];
  let velocity = [0];
  for (let i = 1; i < acc.length; i++) {{
    const dt = Math.max(0, times[i] - times[i - 1]);
    velocity.push(velocity[velocity.length - 1] + 0.5 * (acc[i] + acc[i - 1]) * dt);
  }}
  if (correctDrift && velocity.length > 1) {{
    const drift = velocity[velocity.length - 1] / (velocity.length - 1);
    velocity = velocity.map((value, index) => value - drift * index);
  }}
  return velocity;
}}
function jerkRms(times, smoothed) {{
  const jerks = [];
  for (let i = 1; i < smoothed.length; i++) {{
    const dt = times[i] - times[i - 1];
    if (dt > 1e-9) jerks.push((smoothed[i] - smoothed[i - 1]) / dt);
  }}
  return rms(jerks);
}}
function positivePeakValue(values) {{
  return values.length ? Math.max(0, Math.max(...values)) : 0;
}}
function positivePeakIndex(values) {{
  if (!values.length) return 0;
  let bestIndex = 0;
  let bestValue = values[0];
  for (let i = 1; i < values.length; i++) {{
    if (values[i] > bestValue) {{
      bestValue = values[i];
      bestIndex = i;
    }}
  }}
  return bestIndex;
}}
function strongestDriveAcceleration(acceleration, velocity) {{
  const driveValues = acceleration
    .map((acc, index) => (velocity[index] || 0) > 0 ? Math.abs(acc) : null)
    .filter(value => value !== null);
  if (driveValues.length) return Math.max(...driveValues);
  return acceleration.length ? Math.max(...acceleration.map(value => Math.abs(value))) : 0;
}}
function detectDriveStarts(times, velocity, tuning) {{
  if (velocity.length < 3) return {{ starts: [], threshold: 0 }};
  const driveVelocity = localSpeedCurve(velocity, tuning);
  const maxAbs = Math.max(...driveVelocity.map(value => Math.abs(value)));
  const threshold = Math.max(0.12 * std(driveVelocity), 0.04 * maxAbs, 1e-6);
  const minDistance = tuning.minPeakMs / 1000;
  const starts = [];
  let armed = true;
  let lastStartTime = -1e9;
  for (let i = 1; i < driveVelocity.length; i++) {{
    if (driveVelocity[i - 1] <= threshold && driveVelocity[i] > threshold) {{
      if (times[i] - lastStartTime >= minDistance) {{
        starts.push(i);
        lastStartTime = times[i];
      }}
    }}
  }}
  return {{ starts, threshold, driveVelocity }};
}}
function localSpeedCurve(velocity, tuning) {{
  if (!velocity.length) return [];
  const window = Math.max(15, tuning.smoothWindow * 3);
  const baseline = movingAverage(velocity, window);
  return velocity.map((value, index) => value - baseline[index]);
}}
function interp(values, position) {{
  if (!values.length) return 0;
  if (position <= 0) return values[0];
  if (position >= values.length - 1) return values[values.length - 1];
  const left = Math.floor(position);
  const right = Math.min(values.length - 1, left + 1);
  const ratio = position - left;
  return values[left] + (values[right] - values[left]) * ratio;
}}
function resample(values, count = 101) {{
  if (!values.length) return [];
  const maxIndex = values.length - 1;
  return Array.from({{ length: count }}, (_, i) => interp(values, maxIndex * i / (count - 1)));
}}
function avgSeries(series) {{
  if (!series.length) return [];
  const length = Math.min(...series.map(item => item.length));
  return Array.from({{ length }}, (_, i) => mean(series.map(item => item[i])));
}}
function getTuning() {{
  return {{
    baselineSamples: 200,
    smoothWindow: Math.max(1, Number(document.getElementById("smoothWindow").value || IMU_DATA.tuning.smoothWindow)),
    minPeakMs: Math.max(0, Number(document.getElementById("minPeakMs").value || IMU_DATA.tuning.minPeakMs)),
  }};
}}
function selectedForward() {{
  const axis = document.getElementById("axisSelect").value;
  const invert = document.getElementById("invertAxis").checked ? -1 : 1;
  const source = IMU_DATA.hasBoat ? axisValues("relativeSource") : IMU_DATA.seat;
  return source[axis].map(value => value * invert);
}}
function sourceModeLabel() {{
  const parts = [];
  parts.push(document.getElementById("swapSeatBoat").checked && IMU_DATA.hasBoat ? text("seatBoatSwapped", "SEAT/BOAT swapped") : (IMU_DATA.hasBoat ? text("seatBoatMode", "two-sensor mode") : text("seatOnlyMode", "SEAT only")));
  if (document.getElementById("invertAxis").checked) parts.push(text("globalSignInverted", "global sign inverted"));
  if (document.getElementById("invertSeatOnly").checked) parts.push(text("seatSignInverted", "SEAT sign inverted"));
  return parts.join(" · ");
}}
function axisValues(sourceName) {{
  const axis = document.getElementById("axisSelect").value;
  const invert = document.getElementById("invertAxis").checked ? -1 : 1;
  const seatInvert = document.getElementById("invertSeatOnly").checked ? -1 : 1;
  const swapped = document.getElementById("swapSeatBoat").checked && IMU_DATA.hasBoat;
  const seatSource = swapped ? IMU_DATA.boat : IMU_DATA.seat;
  const boatSource = swapped ? IMU_DATA.seat : IMU_DATA.boat;
  const seatValues = seatSource?.[axis] || IMU_DATA.times.map(() => 0);
  const boatValues = boatSource?.[axis] || IMU_DATA.times.map(() => 0);
  if (sourceName === "relativeSource") {{
    return {{
      [axis]: seatValues.map((value, index) => (value * seatInvert - boatValues[index])),
    }};
  }}
  if (sourceName === "relative") {{
    if (!IMU_DATA.hasBoat) return IMU_DATA.times.map(() => 0);
    return seatValues.map((value, index) => (value * seatInvert - boatValues[index]) * invert);
  }}
  if (sourceName === "seat") return seatValues.map(value => value * seatInvert * invert);
  if (sourceName === "boat") return boatValues.map(value => value * invert);
  return seatValues.map(value => value * seatInvert * invert);
}}
function processSource(times, rawValues, tuning) {{
  const centeredValues = centered(rawValues, tuning.baselineSamples);
  const smoothedValues = movingAverage(centeredValues, tuning.smoothWindow);
  const velocityValues = integrateVelocity(times, smoothedValues, true);
  return {{ centered: centeredValues, smoothed: smoothedValues, velocity: velocityValues }};
}}
function sliceRange(times, values, start, end) {{
  const indices = [];
  for (let i = 0; i < times.length; i++) if (times[i] >= start && times[i] <= end) indices.push(i);
  return {{
    times: indices.map(i => times[i]),
    values: indices.map(i => values[i]),
  }};
}}
function analyzeRange(start, end) {{
  const tuning = getTuning();
  const relativeSlice = sliceRange(IMU_DATA.times, axisValues("relative"), start, end);
  const seatSlice = sliceRange(IMU_DATA.times, axisValues("seat"), start, end);
  const boatSlice = sliceRange(IMU_DATA.times, axisValues("boat"), start, end);
  const localTimes = relativeSlice.times.map(value => value - relativeSlice.times[0]);
  const relativeProcessed = processSource(localTimes, relativeSlice.values, tuning);
  const seatProcessed = processSource(localTimes, seatSlice.values, tuning);
  const boatProcessed = processSource(localTimes, boatSlice.values, tuning);
  const smoothed = relativeProcessed.smoothed;
  let velocity = relativeProcessed.velocity;
  const detected = detectDriveStarts(localTimes, velocity, tuning);
  velocity = detected.driveVelocity;
  const power = smoothed.map((acc, i) => Math.max(0, acc * (velocity[i] || 0)));
  const driveStartTimes = detected.starts.map(index => localTimes[index]);
  const strokeSegments = [];
  const accStrokes = [];
  const velocityStrokes = [];
  const powerStrokes = [];
  for (let i = 0; i < detected.starts.length - 1; i++) {{
    const startIndex = detected.starts[i];
    const endIndex = detected.starts[i + 1];
    const accSeg = smoothed.slice(startIndex, endIndex + 1);
    const timeSeg = localTimes.slice(startIndex, endIndex + 1).map(value => value - localTimes[startIndex]);
    const velSeg = velocity.slice(startIndex, endIndex + 1);
    const powerSeg = accSeg.map((acc, j) => Math.max(0, acc * (velSeg[j] || 0)));
    const seatAccSeg = seatProcessed.smoothed.slice(startIndex, endIndex + 1);
    const boatAccSeg = boatProcessed.smoothed.slice(startIndex, endIndex + 1);
    const boatVelImpactSeg = boatProcessed.velocity.slice(startIndex, endIndex + 1);
    const seatVelSeg = integrateVelocity(timeSeg, seatAccSeg, true);
    const boatVelSeg = integrateVelocity(timeSeg, boatAccSeg, true);
    const boatVelocityChange = boatVelImpactSeg.length ? boatVelImpactSeg[boatVelImpactSeg.length - 1] - boatVelImpactSeg[0] : 0;
    accStrokes.push(resample(seatAccSeg));
    velocityStrokes.push(resample(velSeg));
    powerStrokes.push(resample(powerSeg));
    strokeSegments.push({{
      duration: localTimes[endIndex] - localTimes[startIndex],
      start: localTimes[startIndex],
      end: localTimes[endIndex],
      time: timeSeg,
      speed: positivePeakValue(velSeg),
      power: Math.max(...powerSeg),
      peakAcc: strongestDriveAcceleration(seatAccSeg, velSeg),
      smoothness: jerkRms(timeSeg, accSeg),
      peakPhase: velSeg.length ? positivePeakIndex(velSeg) / Math.max(1, velSeg.length - 1) * 100 : 0,
      phase: Array.from({{ length: 101 }}, (_, phase) => phase),
      acc: accStrokes[accStrokes.length - 1],
      velocity: velocityStrokes[velocityStrokes.length - 1],
      powerSeries: powerStrokes[powerStrokes.length - 1],
      rawAcc: seatAccSeg,
      rawVelocity: velSeg,
      rawSeatAcc: seatAccSeg,
      rawBoatAcc: boatAccSeg,
      rawSeatVelocity: seatVelSeg,
      rawBoatVelocity: boatVelSeg,
      seatAcc: resample(seatAccSeg),
      boatAcc: resample(boatAccSeg),
      seatVelocity: resample(seatVelSeg),
      boatVelocity: resample(boatVelSeg),
      boatVelocityGain: boatVelocityChange,
      boatVelocityChange,
      seatPeakAcc: seatAccSeg.length ? Math.max(...seatAccSeg) : 0,
      boatPeakAcc: boatAccSeg.length ? Math.max(...boatAccSeg) : 0,
    }});
  }}
  const duration = Math.max(0.001, localTimes[localTimes.length - 1] || 0);
  const durations = strokeSegments.map(s => s.duration);
  const rhythmCv = mean(durations) > 1e-9 ? std(durations) / mean(durations) * 100 : 0;
  const speedPeakIndex = positivePeakIndex(velocity);
  const avgStrongestSeatSpeedPhase = mean(strokeSegments.map(stroke => stroke.peakPhase));
  return {{
    start,
    end,
    duration,
    localTimes,
    smoothed,
    velocity,
    power,
    relativeAcc: relativeProcessed.smoothed,
    seatAcc: seatProcessed.smoothed,
    boatAcc: boatProcessed.smoothed,
    relativeVelocity: relativeProcessed.velocity,
    seatVelocity: seatProcessed.velocity,
    boatVelocity: boatProcessed.velocity,
    threshold: detected.threshold,
    peakTimes: driveStartTimes,
    strokeStartTimes: driveStartTimes.slice(0, -1),
    strokeEndTimes: driveStartTimes.slice(1),
    strokes: strokeSegments.length,
    spm: strokeSegments.length / duration * 60,
    rhythmCv,
    rhythmStd: std(durations),
    speed: positivePeakValue(velocity),
    speedPeakPhase: strokeSegments.length ? avgStrongestSeatSpeedPhase : (velocity.length ? localTimes[speedPeakIndex] / duration * 100 : 0),
    peakAcc: strongestDriveAcceleration(seatProcessed.smoothed, velocity),
    peakPower: power.length ? Math.max(...power) : 0,
    smoothness: jerkRms(localTimes, smoothed),
    avgStrokeTime: mean(durations),
    rhythmConsistency: Math.max(0, 100 - rhythmCv),
    strokeDetails: strokeSegments,
    avg: {{
      acc: avgSeries(accStrokes),
      velocity: avgSeries(velocityStrokes),
      power: avgSeries(powerStrokes),
      seatAcc: avgSeries(strokeSegments.map(stroke => stroke.seatAcc)),
      boatAcc: avgSeries(strokeSegments.map(stroke => stroke.boatAcc)),
      seatVelocity: avgSeries(strokeSegments.map(stroke => stroke.seatVelocity)),
      boatVelocity: avgSeries(strokeSegments.map(stroke => stroke.boatVelocity)),
    }},
  }};
}}
function fmtJs(value, digits = 2) {{
  return Number.isFinite(value) ? value.toFixed(digits) : "-";
}}
function selectedTimeMetrics(analysis, name) {{
  const normalize = document.getElementById("normalizeTimeGraph").checked;
  const colors = {{
    relativeAcc: "#1d4ed8",
    seatAcc: "#7c2d12",
    boatAcc: "#0f766e",
    relativeVelocity: "#4338ca",
    seatVelocity: "#c2410c",
    boatVelocity: "#059669",
  }};
  const labels = {{
    relativeAcc: text("relativeAcceleration", "SEAT-only acceleration"),
    seatAcc: text("seatAcceleration", "SEAT-only acceleration"),
    boatAcc: text("boatAcceleration", "BOAT-only acceleration"),
  }};
  return Array.from(document.querySelectorAll(".timeMetric:checked")).map(input => {{
    const key = input.value;
    let values = analysis[key];
    values = values || [];
    if (normalize && values.length) {{
      const scale = Math.max(...values.map(value => Math.abs(value))) || 1;
      values = values.map(value => value / scale);
    }}
    return {{
      label: `${{labels[key] || key}}${{normalize ? " (norm)" : ""}}`,
      values,
      color: colors[key] || "#1f2933",
      width: 2,
    }};
  }});
}}
function selectedStrokeMetrics(analysis, stroke) {{
  const showAverage = document.getElementById("showAverageStroke").checked;
  const choices = Array.from(document.querySelectorAll(".strokeMetric:checked")).map(input => input.value);
  const colors = {{
    acc: "#1d4ed8",
    velocity: "#4338ca",
    seatAcc: "#7c2d12",
    boatAcc: "#0f766e",
    seatVelocity: "#c2410c",
    boatVelocity: "#059669",
  }};
  const labels = {{
    acc: text("relativeAcceleration", "SEAT-only acceleration"),
    seatAcc: text("seatAcceleration", "SEAT-only acceleration"),
    boatAcc: text("boatAcceleration", "BOAT-only acceleration"),
    velocity: text("relativeVelocityProxy", "SEAT movement speed proxy"),
    seatVelocity: text("seatVelocityProxy", "SEAT velocity proxy"),
    boatVelocity: text("boatVelocityProxy", "BOAT velocity proxy"),
  }};
  const averageLabels = {{
    acc: currentLanguage === "pl" ? "średnie przyspieszenie SEAT" : "avg SEAT-only acceleration",
    seatAcc: currentLanguage === "pl" ? "średnie przyspieszenie SEAT" : "avg SEAT-only acceleration",
    boatAcc: currentLanguage === "pl" ? "średnie przyspieszenie BOAT" : "avg BOAT-only acceleration",
    velocity: currentLanguage === "pl" ? "średnie proxy prędkości SEAT" : "avg SEAT movement speed proxy",
    seatVelocity: currentLanguage === "pl" ? "średnie proxy prędkości SEAT" : "avg SEAT velocity proxy",
    boatVelocity: currentLanguage === "pl" ? "średnie proxy prędkości BOAT" : "avg BOAT velocity proxy",
  }};
  const series = [];
  for (const key of choices) {{
    series.push({{ label: labels[key], values: stroke[key === "power" ? "powerSeries" : key] || [], color: colors[key], width: 2.4 }});
    if (showAverage) {{
      series.push({{ label: averageLabels[key], values: analysis.avg[key] || [], color: colors[key], width: 1.4, dashed: true }});
    }}
  }}
  return series;
}}
function selectedOverlayMetricInfo() {{
  const key = document.getElementById("overlayMetric").value;
  const labels = {{
    acc: text("relativeAcceleration", "SEAT-only acceleration"),
    seatAcc: text("seatAcceleration", "SEAT-only acceleration"),
    boatAcc: text("boatAcceleration", "BOAT-only acceleration"),
    velocity: text("relativeVelocityProxy", "SEAT movement speed proxy"),
    seatVelocity: text("seatVelocityProxy", "SEAT velocity proxy"),
    boatVelocity: text("boatVelocityProxy", "BOAT velocity proxy"),
  }};
  return {{ key, label: labels[key] || key }};
}}
function overlayItems() {{
  const items = [];
  segments.forEach((segment, segmentIndex) => {{
    if (!segment.enabled || !segment.analysis) return;
    segment.analysis.strokeDetails.forEach((stroke, strokeIndex) => {{
      items.push({{
        id: `${{segmentIndex}}:${{strokeIndex}}`,
        segmentIndex,
        strokeIndex,
        athlete: segment.name || `${{text("athlete", "Athlete")}} ${{segmentIndex + 1}}`,
        stroke,
      }});
    }});
  }});
  return items;
}}
function updateStrokeOverlayControls() {{
  const container = document.getElementById("strokeOverlayControls");
  const items = overlayItems();
  const ids = new Set(items.map(item => item.id));
  selectedOverlayStrokes = new Set(
    Array.from(selectedOverlayStrokes).filter(id => ids.has(id))
  );
  if (!selectedOverlayStrokes.size) {{
    selectedOverlayStrokes = new Set(items.slice(0, Math.min(4, items.length)).map(item => item.id));
  }}
  container.innerHTML = items.map(item =>
    `<label><input class="overlayStroke" value="${{item.id}}" type="checkbox" ${{selectedOverlayStrokes.has(item.id) ? "checked" : ""}}> ${{text("strokes", "Stroke")}} ${{item.strokeIndex + 1}} - ${{htmlEscapeText(item.athlete)}}</label>`
  ).join("");
  document.querySelectorAll(".overlayStroke").forEach(input => {{
    input.addEventListener("change", event => {{
      const id = event.target.value;
      if (event.target.checked) selectedOverlayStrokes.add(id);
      else selectedOverlayStrokes.delete(id);
      renderStrokeOverlay();
    }});
  }});
}}
function renderStrokeOverlay() {{
  const metric = selectedOverlayMetricInfo();
  const items = overlayItems();
  const order = new Map(items.map((item, index) => [item.id, index]));
  const selected = Array.from(selectedOverlayStrokes)
    .sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0))
    .map(id => items.find(item => item.id === id))
    .filter(Boolean);
  const maxLength = Math.max(0, ...selected.map(item => item.stroke.time.length));
  const xValues = Array.from({{ length: maxLength }}, (_, pointIndex) =>
    Math.max(...selected.map(item => item.stroke.time[Math.min(pointIndex, item.stroke.time.length - 1)] || 0))
  );
  const rawKeys = {{
    acc: "rawAcc",
    seatAcc: "rawSeatAcc",
    boatAcc: "rawBoatAcc",
    velocity: "rawVelocity",
    seatVelocity: "rawSeatVelocity",
    boatVelocity: "rawBoatVelocity",
  }};
  const series = selected
    .map((item, seriesIndex) => {{
      const stroke = item.stroke;
      const rawKey = rawKeys[metric.key] || metric.key;
      return {{
        label: `${{text("strokes", "Stroke")}} ${{item.strokeIndex + 1}} - ${{item.athlete}}`,
        values: stroke[rawKey] || [],
        color: palette[seriesIndex % palette.length],
        width: 2,
      }};
    }})
  drawCanvas(
    document.getElementById("strokeOverlayCanvas"),
    xValues,
    series,
    `${{metric.label}} ${{text("byStrokeTime", "by stroke time (s)")}}`,
    {{ zoom: strokeOverlayZoomRange, phase: false }}
  );
}}
function formatTick(value, options = {{}}) {{
  if (options.phase) return `${{value.toFixed(0)}}%`;
  const digits = Number(document.getElementById("timePrecision")?.value ?? 1);
  return value.toFixed(digits);
}}
function sliceSeriesForZoom(xValues, series, zoom) {{
  if (!zoom) return {{ xValues, series }};
  const indices = [];
  for (let i = 0; i < xValues.length; i++) {{
    if (xValues[i] >= zoom.start && xValues[i] <= zoom.end) indices.push(i);
  }}
  return {{
    xValues: indices.map(i => xValues[i]),
    series: series.map(item => ({{
      ...item,
      values: indices.map(i => item.values[i]),
    }})),
  }};
}}
function drawCanvas(canvas, xValues, series, xLabel, options = {{}}) {{
  const zoomed = sliceSeriesForZoom(xValues, series, options.zoom || null);
  xValues = zoomed.xValues;
  series = zoomed.series;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
  const padL = 56, padR = 18, padT = 28, padB = 78;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  const ys = series.flatMap(s => s.values).filter(Number.isFinite);
  if (!xValues.length || !ys.length) {{
    ctx.fillStyle = "#667064";
    ctx.font = "14px system-ui";
    ctx.fillText(text("noData", "No data in this graph/zoom window"), 56, 60);
    return;
  }}
  let minX = Math.min(...xValues), maxX = Math.max(...xValues);
  let minY = Math.min(...ys), maxY = Math.max(...ys);
  if (minX === maxX) maxX = minX + 1;
  if (minY === maxY) {{ minY -= 1; maxY += 1; }}
  const padY = (maxY - minY) * 0.08;
  minY -= padY; maxY += padY;
  const sx = x => padL + (x - minX) / (maxX - minX) * plotW;
  const sy = y => padT + (maxY - y) / (maxY - minY) * plotH;
  canvas._scale = {{ minX, maxX, padL, plotW }};
  ctx.strokeStyle = "#e4e8dd";
  ctx.lineWidth = 1;
  const xTickStep = options.phase ? Number(document.getElementById("phaseTickStep")?.value || 10) : null;
  const xTicks = options.phase
    ? Array.from({{ length: Math.floor(100 / xTickStep) + 1 }}, (_, i) => i * xTickStep).filter(value => value >= minX && value <= maxX)
    : Array.from({{ length: 6 }}, (_, i) => minX + (maxX - minX) * i / 5);
  for (const value of xTicks) {{
    const x = sx(value);
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
    ctx.fillStyle = "#667064";
    ctx.font = "12px system-ui";
    ctx.textAlign = "center";
    ctx.fillText(formatTick(value, options), x, padT + plotH + 18);
  }}
  for (let i = 0; i <= 4; i++) {{
    const y = padT + plotH * i / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
    const yValue = maxY - (maxY - minY) * i / 4;
    ctx.fillStyle = "#667064";
    ctx.font = "12px system-ui";
    ctx.textAlign = "right";
    ctx.fillText(yValue.toFixed(2), padL - 8, y + 4);
  }}
  ctx.strokeStyle = "#475046";
  ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH); ctx.lineTo(padL + plotW, padT + plotH); ctx.stroke();
  if (options.events && options.events.length) {{
    ctx.save();
    ctx.strokeStyle = "#64748b";
    ctx.fillStyle = "#64748b";
    ctx.setLineDash([5, 5]);
    ctx.font = "11px system-ui";
    for (const event of options.events) {{
      if (event.time < minX || event.time > maxX) continue;
      const x = sx(event.time);
      ctx.strokeStyle = event.color || "#64748b";
      ctx.fillStyle = event.color || "#64748b";
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, padT + plotH);
      ctx.stroke();
      if (event.label) ctx.fillText(event.label, x + 4, padT + 12 + (event.offset || 0));
    }}
    ctx.restore();
  }}
  for (const s of series) {{
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 2;
    if (s.dashed) ctx.setLineDash([7, 5]); else ctx.setLineDash([]);
    ctx.beginPath();
    s.values.forEach((y, i) => {{
      const x = xValues[Math.min(i, xValues.length - 1)];
      if (i === 0) ctx.moveTo(sx(x), sy(y)); else ctx.lineTo(sx(x), sy(y));
    }});
    ctx.stroke();
    ctx.setLineDash([]);
  }}
  ctx.fillStyle = "#1f2933";
  ctx.font = "13px system-ui";
  ctx.textAlign = "left";
  let lx = padL;
  let ly = padT + plotH + 40;
  const maxLegendX = padL + plotW;
  for (const s of series) {{
    const label = s.label || "";
    const itemW = Math.min(240, Math.max(96, ctx.measureText(label).width + 28));
    if (lx + itemW > maxLegendX) {{
      lx = padL;
      ly += 18;
    }}
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, ly - 10, 10, 10);
    ctx.fillStyle = "#1f2933";
    ctx.fillText(label, lx + 15, ly);
    lx += itemW;
  }}
  ctx.fillStyle = "#667064";
  ctx.textAlign = "center";
  ctx.fillText(xLabel, padL + plotW / 2, h - 6);
}}
function render() {{
  const fullEnd = IMU_DATA.times[IMU_DATA.times.length - 1] || 0;
  if (!segments.length) segments.push({{ name: text("fullRecording", "Full recording"), start: 0, end: fullEnd, color: palette[0], enabled: true }});
  const rows = [];
  const cards = [];
  const full = analyzeRange(0, fullEnd);
  cards.push([text("axis", "Axis"), axisNames[document.getElementById("axisSelect").value], sourceModeLabel()]);
  cards.push([text("detectedStrokes", "Detected strokes"), full.strokes, text("inSelectedRecording", "In the selected recording")]);
  cards.push([text("strokeRate", "Stroke rate"), `${{fmtJs(full.spm, 1)}} strokes/min`, text("averagePace", "Average pace")]);
  cards.push([text("rhythmConsistency", "Rhythm consistency"), `${{fmtJs(full.rhythmConsistency, 0)}} / 100`, text("higherRegularTiming", "Higher means more regular stroke timing")]);
  cards.push([text("smoothness", "Smoothness"), fmtJs(full.smoothness, 2), text("lowerLessJerky", "Lower means less jerky seat movement")]);
  document.getElementById("interactiveMetrics").innerHTML = cards.map(card =>
    `<div class="metric"><span>${{card[0]}}</span><strong>${{card[1]}}</strong><small>${{card[2]}}</small></div>`
  ).join("");
  segments.forEach((segment, index) => {{
    const analysis = analyzeRange(segment.start, segment.end);
    segment.analysis = analysis;
    rows.push(`<tr>
      <td><input type="checkbox" data-action="toggle" data-index="${{index}}" ${{segment.enabled ? "checked" : ""}}></td>
      <td><input data-action="name" data-index="${{index}}" value="${{segment.name}}"></td>
      <td><input data-action="start" data-index="${{index}}" type="number" step="0.1" value="${{segment.start}}"> - <input data-action="end" data-index="${{index}}" type="number" step="0.1" value="${{segment.end}}"></td>
      <td>${{analysis.strokes}}</td>
      <td>${{fmtJs(analysis.spm, 1)}}</td>
      <td>${{fmtJs(analysis.rhythmConsistency, 0)}} / 100</td>
      <td>${{fmtJs(analysis.speedPeakPhase, 1)}}%</td>
      <td>${{fmtJs(analysis.smoothness, 3)}}</td>
      <td><textarea data-action="comment" data-index="${{index}}" rows="2" placeholder="${{htmlEscapeText(text("shortNoteForAthlete", "Short note for athlete"))}}">${{htmlEscapeText(segment.comment || "")}}</textarea></td>
      <td><button type="button" data-action="download" data-index="${{index}}">HTML</button> <button type="button" data-action="remove" data-index="${{index}}">${{text("remove", "Remove")}}</button></td>
    </tr>`);
  }});
  document.querySelector("#segmentTable tbody").innerHTML = rows.join("");
  const firstEnabled = segments.find(s => s.enabled) || segments[0];
  const firstAnalysis = firstEnabled ? firstEnabled.analysis : full;
  const events = document.getElementById("showStrokeEvents").checked
    ? [
        ...firstAnalysis.strokeStartTimes.map((time, index) => ({{ time, label: `S${{index + 1}} ${{text("start", "start")}}`, color: "#1d4ed8", offset: 0 }})),
        ...firstAnalysis.strokeEndTimes.map((time, index) => ({{ time, label: `S${{index + 1}} ${{text("end", "end")}}`, color: "#b91c1c", offset: 14 }})),
      ]
    : [];
  drawCanvas(
    document.getElementById("timeCanvas"),
    firstAnalysis.localTimes,
    selectedTimeMetrics(firstAnalysis, firstEnabled.name),
    text("timeInSelectedSegment", "time in selected segment (s)"),
    {{ events, zoom: zoomRange, phase: false }}
  );
  updateStrokeView(firstAnalysis);
}}
function updateStrokeView(analysis) {{
  const select = document.getElementById("strokeSelect");
  const previous = select.value;
  select.innerHTML = analysis.strokeDetails.map((stroke, index) =>
    `<option value="${{index}}">${{text("strokes", "Stroke")}} ${{index + 1}} (${{fmtJs(stroke.duration, 2)}} s)</option>`
  ).join("");
  if (previous && Number(previous) < analysis.strokeDetails.length) select.value = previous;
  selectedStrokeIndex = Math.min(Number(select.value || 0), Math.max(0, analysis.strokeDetails.length - 1));
  const stroke = analysis.strokeDetails[selectedStrokeIndex];
  const metrics = document.getElementById("strokeMetrics");
  const canvas = document.getElementById("strokeCanvas");
  if (!stroke) {{
    metrics.innerHTML = `<p class='note'>${{text("noCompleteStroke", "No complete stroke detected for this segment.")}}</p>`;
    document.querySelector("#allStrokeTable tbody").innerHTML = `<tr><td colspan='9'>${{text("noCompleteStroke", "No complete stroke detected for this segment.")}}</td></tr>`;
    drawCanvas(canvas, [], [], text("strokePhase", "stroke phase (%)"));
    drawCanvas(document.getElementById("strokeVelocityCanvas"), [], [], text("strokePhase", "stroke phase (%)"));
    document.getElementById("strokeOverlayControls").innerHTML = "";
    drawCanvas(document.getElementById("strokeOverlayCanvas"), [], [], text("strokeTimeAxis", "stroke time (s)"));
    return;
  }}
  updateStrokeOverlayControls();
  const tableItems = overlayItems();
  document.querySelector("#allStrokeTable tbody").innerHTML = tableItems.map(itemInfo => {{
    const item = itemInfo.stroke;
    return `<tr>
    <td>${{itemInfo.strokeIndex + 1}}</td>
    <td>${{htmlEscapeText(itemInfo.athlete)}}</td>
    <td>${{fmtJs(item.start, 2)}}-${{fmtJs(item.end, 2)}}s</td>
    <td>${{fmtJs(item.duration, 2)}}s</td>
    <td>${{fmtJs(60 / item.duration, 1)}} SPM</td>
    <td>${{fmtJs(item.peakPhase, 1)}}%</td>
    <td>${{fmtJs(item.peakAcc, 3)}} m/s^2</td>
    <td>${{fmtJs(item.boatVelocityGain, 3)}}</td>
    <td>${{fmtJs(item.smoothness, 3)}}</td>
  </tr>`;
  }}).join("");
  metrics.innerHTML = [
    [text("selectedStroke", "Selected stroke"), `${{selectedStrokeIndex + 1}}`, `${{fmtJs(stroke.start, 2)}}s to ${{fmtJs(stroke.end, 2)}}s`],
    [text("strokeTime", "Stroke time"), `${{fmtJs(stroke.duration, 2)}} s`, text("timeDriveChange", "Time from one drive-direction change to the next")],
    [text("strokeRate", "Stroke rate"), `${{fmtJs(60 / stroke.duration, 1)}} SPM`, text("paceForStroke", "Pace for this stroke")],
    [text("strongestSeatDriveSpeed", "Strongest seat drive speed"), `${{fmtJs(stroke.peakPhase, 1)}}%`, text("whereSeatFastest", "Where the seat moves fastest in this stroke")],
    [text("peakAcceleration", "Strongest SEAT drive acceleration"), `${{fmtJs(stroke.peakAcc, 3)}} m/s^2`, text("strongestSeatAcceleration", "Largest SEAT acceleration magnitude while the seat is moving in drive direction")],
    [text("boatVelocityChange", "BOAT velocity change"), fmtJs(stroke.boatVelocityGain, 3), text("boatVelocityChangeNote", "Signed BOAT velocity proxy change from stroke start to stroke end")],
    [text("smoothness", "Smoothness"), fmtJs(stroke.smoothness, 3), text("lowerSmoother", "Lower means a smoother seat movement")],
  ].map(card => `<div class="metric"><span>${{card[0]}}</span><strong>${{card[1]}}</strong><small>${{card[2]}}</small></div>`).join("");
  drawCanvas(
    canvas,
    stroke.phase,
    selectedStrokeMetrics(analysis, stroke),
    text("strokePhase", "stroke phase (%)"),
    {{
      zoom: strokeZoomRange,
      phase: true,
      events: [
        {{ time: stroke.peakPhase, label: text("strongestSpeed", "strongest speed"), color: "#6d28d9", offset: 0 }},
      ],
    }}
  );
  drawCanvas(
    document.getElementById("strokeVelocityCanvas"),
    stroke.phase,
    [
      {{ label: text("selectedStrokeSpeedCurve", "selected stroke speed curve"), values: stroke.velocity, color: "#6d28d9", width: 2.4 }},
      {{ label: text("avgSpeedCurve", "avg speed curve"), values: analysis.avg.velocity, color: "#6d28d9", width: 1.4, dashed: true }},
    ],
    text("strokePhase", "stroke phase (%)"),
    {{
      zoom: strokeVelocityZoomRange,
      phase: true,
      events: [
        {{ time: stroke.peakPhase, label: text("strongestSpeed", "strongest speed"), color: "#6d28d9", offset: 0 }},
      ],
    }}
  );
  renderStrokeOverlay();
}}
function htmlEscapeText(value) {{
  return String(value).replace(/[&<>"']/g, char => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }}[char]));
}}
function segmentReportHtml(segment) {{
  const analysis = segment.analysis || analyzeRange(segment.start, segment.end);
  const data = JSON.stringify({{
    name: segment.name,
    comment: segment.comment || "",
    sourceMode: sourceModeLabel(),
    sourceStart: segment.start,
    sourceEnd: segment.end,
    time: analysis.localTimes,
    acc: analysis.seatAcc,
    seatAcc: analysis.seatAcc,
    boatAcc: analysis.boatAcc,
    velocity: analysis.velocity,
    seatVelocity: analysis.seatVelocity,
    boatVelocity: analysis.boatVelocity,
    power: analysis.power,
    threshold: analysis.threshold,
    starts: analysis.strokeStartTimes,
    ends: analysis.strokeEndTimes,
    phase: Array.from({{ length: 101 }}, (_, i) => i),
    avgAcc: analysis.avg.acc,
    avgSeatAcc: analysis.avg.seatAcc,
    avgBoatAcc: analysis.avg.boatAcc,
    avgVelocity: analysis.avg.velocity,
    avgSeatVelocity: analysis.avg.seatVelocity,
    avgBoatVelocity: analysis.avg.boatVelocity,
    avgPower: analysis.avg.power,
    strokes: analysis.strokeDetails,
    metrics: {{
      strokes: analysis.strokes,
      spm: analysis.spm,
      rhythmConsistency: analysis.rhythmConsistency,
      strongestSeatDriveSpeed: analysis.speedPeakPhase,
      peakAcc: analysis.peakAcc,
      smoothness: analysis.smoothness,
    }},
  }});
  return `<!doctype html>
<html><head><meta charset="utf-8"><title>${{htmlEscapeText(segment.name)}} rowing report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f6f7f2; color: #1f2933; }}
header {{ padding: 28px 36px; background: #20251f; color: white; }}
main {{ padding: 26px 36px 44px; max-width: 1180px; margin: 0 auto; }}
h1, h2, h3 {{ margin: 0 0 12px; }}
h2 {{ margin-top: 28px; border-bottom: 1px solid #d8ddd0; padding-bottom: 8px; }}
table {{ border-collapse: collapse; width: 100%; background: white; margin: 16px 0; }}
td, th {{ border-bottom: 1px solid #dfe4d7; padding: 8px; text-align: left; }}
canvas {{ width: 100%; height: auto; background: white; border: 1px solid #dfe4d7; border-radius: 8px; margin: 12px 0 20px; }}
.note {{ color: #556052; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0; }}
.metric {{ background: white; border: 1px solid #dfe4d7; border-radius: 8px; padding: 12px; }}
.metric span {{ display: block; color: #667064; font-size: 13px; }}
.metric strong {{ display: block; font-size: 22px; margin: 4px 0; color: #17201a; }}
.metric small {{ display: block; color: #667064; min-height: 28px; }}
textarea {{ width: 100%; min-height: 88px; border: 1px solid #cbd5c0; border-radius: 8px; padding: 10px; font: inherit; background: white; }}
select, button {{ font: inherit; border: 1px solid #cbd5c0; border-radius: 6px; padding: 7px 8px; background: white; }}
.controls {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; background: white; border: 1px solid #dfe4d7; border-radius: 8px; padding: 12px; margin: 12px 0; }}
.controls label {{ color: #556052; font-size: 13px; }}
#strokeOverlayControls {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
#strokeOverlayControls label {{ display: inline-flex; flex-direction: row; align-items: center; gap: 5px; min-width: 86px; padding: 5px 8px; border: 1px solid #dfe4d7; border-radius: 6px; background: #f8faf6; }}
</style></head><body>
<header><h1>${{htmlEscapeText(segment.name)}} ${{text("coachDashboard", "dashboard")}}</h1><p>${{htmlEscapeText(currentLanguage === "pl" ? "Ten eksport zawiera tylko segment tego zawodnika, pokazany od 0 s do " : "This export contains only this athlete's segment, shown from 0s to ")}}${{fmtJs(analysis.duration, 1)}}s · ${{htmlEscapeText(sourceModeLabel())}}</p></header>
<main>
<h2>${{text("athleteDashboard", "Athlete dashboard")}}</h2>
<p class="note">${{htmlEscapeText(currentLanguage === "pl" ? "Ten plik używa tych samych widoków panelu co raport trenera, ale wszystkie wartości czasu odnoszą się do wybranego segmentu zawodnika." : "This exported file uses the same dashboard views as the coach report, but all time values are local to this athlete's selected segment.")}}</p>
<div id="metrics" class="grid"></div>
<h2>${{text("coachMessageBeforeExport", "Coach comment")}}</h2>
<textarea id="coachComment" placeholder="${{htmlEscapeText(text("shortNoteForAthlete", "Write a short note for the athlete..."))}}"></textarea>
<h2>${{text("timeGraph", "Time graph")}}</h2>
<div class="controls">
  <label><input class="timeMetric" value="relativeAcc" type="checkbox"> ${{text("relativeAcceleration", "SEAT-only acceleration")}}</label>
  <label><input class="timeMetric" value="seatAcc" type="checkbox" checked> ${{text("seatAcceleration", "SEAT-only acceleration")}}</label>
  <label><input class="timeMetric" value="boatAcc" type="checkbox" checked> ${{text("boatAcceleration", "BOAT-only acceleration")}}</label>
  <label><input id="normalizeTimeGraph" type="checkbox" checked> ${{text("normalizeGraphLines", "normalize graph lines")}}</label>
  <label><input id="showStrokeEvents" type="checkbox" checked> ${{text("strokeStartEndLines", "stroke start/end lines")}}</label>
  <label>${{text("timeLabelDetail", "Time label detail")}}
    <select id="timePrecision">
      <option value="1">simple</option>
      <option value="2">more exact</option>
      <option value="0">rough</option>
    </select>
  </label>
  <label>${{text("zoomStart", "Zoom start s")}} <input id="zoomStart" type="number" step="0.1"></label>
  <label>${{text("zoomEnd", "Zoom end s")}} <input id="zoomEnd" type="number" step="0.1"></label>
  <button id="applyZoom" type="button">${{text("applyZoom", "Apply zoom")}}</button>
  <button id="resetZoom" type="button">${{text("resetZoom", "Reset zoom")}}</button>
</div>
<canvas id="time" width="1100" height="520"></canvas>
<h2>${{text("selectedStroke", "Selected stroke")}}</h2>
<div class="controls">
  <label>${{text("strokes", "Stroke")}} <select id="strokeSelect"></select></label>
  <label><input class="strokeMetric" value="acc" type="checkbox" checked> ${{text("relativeAcceleration", "SEAT-only acceleration")}}</label>
  <label><input class="strokeMetric" value="seatAcc" type="checkbox"> ${{text("seatAcceleration", "SEAT-only acceleration")}}</label>
  <label><input class="strokeMetric" value="boatAcc" type="checkbox"> ${{text("boatAcceleration", "BOAT-only acceleration")}}</label>
  <label><input class="strokeMetric" value="velocity" type="checkbox"> ${{text("relativeVelocityProxy", "SEAT movement speed proxy")}}</label>
  <label><input class="strokeMetric" value="seatVelocity" type="checkbox"> ${{text("seatVelocityProxy", "SEAT velocity proxy")}}</label>
  <label><input class="strokeMetric" value="boatVelocity" type="checkbox"> ${{text("boatVelocityProxy", "BOAT velocity proxy")}}</label>
  <label><input id="showAverageStroke" type="checkbox" checked> ${{text("showSegmentAverage", "show average")}}</label>
</div>
<div id="strokeMetrics" class="grid"></div>
<canvas id="stroke" width="1100" height="360"></canvas>
<canvas id="strokeVelocity" width="1100" height="300"></canvas>
<h2>${{text("strokeOverlay", "Stroke overlay")}}</h2>
<div class="controls">
  <label>${{text("overlayMetric", "Overlay metric")}}
    <select id="overlayMetric">
      <option value="velocity">${{text("relativeVelocityProxy", "SEAT movement speed proxy")}}</option>
      <option value="boatVelocity">${{text("boatVelocityProxy", "BOAT velocity proxy")}}</option>
      <option value="seatVelocity">${{text("seatVelocityProxy", "SEAT velocity proxy")}}</option>
      <option value="acc">${{text("relativeAcceleration", "SEAT-only acceleration")}}</option>
      <option value="seatAcc">${{text("seatAcceleration", "SEAT-only acceleration")}}</option>
      <option value="boatAcc">${{text("boatAcceleration", "BOAT-only acceleration")}}</option>
    </select>
  </label>
  <span id="strokeOverlayControls"></span>
</div>
<canvas id="strokeOverlay" width="1100" height="400"></canvas>
<p class="note">${{text("strokeOverlayNote", "Selected strokes start at 0s and keep their real duration, so timing differences stay visible.")}}</p>
<h2>${{text("strokeByStrokeValues", "Stroke-by-stroke values")}}</h2>
<table id="strokeTable"><thead><tr><th>#</th><th>${{text("time", "Time")}}</th><th>${{text("strokeTime", "Stroke time")}}</th><th>${{text("strokeRate", "Stroke rate")}}</th><th>${{text("strongestSeatDriveSpeed", "Strongest seat drive speed")}}</th><th>${{text("peakAcceleration", "Strongest SEAT drive acceleration")}}</th><th>${{text("boatVelocityChange", "BOAT velocity change")}}</th><th>${{text("smoothness", "Smoothness")}}</th></tr></thead><tbody></tbody></table>
</main>
<script>
const DATA = ${{data}};
const palette = ['#1d4ed8', '#b91c1c', '#0f766e', '#7c2d12', '#6d28d9', '#be123c', '#0369a1', '#4d7c0f'];
let zoomRange = null;
let strokeZoomRange = null;
let strokeVelocityZoomRange = null;
let strokeOverlayZoomRange = null;
let selectedOverlayStrokes = new Set([0, 1, 2]);
function fmt(value, digits=2) {{ return Number.isFinite(value) ? value.toFixed(digits) : "-"; }}
function esc(value) {{ return String(value).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function formatTick(value, phase) {{
 if (phase) return value.toFixed(0) + '%';
 const digits = Number(document.getElementById('timePrecision')?.value || 1);
 return value.toFixed(digits);
}}
function sliceForZoom(x, series, zoom) {{
 if (!zoom) return {{x:x, series:series}};
 const indices = [];
 for (let i = 0; i < x.length; i++) if (x[i] >= zoom.start && x[i] <= zoom.end) indices.push(i);
 return {{
  x: indices.map(i => x[i]),
  series: series.map(item => ({{...item, v: indices.map(i => item.v[i])}})),
 }};
}}
function draw(canvas, x, series, events, zoom, phase) {{
 const sliced = sliceForZoom(x, series, zoom);
 x = sliced.x;
 series = sliced.series;
 const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
 ctx.clearRect(0,0,w,h); ctx.fillStyle='white'; ctx.fillRect(0,0,w,h);
 const p={{l:56,r:18,t:28,b:78}}, pw=w-p.l-p.r, ph=h-p.t-p.b;
 const ys=series.flatMap(s=>s.v).filter(Number.isFinite); if(!x.length||!ys.length) {{ ctx.fillStyle='#667064'; ctx.font='14px system-ui'; ctx.fillText('No data in this graph/zoom window', 56, 60); return; }}
 let minX=Math.min(...x), maxX=Math.max(...x), minY=Math.min(...ys), maxY=Math.max(...ys);
 if(minX===maxX) maxX=minX+1; if(minY===maxY){{minY-=1;maxY+=1;}}
 const py=(maxY-minY)*0.08; minY-=py; maxY+=py;
 const sx=v=>p.l+(v-minX)/(maxX-minX)*pw, sy=v=>p.t+(maxY-v)/(maxY-minY)*ph;
 canvas._scale = {{minX:minX, maxX:maxX, padL:p.l, plotW:pw}};
 ctx.strokeStyle='#e4e8dd';
 const tickCount = phase ? 10 : 5;
 for(let i=0;i<=tickCount;i++){{const value=minX+(maxX-minX)*i/tickCount;const xx=sx(value);ctx.beginPath();ctx.moveTo(xx,p.t);ctx.lineTo(xx,p.t+ph);ctx.stroke();ctx.fillStyle='#667064';ctx.font='12px system-ui';ctx.textAlign='center';ctx.fillText(formatTick(value, phase), xx, p.t+ph+18);}}
 for(let i=0;i<=4;i++){{const yy=p.t+ph*i/4;ctx.beginPath();ctx.moveTo(p.l,yy);ctx.lineTo(p.l+pw,yy);ctx.stroke();const yv=maxY-(maxY-minY)*i/4;ctx.fillStyle='#667064';ctx.font='12px system-ui';ctx.textAlign='right';ctx.fillText(yv.toFixed(2), p.l-8, yy+4);}}
 if(events){{ctx.save();ctx.setLineDash([5,5]);for(const e of events){{const time = typeof e === 'number' ? e : e.time; if(time<minX||time>maxX)continue;const xx=sx(time);ctx.strokeStyle=e.color||'#64748b';ctx.fillStyle=e.color||'#64748b';ctx.beginPath();ctx.moveTo(xx,p.t);ctx.lineTo(xx,p.t+ph);ctx.stroke();if(e.label)ctx.fillText(e.label,xx+4,p.t+12+(e.offset||0));}}ctx.restore();}}
 for(const s of series){{ctx.strokeStyle=s.c;ctx.lineWidth=2;ctx.beginPath();s.v.forEach((y,i)=>{{const xx=sx(x[Math.min(i,x.length-1)]); if(i===0)ctx.moveTo(xx,sy(y)); else ctx.lineTo(xx,sy(y));}});ctx.stroke();}}
 ctx.fillStyle='#1f2933'; ctx.font='13px system-ui'; ctx.textAlign='left'; let lx=p.l; let ly=p.t+ph+40; const maxLegendX=p.l+pw; for(const s of series){{const label=s.n||''; const itemW=Math.min(240,Math.max(96,ctx.measureText(label).width+28)); if(lx+itemW>maxLegendX){{lx=p.l;ly+=18;}} ctx.fillStyle=s.c;ctx.fillRect(lx,ly-10,10,10);ctx.fillStyle='#1f2933';ctx.fillText(label,lx+15,ly);lx+=itemW;}}
}}
function renderMetrics() {{
 const cards = [
  ["${{text("strokes", "Strokes")}}", DATA.metrics.strokes, "${{text("inSelectedRecording", "In this athlete segment")}}"],
  ["${{text("strokeRate", "Stroke rate")}}", fmt(DATA.metrics.spm, 1) + " strokes/min", "${{text("averagePace", "Average pace")}}"],
  ["${{text("rhythmConsistency", "Rhythm consistency")}}", fmt(DATA.metrics.rhythmConsistency, 0) + " / 100", "${{text("higherRegularTiming", "Higher means more regular timing")}}"],
  ["${{text("strongestSeatDriveSpeed", "Strongest seat drive speed")}}", fmt(DATA.metrics.strongestSeatDriveSpeed, 1) + "%", "${{text("whereSeatFastest", "Where the seat is fastest")}}"],
  ["${{text("peakAcceleration", "Strongest SEAT drive acceleration")}}", fmt(DATA.metrics.peakAcc, 3) + " m/s^2", "${{text("strongestSeatAcceleration", "Largest SEAT acceleration magnitude while the seat is moving in drive direction")}}"],
  ["${{text("smoothness", "Smoothness")}}", fmt(DATA.metrics.smoothness, 3), "${{text("lowerSmoother", "Lower means smoother seat movement")}}"],
 ];
 document.getElementById('metrics').innerHTML = cards.map(card => '<div class="metric"><span>' + esc(card[0]) + '</span><strong>' + esc(card[1]) + '</strong><small>' + esc(card[2]) + '</small></div>').join('');
}}
function renderStrokeTable() {{
 document.querySelector('#strokeTable tbody').innerHTML = DATA.strokes.map((s, i) => '<tr><td>' + (i + 1) + '</td><td>' + fmt(s.start,2) + '-' + fmt(s.end,2) + 's</td><td>' + fmt(s.duration,2) + 's</td><td>' + fmt(60/s.duration,1) + ' SPM</td><td>' + fmt(s.peakPhase,1) + '%</td><td>' + fmt(s.peakAcc,3) + ' m/s^2</td><td>' + fmt(s.boatVelocityGain,3) + '</td><td>' + fmt(s.smoothness,3) + '</td></tr>').join('');
}}
function normalize(values) {{
 const scale = Math.max(...values.map(value => Math.abs(value))) || 1;
 return values.map(value => value / scale);
}}
function selectedTimeSeries() {{
 const normalizeLines = document.getElementById('normalizeTimeGraph').checked;
 const map = {{
 relativeAcc: {{n:'SEAT-only acceleration', v:DATA.acc, c:'#1d4ed8'}},
  seatAcc: {{n:'${{text("seatAcceleration", "SEAT-only acceleration")}}', v:DATA.seatAcc, c:'#7c2d12'}},
  boatAcc: {{n:'${{text("boatAcceleration", "BOAT-only acceleration")}}', v:DATA.boatAcc, c:'#0f766e'}},
 }};
 return Array.from(document.querySelectorAll('.timeMetric:checked')).map(input => {{
  const item = map[input.value];
  const values = normalizeLines ? normalize(item.v) : item.v;
  return {{n:item.n + (normalizeLines ? ' (norm)' : ''), v:values, c:item.c}};
 }});
}}
function selectedStrokeSeries(stroke) {{
 const showAverage = document.getElementById('showAverageStroke').checked;
 const map = {{
  acc: {{n:'${{text("relativeAcceleration", "selected SEAT-only acceleration")}}', avg:'${{currentLanguage === "pl" ? "średnie przyspieszenie SEAT" : "average SEAT-only acceleration"}}', v:stroke.acc, av:DATA.avgAcc, c:'#1d4ed8'}},
  seatAcc: {{n:'${{text("seatAcceleration", "selected SEAT-only acceleration")}}', avg:'${{currentLanguage === "pl" ? "średnie przyspieszenie SEAT" : "average SEAT-only acceleration"}}', v:stroke.seatAcc, av:DATA.avgSeatAcc, c:'#7c2d12'}},
  boatAcc: {{n:'${{text("boatAcceleration", "selected BOAT-only acceleration")}}', avg:'${{currentLanguage === "pl" ? "średnie przyspieszenie BOAT" : "average BOAT-only acceleration"}}', v:stroke.boatAcc, av:DATA.avgBoatAcc, c:'#0f766e'}},
  velocity: {{n:'${{text("relativeVelocityProxy", "selected SEAT movement speed proxy")}}', avg:'${{currentLanguage === "pl" ? "średnie proxy prędkości SEAT" : "average SEAT movement speed proxy"}}', v:stroke.velocity, av:DATA.avgVelocity, c:'#6d28d9'}},
  seatVelocity: {{n:'${{text("seatVelocityProxy", "selected SEAT velocity proxy")}}', avg:'${{currentLanguage === "pl" ? "średnie proxy prędkości SEAT" : "average SEAT velocity proxy"}}', v:stroke.seatVelocity, av:DATA.avgSeatVelocity, c:'#c2410c'}},
  boatVelocity: {{n:'${{text("boatVelocityProxy", "selected BOAT velocity proxy")}}', avg:'${{currentLanguage === "pl" ? "średnie proxy prędkości BOAT" : "average BOAT velocity proxy"}}', v:stroke.boatVelocity, av:DATA.avgBoatVelocity, c:'#059669'}},
 }};
 const series = [];
 for (const input of Array.from(document.querySelectorAll('.strokeMetric:checked'))) {{
  const item = map[input.value];
  series.push({{n:item.n, v:item.v, c:item.c}});
  if (showAverage) series.push({{n:item.avg, v:item.av, c:item.c}});
 }}
 return series;
}}
function selectedOverlayMetricInfo() {{
 const key = document.getElementById('overlayMetric').value;
 const labels = {{
  acc:'SEAT-only acceleration',
  seatAcc:'SEAT-only acceleration',
  boatAcc:'BOAT-only acceleration',
  velocity:'SEAT movement speed proxy',
  seatVelocity:'SEAT velocity proxy',
  boatVelocity:'BOAT velocity proxy',
 }};
 return {{key:key, label:labels[key] || key}};
}}
function updateOverlayControls() {{
 const available = DATA.strokes.length;
 selectedOverlayStrokes = new Set(Array.from(selectedOverlayStrokes).filter(index => index < available));
 if (!selectedOverlayStrokes.size) selectedOverlayStrokes = new Set(Array.from({{length: Math.min(3, available)}}, (_, index) => index));
 document.getElementById('strokeOverlayControls').innerHTML = DATA.strokes.map((stroke, index) =>
  '<label><input class="overlayStroke" value="' + index + '" type="checkbox" ' + (selectedOverlayStrokes.has(index) ? 'checked' : '') + '> Stroke ' + (index + 1) + '</label>'
 ).join('');
 document.querySelectorAll('.overlayStroke').forEach(input => input.addEventListener('change', event => {{
  const index = Number(event.target.value);
  if (event.target.checked) selectedOverlayStrokes.add(index);
  else selectedOverlayStrokes.delete(index);
  renderOverlay();
 }}));
}}
function renderOverlay() {{
 const metric = selectedOverlayMetricInfo();
 const selected = Array.from(selectedOverlayStrokes).sort((a,b) => a-b).map(index => DATA.strokes[index] ? {{index:index, stroke:DATA.strokes[index]}} : null).filter(Boolean);
 const maxLength = Math.max(0, ...selected.map(item => item.stroke.time.length));
 const xValues = Array.from({{length:maxLength}}, (_, pointIndex) =>
  Math.max(...selected.map(item => item.stroke.time[Math.min(pointIndex, item.stroke.time.length - 1)] || 0))
 );
 const rawKeys = {{
  acc:'rawAcc',
  seatAcc:'rawSeatAcc',
  boatAcc:'rawBoatAcc',
  velocity:'rawVelocity',
  seatVelocity:'rawSeatVelocity',
  boatVelocity:'rawBoatVelocity',
 }};
 const series = selected.map((item, seriesIndex) => {{
  const rawKey = rawKeys[metric.key] || metric.key;
  return {{n:'Stroke ' + (item.index + 1), v:item.stroke[rawKey] || [], c:palette[seriesIndex % palette.length]}};
 }});
 draw(document.getElementById('strokeOverlay'), xValues, series, null, strokeOverlayZoomRange, false);
}}
function renderStroke() {{
 const select = document.getElementById('strokeSelect');
 if (!select.options.length) select.innerHTML = DATA.strokes.map((s, i) => '<option value="' + i + '">Stroke ' + (i + 1) + ' (' + fmt(s.duration,2) + 's)</option>').join('');
 const stroke = DATA.strokes[Number(select.value || 0)];
 if (!stroke) return;
 document.getElementById('strokeMetrics').innerHTML = [
  ["${{text("strokeTime", "Stroke time")}}", fmt(stroke.duration,2) + " s", "${{text("timeDriveChange", "Time from one drive-direction change to the next")}}"],
  ["${{text("strokeRate", "Stroke rate")}}", fmt(60/stroke.duration,1) + " SPM", "${{text("paceForStroke", "Pace for this stroke")}}"],
  ["${{text("strongestSeatDriveSpeed", "Strongest seat drive speed")}}", fmt(stroke.peakPhase,1) + "%", "${{text("whereSeatFastest", "Where the seat moves fastest")}}"],
  ["${{text("peakAcceleration", "Strongest SEAT drive acceleration")}}", fmt(stroke.peakAcc,3) + " m/s^2", "${{text("strongestSeatAcceleration", "Largest SEAT acceleration magnitude while the seat is moving in drive direction")}}"],
  ["${{text("boatVelocityChange", "BOAT velocity change")}}", fmt(stroke.boatVelocityGain,3), "${{text("boatVelocityChangeNote", "Signed BOAT velocity proxy change from stroke start to stroke end")}}"],
  ["${{text("smoothness", "Smoothness")}}", fmt(stroke.smoothness,3), "${{text("lowerSmoother", "Lower means smoother seat movement")}}"],
 ].map(card => '<div class="metric"><span>' + esc(card[0]) + '</span><strong>' + esc(card[1]) + '</strong><small>' + esc(card[2]) + '</small></div>').join('');
 draw(document.getElementById('stroke'), stroke.phase, selectedStrokeSeries(stroke), [{{time:stroke.peakPhase,color:'#6d28d9',label:'strongest speed'}}], strokeZoomRange, true);
 draw(document.getElementById('strokeVelocity'), stroke.phase, [
  {{n:'${{text("relativeVelocityProxy", "selected SEAT movement speed proxy")}}', v:stroke.velocity, c:'#6d28d9'}},
  {{n:'${{currentLanguage === "pl" ? "średnie proxy prędkości SEAT" : "average SEAT movement speed proxy"}}', v:DATA.avgVelocity, c:'#7c2d12'}},
 ], [{{time:stroke.peakPhase,color:'#6d28d9',label:'strongest speed'}}], strokeVelocityZoomRange, true);
 renderOverlay();
}}
document.getElementById('coachComment').value = DATA.comment || '';
renderMetrics();
renderStrokeTable();
function renderTime() {{
 const events = document.getElementById('showStrokeEvents').checked
  ? DATA.starts.map((time, index) => ({{time:time,label:'S' + (index + 1) + ' start',color:'#1d4ed8',offset:0}})).concat(DATA.ends.map((time, index) => ({{time:time,label:'S' + (index + 1) + ' end',color:'#b91c1c',offset:14}})))
  : [];
 draw(document.getElementById('time'), DATA.time, selectedTimeSeries(), events, zoomRange, false);
}}
function canvasTimeAtEvent(canvas, event) {{
 const scale = canvas._scale;
 if (!scale) return null;
 const rect = canvas.getBoundingClientRect();
 const canvasX = (event.clientX - rect.left) * canvas.width / rect.width;
 const ratio = Math.min(1, Math.max(0, (canvasX - scale.padL) / scale.plotW));
 return scale.minX + ratio * (scale.maxX - scale.minX);
}}
function attachZoom(canvas, setter, redraw, minWidth) {{
 let dragStart = null;
 canvas.addEventListener('mousedown', event => {{ dragStart = canvasTimeAtEvent(canvas, event); }});
 canvas.addEventListener('mouseup', event => {{
  const end = canvasTimeAtEvent(canvas, event);
  if (dragStart === null || end === null) return;
  const start = Math.min(dragStart, end);
  const stop = Math.max(dragStart, end);
  dragStart = null;
  if (stop - start < minWidth) return;
  setter({{start:start,end:stop}});
  redraw();
 }});
 canvas.addEventListener('dblclick', () => {{ setter(null); redraw(); }});
}}
updateOverlayControls();
renderTime();
document.getElementById('strokeSelect').addEventListener('change', renderStroke);
document.querySelectorAll('.timeMetric').forEach(input => input.addEventListener('change', renderTime));
document.getElementById('normalizeTimeGraph').addEventListener('change', renderTime);
document.getElementById('showStrokeEvents').addEventListener('change', renderTime);
document.getElementById('timePrecision').addEventListener('change', () => {{ renderTime(); renderStroke(); renderOverlay(); }});
document.getElementById('applyZoom').addEventListener('click', () => {{
 const start = Number(document.getElementById('zoomStart').value);
 const end = Number(document.getElementById('zoomEnd').value);
 zoomRange = Number.isFinite(start) && Number.isFinite(end) && end > start ? {{start:start,end:end}} : null;
 renderTime();
}});
document.getElementById('resetZoom').addEventListener('click', () => {{
 zoomRange = null;
 document.getElementById('zoomStart').value = '';
 document.getElementById('zoomEnd').value = '';
 renderTime();
}});
document.querySelectorAll('.strokeMetric').forEach(input => input.addEventListener('change', renderStroke));
document.getElementById('showAverageStroke').addEventListener('change', renderStroke);
document.getElementById('overlayMetric').addEventListener('change', renderOverlay);
attachZoom(document.getElementById('time'), value => {{ zoomRange = value; }}, renderTime, 0.05);
attachZoom(document.getElementById('stroke'), value => {{ strokeZoomRange = value; }}, renderStroke, 1.0);
attachZoom(document.getElementById('strokeVelocity'), value => {{ strokeVelocityZoomRange = value; }}, renderStroke, 1.0);
attachZoom(document.getElementById('strokeOverlay'), value => {{ strokeOverlayZoomRange = value; }}, renderOverlay, 1.0);
renderStroke();
<\\/script></body></html>`;
}}
function downloadTextFile(filename, text) {{
  const blob = new Blob([text], {{ type: "text/html;charset=utf-8" }});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}}
function safeFilename(value) {{
  return String(value).replace(/[^a-z0-9_-]+/gi, "_").replace(/^_+|_+$/g, "") || "segment";
}}
function downloadSegment(index) {{
  const segment = segments[index];
  if (!segment) return;
  const commentInput = document.querySelector(`[data-action="comment"][data-index="${{index}}"]`);
  if (commentInput) segment.comment = commentInput.value;
  downloadTextFile(`${{safeFilename(segment.name)}}_rowing_report.html`, segmentReportHtml(segment));
}}
function downloadEnabledSegments() {{
  segments.forEach((segment, index) => {{
    if (segment.enabled) setTimeout(() => downloadSegment(index), index * 250);
  }});
}}
function canvasTimeAtEvent(canvas, event) {{
  const scale = canvas._scale;
  if (!scale) return null;
  const rect = canvas.getBoundingClientRect();
  const canvasX = (event.clientX - rect.left) * canvas.width / rect.width;
  const ratio = Math.min(1, Math.max(0, (canvasX - scale.padL) / scale.plotW));
  return scale.minX + ratio * (scale.maxX - scale.minX);
}}
function attachZoom(canvas, getter, setter, minWidth) {{
  let dragStart = null;
  canvas.addEventListener("mousedown", event => {{
    dragStart = canvasTimeAtEvent(canvas, event);
  }});
  canvas.addEventListener("mouseup", event => {{
    const end = canvasTimeAtEvent(canvas, event);
    if (dragStart === null || end === null) return;
    const start = Math.min(dragStart, end);
    const stop = Math.max(dragStart, end);
    dragStart = null;
    if (stop - start < minWidth) return;
    setter({{ start, end: stop }});
    render();
  }});
  canvas.addEventListener("dblclick", () => {{
    setter(null);
    render();
  }});
}}
function addSegment() {{
  const fullEnd = IMU_DATA.times[IMU_DATA.times.length - 1] || 0;
  const zoomStart = Number(document.getElementById("zoomStart").value);
  const zoomEnd = Number(document.getElementById("zoomEnd").value);
  const range = zoomRange || (Number.isFinite(zoomStart) && Number.isFinite(zoomEnd) && zoomEnd > zoomStart
    ? {{ start: zoomStart, end: zoomEnd }}
    : {{ start: 0, end: fullEnd }});
  const start = Math.max(0, Math.min(range.start, fullEnd));
  const end = Math.max(start, Math.min(range.end, fullEnd));
  const name = `Athlete ${{segments.length + 1}}`;
  segments.push({{ name, start, end, color: palette[segments.length % palette.length], enabled: true, comment: "" }});
  render();
}}
document.getElementById("minPeakMs").value = IMU_DATA.tuning.minPeakMs;
document.getElementById("smoothWindow").value = IMU_DATA.tuning.smoothWindow;
const timeCanvas = document.getElementById("timeCanvas");
attachZoom(timeCanvas, () => zoomRange, value => {{
  zoomRange = value;
  if (value) {{
    document.getElementById("zoomStart").value = value.start.toFixed(2);
    document.getElementById("zoomEnd").value = value.end.toFixed(2);
  }} else {{
    document.getElementById("zoomStart").value = "";
    document.getElementById("zoomEnd").value = "";
  }}
}}, 0.05);
attachZoom(document.getElementById("strokeCanvas"), () => strokeZoomRange, value => {{ strokeZoomRange = value; }}, 1.0);
attachZoom(document.getElementById("strokeVelocityCanvas"), () => strokeVelocityZoomRange, value => {{ strokeVelocityZoomRange = value; }}, 1.0);
attachZoom(document.getElementById("strokeOverlayCanvas"), () => strokeOverlayZoomRange, value => {{ strokeOverlayZoomRange = value; }}, 1.0);
document.getElementById("addSegment").addEventListener("click", addSegment);
document.getElementById("applyZoom").addEventListener("click", () => {{
  const start = Number(document.getElementById("zoomStart").value);
  const end = Number(document.getElementById("zoomEnd").value);
  zoomRange = Number.isFinite(start) && Number.isFinite(end) && end > start ? {{ start, end }} : null;
  render();
}});
document.getElementById("resetZoom").addEventListener("click", () => {{
  zoomRange = null;
  document.getElementById("zoomStart").value = "";
  document.getElementById("zoomEnd").value = "";
  render();
}});
document.querySelector("#segmentTable tbody").addEventListener("change", event => {{
  const target = event.target;
  const index = Number(target.dataset.index);
  if (!Number.isInteger(index) || !segments[index]) return;
  if (target.dataset.action === "name") segments[index].name = target.value;
  if (target.dataset.action === "start") segments[index].start = Number(target.value);
  if (target.dataset.action === "end") segments[index].end = Number(target.value);
  if (target.dataset.action === "comment") segments[index].comment = target.value;
  if (target.dataset.action === "toggle") segments[index].enabled = target.checked;
  render();
}});
document.querySelector("#segmentTable tbody").addEventListener("click", event => {{
  const target = event.target;
  if (target.dataset.action === "remove") {{ segments.splice(Number(target.dataset.index), 1); render(); }}
  if (target.dataset.action === "download") downloadSegment(Number(target.dataset.index));
}});
document.getElementById("languageSelect").addEventListener("input", event => {{
  currentLanguage = event.target.value;
  applyLanguage();
  render();
}});
["axisSelect", "invertAxis", "invertSeatOnly", "swapSeatBoat", "minPeakMs", "smoothWindow", "showStrokeEvents", "normalizeTimeGraph", "timePrecision", "strokeSelect", "showAverageStroke", "phaseTickStep", "overlayMetric"].forEach(id => {{
  document.getElementById(id).addEventListener("input", render);
}});
document.querySelectorAll(".timeMetric").forEach(input => input.addEventListener("change", render));
document.querySelectorAll(".strokeMetric").forEach(input => input.addEventListener("change", render));
initLanguage();
render();
</script>
"""


def make_report_html(
    *,
    label: str,
    seat_file: Path,
    boat_file: Path | None,
    seat_rows: list[dict],
    boat_rows: list[dict] | None,
    dual: dict,
    analysis: dict,
    tuning: Tuning,
    segment: SegmentSpec | None,
) -> str:
    times = dual["times"]
    source = dual["relative"] if dual["relative"] else dual["seat"]
    seat = dual["seat"]
    boat = dual["boat"]
    relative = dual["relative"]
    forward = source[DEFAULT_FORWARD_COLUMN]
    seat_forward = seat[DEFAULT_FORWARD_COLUMN]
    boat_forward = boat[DEFAULT_FORWARD_COLUMN] if boat else []

    seq_seat = sequence_gap_summary(seat_rows)
    seq_boat = sequence_gap_summary(boat_rows) if boat_rows else None
    sample = sampling_summary(times)

    cards = [
        metric_card("Detected strokes", str(analysis["stroke_count"]), "In the analyzed recording"),
        metric_card("Stroke rate", f"{fmt(analysis['stroke_rate_spm'], 1)} strokes/min", "Average pace"),
        metric_card("Rhythm consistency", f"{fmt(analysis['rhythm_consistency_score'], 0)} / 100", "Higher means more regular timing"),
        metric_card("Strongest seat drive speed", f"{fmt(analysis['speed_peak_phase_pct'], 1)}%", "Where the seat is fastest in the stroke"),
        metric_card("Strongest SEAT drive acceleration", f"{fmt(analysis['peak_force_proxy'], 3)} m/s^2", "Largest SEAT acceleration magnitude while the seat is moving in drive direction"),
        metric_card("Smoothness", f"{fmt(analysis['smoothness_jerk_rms'], 3)}", "Lower means smoother seat movement"),
    ]
    advanced_cards: list[str] = [
        metric_card("Rhythm spread", f"{fmt(analysis['rhythm_std_s'], 3)} s", "Technical timing spread"),
        metric_card("Rhythm variation", f"{fmt(analysis['rhythm_cv_pct'], 1)}%", "Technical rhythm CV"),
    ]

    if relative:
        rel_std = safe_std(relative[DEFAULT_FORWARD_COLUMN])
        seat_std = safe_std(seat_forward)
        boat_std = safe_std(boat_forward)
        subtraction_effect = (1.0 - rel_std / seat_std) * 100.0 if seat_std > 1e-9 else 0.0
        relative_dominance = rel_std / (rel_std + boat_std) * 100.0 if rel_std + boat_std > 1e-9 else 0.0
        advanced_cards.extend(
            [
                metric_card("Seat motion spread", f"{fmt(seat_std, 3)} m/s^2", "Raw SEAT forward variation"),
                metric_card("Boat reference spread", f"{fmt(boat_std, 3)} m/s^2", "BOAT forward variation"),
                metric_card("Seat-vs-boat spread", f"{fmt(rel_std, 3)} m/s^2", "SEAT minus BOAT variation"),
                metric_card("Boat subtraction effect", f"{fmt(subtraction_effect, 1)}%", "Positive means BOAT subtraction reduced SEAT variation"),
                metric_card("Seat-relative dominance", f"{fmt(relative_dominance, 1)}%", "Higher means seat-relative motion dominates"),
            ]
        )

    title = f"Offline Rowing IMU Report - {label}"
    segment_text = (
        f"Segment: {segment.start_s:.2f}s to {segment.end_s:.2f}s"
        if segment else
        "Segment: full overlapping recording"
    )
    boat_name = str(boat_file) if boat_file else "not used"
    seat_rotation = [
        math.sqrt(x * x + y * y + z * z)
        for x, y, z in zip(seat["gyro_x_rads"], seat["gyro_y_rads"], seat["gyro_z_rads"])
    ]
    rotation_series = [("SEAT rotation", times, seat_rotation, PLOT_COLORS["seat"])]
    if boat:
        boat_rotation = [
            math.sqrt(x * x + y * y + z * z)
            for x, y, z in zip(boat["gyro_x_rads"], boat["gyro_y_rads"], boat["gyro_z_rads"])
        ]
        rotation_series.append(("BOAT rotation", times, boat_rotation, PLOT_COLORS["boat"]))

    phase = analysis["phase"]
    html_parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>",
        """
        body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #1f2933; background: #f6f7f2; }
        header { padding: 28px 36px; background: #20251f; color: #f8fafc; }
        main { padding: 26px 36px 46px; max-width: 1180px; margin: 0 auto; }
        h1, h2, h3 { margin: 0 0 12px; }
        h2 { margin-top: 30px; border-bottom: 1px solid #d8ddd0; padding-bottom: 8px; }
        p { line-height: 1.55; }
        code { background: #eef1e7; padding: 1px 4px; border-radius: 4px; }
        .meta, .note { color: #556052; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0; }
        .metric { background: white; border: 1px solid #dfe4d7; border-radius: 8px; padding: 12px; }
        .metric span { display: block; color: #667064; font-size: 13px; }
        .metric strong { display: block; font-size: 22px; margin: 4px 0; color: #17201a; }
        .metric small { display: block; color: #667064; min-height: 30px; }
        table { width: 100%; border-collapse: collapse; background: white; margin: 14px 0 22px; }
        th, td { border-bottom: 1px solid #dfe4d7; padding: 9px 10px; text-align: left; vertical-align: top; }
        th { background: #edf1e8; }
        input, select, button { font: inherit; border: 1px solid #cbd5c0; border-radius: 6px; padding: 7px 8px; background: white; }
        button { background: #20251f; color: white; cursor: pointer; }
        button:hover { background: #343b32; }
        .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; background: white; border: 1px solid #dfe4d7; border-radius: 8px; padding: 12px; margin: 12px 0; }
        .controls label { display: grid; gap: 4px; color: #556052; font-size: 13px; }
        .controls input[type='checkbox'] { width: auto; }
        .interactive canvas { width: 100%; height: auto; background: white; border: 1px solid #dfe4d7; border-radius: 8px; margin: 12px 0 20px; }
        #strokeOverlayControls { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
        #strokeOverlayControls label { display: inline-flex; grid-template-columns: none; flex-direction: row; align-items: center; gap: 5px; min-width: 86px; padding: 5px 8px; border: 1px solid #dfe4d7; border-radius: 6px; background: #f8faf6; }
        #segmentTable input[type='number'] { width: 78px; }
        #segmentTable input:not([type='checkbox']) { max-width: 150px; }
        svg { width: 100%; height: auto; background: white; border: 1px solid #dfe4d7; border-radius: 8px; margin: 12px 0 20px; }
        .axis { stroke: #475046; stroke-width: 1.2; }
        .grid { stroke: #e4e8dd; stroke-width: 1; }
        .tick { fill: #667064; font-size: 12px; text-anchor: middle; }
        .tick.right { text-anchor: end; }
        .legend { fill: #1f2933; font-size: 12px; }
        .svg-title { fill: #17201a; font-weight: 700; font-size: 16px; }
        """,
        "</style></head><body>",
        f"<header><h1>{html.escape(title)}</h1><p>{html.escape(segment_text)}</p></header>",
        "<main>",
        "<h2>Files and tuning</h2>",
        f"<p class='meta'>SEAT file: <code>{html.escape(str(seat_file))}</code><br>BOAT file: <code>{html.escape(boat_name)}</code><br>{html.escape(dual['alignment_note'])}</p>",
        "<div class='grid'>",
        metric_card("Samples", str(len(times)), "Recorded data points"),
        metric_card("Duration", f"{fmt(sample['duration_s'], 2)} s", "Recording length"),
        metric_card("Sampling rate", f"{fmt(sample['rate_hz'], 2)} Hz", "Sensor update rate"),
        metric_card("SEAT gaps", str(seq_seat["gaps"]), "Missing SEAT rows"),
    ]
    if seq_boat:
        html_parts.append(metric_card("BOAT gaps", str(seq_boat["gaps"]), "Missing BOAT rows"))
    html_parts.extend(
        [
            "</div>",
            "<h2>Main metrics</h2>",
            "<div class='grid'>",
            *cards,
            "</div>",
            "<section id='joanna-method-note'>",
            "<h2>Method update for Joanna</h2>",
            "<p>I finally understood the point from the beginning: the stroke window should describe the stroke itself, not just the distance between two acceleration peaks. The offline analysis now uses the seat movement speed proxy to detect where a stroke starts and ends. Earlier versions relied more directly on smoothed acceleration peaks, which could make the window too dependent on a strong drive peak or on small bumps in the acceleration signal.</p>",
            "<p>The current approach is therefore based on the change into positive seat movement speed. A stroke is categorized from one detected positive drive-start movement to the next detected positive drive-start movement. This is intended to represent the rowing stroke as a time window more naturally than a peak-to-peak acceleration definition.</p>",
            "<p>This change should make the segmentation easier to explain and hopefully more accurate: the dashboard now uses velocity-based drive-start timing for categorization, while acceleration remains useful for describing the intensity and smoothness inside the detected stroke.</p>",
            "</section>",
            make_interactive_dashboard(dual, tuning),
            "<h2>IMU rotation stability</h2>",
            make_svg("Rotation rate magnitude", rotation_series, "rad/s"),
            "</main></body></html>",
        ]
    )
    return "\n".join(html_parts)


def parse_segment(text: str) -> SegmentSpec:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("segment must be NAME:START_SECONDS:END_SECONDS")
    label = parts[0].strip()
    if not label:
        raise argparse.ArgumentTypeError("segment name must not be empty")
    start_s = float(parts[1])
    end_s = float(parts[2])
    if end_s <= start_s:
        raise argparse.ArgumentTypeError("segment end must be greater than start")
    return SegmentSpec(label=label, start_s=start_s, end_s=end_s)


def safe_output_path(base: Path, suffix: str) -> Path:
    stem = base.stem
    clean = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in suffix)
    return base.with_name(f"{stem}_{clean}{base.suffix}")


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_report(
    *,
    label: str,
    output: Path,
    seat_file: Path,
    boat_file: Path | None,
    seat_rows: list[dict],
    boat_rows: list[dict] | None,
    full_dual: dict,
    tuning: Tuning,
    segment: SegmentSpec | None = None,
) -> dict:
    dual = slice_dual_data(full_dual, segment.start_s, segment.end_s) if segment else full_dual
    source = dual["relative"] if dual["relative"] else dual["seat"]
    analysis = analyze_signal(dual["times"], source[DEFAULT_FORWARD_COLUMN], tuning)
    html_report = make_report_html(
        label=label,
        seat_file=seat_file,
        boat_file=boat_file,
        seat_rows=seat_rows,
        boat_rows=boat_rows,
        dual=dual,
        analysis=analysis,
        tuning=tuning,
        segment=segment,
    )
    write_report(output, html_report)
    return {
        "label": label,
        "output": output,
        "strokes": analysis["stroke_count"],
        "spm": analysis["stroke_rate_spm"],
        "rhythm_consistency": analysis["rhythm_consistency_score"],
        "strongest_seat_drive_speed_phase": analysis["speed_peak_phase_pct"],
        "smoothness": analysis["smoothness_jerk_rms"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a self-contained offline rowing IMU HTML report.",
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        type=Path,
        default=DEFAULT_SEAT_CSV_FILE,
        help="CSV file or folder containing SEAT/BOAT CSV files",
    )
    parser.add_argument("boat_csv", nargs="?", type=Path, default=None, help="Optional BOAT CSV file when first argument is SEAT CSV")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_REPORT_FILE, help="Output HTML report path")
    parser.add_argument("--label", default="Full recording", help="Report label/title")
    parser.add_argument("--baseline-samples", type=int, default=200, help="Samples used for baseline removal")
    parser.add_argument("--smooth-window", type=int, default=10, help="Moving average window in samples")
    parser.add_argument("--min-peak-ms", type=float, default=800.0, help="Minimum gap between detected drive starts in milliseconds")
    parser.add_argument("--start-s", type=float, default=None, help="Analyze only from this second")
    parser.add_argument("--end-s", type=float, default=None, help="Analyze only until this second")
    parser.add_argument(
        "--segment",
        action="append",
        type=parse_segment,
        default=[],
        help="Create extra report for a segment, format NAME:START_SECONDS:END_SECONDS. Can be repeated.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    seat_file, boat_file, discovery_note = discover_input_files(args.input_path, args.boat_csv)
    if not seat_file.exists():
        raise SystemExit(f"SEAT file not found: {seat_file}")
    if boat_file and not boat_file.exists():
        raise SystemExit(f"BOAT file not found: {boat_file}")

    tuning = Tuning(
        baseline_samples=max(1, args.baseline_samples),
        smooth_window=max(1, args.smooth_window),
        min_peak_distance_s=max(0.0, args.min_peak_ms / 1000.0),
    )
    seat_rows = load_rows(seat_file)
    boat_rows = load_rows(boat_file) if boat_file else None
    full_dual = build_dual_samples(seat_rows, boat_rows)

    results = []
    base_segment = None
    if args.start_s is not None or args.end_s is not None:
        base_segment = SegmentSpec(
            label=args.label,
            start_s=args.start_s if args.start_s is not None else full_dual["times"][0],
            end_s=args.end_s if args.end_s is not None else full_dual["times"][-1],
        )
    results.append(
        run_report(
            label=args.label,
            output=args.output,
            seat_file=seat_file,
            boat_file=boat_file,
            seat_rows=seat_rows,
            boat_rows=boat_rows,
            full_dual=full_dual,
            tuning=tuning,
            segment=base_segment,
        )
    )

    for segment in args.segment:
        results.append(
            run_report(
                label=segment.label,
                output=safe_output_path(args.output, segment.label),
                seat_file=seat_file,
                boat_file=boat_file,
                seat_rows=seat_rows,
                boat_rows=boat_rows,
                full_dual=full_dual,
                tuning=tuning,
                segment=segment,
            )
        )

    print(discovery_note)
    print(f"SEAT file: {seat_file}")
    print(f"BOAT file: {boat_file if boat_file else 'not used'}")
    print(
        "Tuning: "
        f"baseline={tuning.baseline_samples}, smooth={tuning.smooth_window}, "
        f"min_peak={tuning.min_peak_distance_s * 1000:.0f} ms, "
        "stroke_window=drive-start to drive-start"
    )
    for result in results:
        print(
            f"{result['label']}: {result['output']} | "
            f"strokes={result['strokes']} | "
            f"{result['spm']:.1f} strokes/min | "
            f"rhythm_consistency={result['rhythm_consistency']:.0f}/100 | "
            f"strongest_seat_drive_speed={result['strongest_seat_drive_speed_phase']:.1f}% | "
            f"smoothness={result['smoothness']:.3f}"
        )


if __name__ == "__main__":
    main()
