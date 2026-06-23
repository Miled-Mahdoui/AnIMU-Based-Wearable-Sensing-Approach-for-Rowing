#!/usr/bin/env python3
"""Offline report generator for rowing IMU CSV logs.

The script reads one SEAT CSV file and optionally one BOAT/reference CSV file.
It creates an HTML report with signal statistics, stroke estimates, and
SEAT - BOAT relative plots.
"""
import argparse
import csv
import html
import math
from pathlib import Path
from statistics import mean, pstdev


ACC_COLUMNS = ("acc_x_ms2", "acc_y_ms2", "acc_z_ms2")
GYRO_COLUMNS = ("gyro_x_rads", "gyro_y_rads", "gyro_z_rads")
ROWING_AXIS_LABELS = {
    "acc_x_ms2": "seat_forward_acc",
    "acc_y_ms2": "seat_lateral_acc",
    "acc_z_ms2": "seat_vertical_acc",
    "gyro_x_rads": "seat_roll_rate",
    "gyro_y_rads": "seat_pitch_rate",
    "gyro_z_rads": "seat_yaw_rate",
}
ROWING_AXIS_DESCRIPTIONS = {
    "seat_forward_acc": "X: forward/back along the seat rail",
    "seat_lateral_acc": "Y: left/right across the boat",
    "seat_vertical_acc": "Z: up/down",
    "seat_roll_rate": "Rotation around the forward axis",
    "seat_pitch_rate": "Rotation around the lateral axis",
    "seat_yaw_rate": "Rotation around the vertical axis",
}
PHASE_MARKERS = (0, 25, 50, 75, 100)
# ============================================================
# Files to change for normal use
# ============================================================
#
# Put your SEAT and BOAT/reference CSV paths here. If the BOAT
# file exists, the report automatically adds the SEAT - BOAT
# relative analysis. You can still override both paths from the
# command line.
DEFAULT_SEAT_CSV_FILE = Path("/Users/mahdoui/Downloads/LOG002.CSV")
DEFAULT_BOAT_CSV_FILE = Path("/Users/mahdoui/Downloads/BOAT_LOG.csv")
DEFAULT_REPORT_FILE = Path("/Users/mahdoui/Downloads/imu_report.html")

# Backward-compatible name used by the argument parser below.
DEFAULT_CSV_FILE = DEFAULT_SEAT_CSV_FILE
PLOT_COLORS = ("#0f766e", "#b91c1c", "#1d4ed8")


def load_rows(path):
    """Load firmware CSV rows and convert numeric columns to Python numbers."""
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []

        for line_number, row in enumerate(reader, start=2):
            try:
                parsed = {
                    "device_id": row["device_id"],
                    "sequence": int(row["sequence"]),
                    "time_us": int(row["time_us"]),
                }

                for column in ACC_COLUMNS + GYRO_COLUMNS:
                    parsed[column] = float(row[column])

                rows.append(parsed)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid CSV data in line {line_number}: {error}") from error

    return rows


def default_marker_file(csv_file):
    return csv_file.with_name(f"{csv_file.stem}_markers.csv")


def load_markers(path):
    if not path or not path.exists():
        return []

    markers = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                markers.append({
                    "time_us": int(row["time_us"]),
                    "sequence": int(row["sequence"]),
                    "label": row.get("label", "marker"),
                })
            except (TypeError, ValueError, KeyError):
                continue
    return markers


def markers_to_plot_times(rows, markers):
    if not rows:
        return []

    start_time_us = rows[0]["time_us"]
    end_time_us = rows[-1]["time_us"]
    result = []

    for marker in markers:
        marker_time_us = marker["time_us"]
        if start_time_us <= marker_time_us <= end_time_us:
            result.append({
                "time_s": (marker_time_us - start_time_us) / 1_000_000.0,
                "label": marker["label"],
                "sequence": marker["sequence"],
            })

    return result


def summarize_column(rows, column):
    values = [row[column] for row in rows]
    return {
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "std": pstdev(values) if len(values) > 1 else 0.0,
        "range": max(values) - min(values),
    }


def format_seconds(microseconds):
    return microseconds / 1_000_000.0


def print_group_summary(title, rows, columns, unit):
    print()
    print(title)
    print("-" * len(title))

    summaries = {}
    for column in columns:
        label = display_label(column)
        summaries[column] = summarize_column(rows, column)
        summary = summaries[column]
        print(
            f"{label:18s} "
            f"min={summary['min']:9.3f} {unit}  "
            f"max={summary['max']:9.3f} {unit}  "
            f"mean={summary['mean']:9.3f} {unit}  "
            f"std={summary['std']:8.3f}  "
            f"range={summary['range']:9.3f}"
        )

    strongest = max(columns, key=lambda column: summaries[column]["range"])
    print(
        f"Strongest change: {display_label(strongest)} "
        f"(raw: {strongest}, range {summaries[strongest]['range']:.3f} {unit})"
    )
    return strongest


def check_sequences(rows):
    """Return expected/actual sequence pairs where rows are missing."""
    expected = rows[0]["sequence"]
    gaps = []

    for row in rows:
        sequence = row["sequence"]
        if sequence != expected:
            gaps.append((expected, sequence))
            expected = sequence
        expected += 1

    return gaps


def check_sample_intervals(rows):
    intervals = [
        rows[index]["time_us"] - rows[index - 1]["time_us"]
        for index in range(1, len(rows))
    ]

    if not intervals:
        return None

    return {
        "min_us": min(intervals),
        "max_us": max(intervals),
        "mean_us": mean(intervals),
        "std_us": pstdev(intervals) if len(intervals) > 1 else 0.0,
        "rate_hz": 1_000_000.0 / mean(intervals),
    }


def estimate_static_tilt(rows, sample_count=200):
    static_rows = rows[: min(sample_count, len(rows))]
    ax = mean(row["acc_x_ms2"] for row in static_rows)
    ay = mean(row["acc_y_ms2"] for row in static_rows)
    az = mean(row["acc_z_ms2"] for row in static_rows)
    magnitude = math.sqrt(ax * ax + ay * ay + az * az)
    return ax, ay, az, magnitude


def display_label(column):
    return ROWING_AXIS_LABELS.get(column, column)


def display_description(column):
    label = display_label(column)
    return ROWING_AXIS_DESCRIPTIONS.get(label, "")


def values_for(rows, column):
    return [row[column] for row in rows]


def time_seconds(rows):
    start_time = rows[0]["time_us"]
    return [(row["time_us"] - start_time) / 1_000_000.0 for row in rows]


def downsample_series(points, max_points=1200):
    if len(points) <= max_points:
        return points

    step = math.ceil(len(points) / max_points)
    return points[::step]


def scale(value, source_min, source_max, target_min, target_max):
    if source_max == source_min:
        return (target_min + target_max) / 2.0
    ratio = (value - source_min) / (source_max - source_min)
    return target_min + ratio * (target_max - target_min)


def y_domain(values, include_zero=True):
    """Return a padded y-axis range; include zero to keep plots comparable."""
    y_min = min(values)
    y_max = max(values)
    if include_zero:
        y_min = min(y_min, 0.0)
        y_max = max(y_max, 0.0)

    y_padding = (y_max - y_min) * 0.08 if y_max != y_min else 1.0
    return y_min - y_padding, y_max + y_padding


def y_tick_values(y_min, y_max, count=7):
    if count <= 1 or y_max == y_min:
        return [y_min]
    step = (y_max - y_min) / (count - 1)
    return [y_min + step * index for index in range(count)]


def make_y_grid_svg(y_min, y_max, margin_left, margin_top, plot_width, plot_height):
    ticks = y_tick_values(y_min, y_max)
    lines = []
    labels = []
    for tick in ticks:
        y = scale(tick, y_min, y_max, margin_top + plot_height, margin_top)
        grid_class = "zero-grid" if abs(tick) < 1e-9 else "grid"
        lines.append(
            f'<line x1="{margin_left}" y1="{y:.1f}" '
            f'x2="{margin_left + plot_width}" y2="{y:.1f}" class="{grid_class}" />'
        )
        labels.append(f'<text x="12" y="{y + 4:.1f}" class="tick">{tick:.2f}</text>')
    return "".join(lines), "".join(labels)


def svg_polyline(times, values, x_min, x_max, y_min, y_max, width, height, color):
    margin_left = 58
    margin_right = 18
    margin_top = 18
    margin_bottom = 34

    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    points = downsample_series(list(zip(times, values)))
    point_text = []

    for time_s, value in points:
        x = scale(time_s, x_min, x_max, margin_left, margin_left + plot_width)
        y = scale(value, y_min, y_max, margin_top + plot_height, margin_top)
        point_text.append(f"{x:.1f},{y:.1f}")

    return (
        f'<polyline points="{" ".join(point_text)}" '
        f'fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linejoin="round" stroke-linecap="round" />'
    )


def make_marker_svg(markers, x_min, x_max, margin_left, margin_top, plot_width, plot_height):
    if not markers:
        return ""

    marker_svg = []
    for marker in markers:
        time_s = marker["time_s"]
        if time_s < x_min or time_s > x_max:
            continue
        x = scale(time_s, x_min, x_max, margin_left, margin_left + plot_width)
        color = "#b91c1c" if "bad" in marker["label"] else "#0f766e"
        label = html.escape(marker["label"])
        marker_svg.append(
            f'<line x1="{x:.1f}" y1="{margin_top}" x2="{x:.1f}" '
            f'y2="{margin_top + plot_height}" stroke="{color}" '
            'stroke-width="1.4" stroke-dasharray="5 4" />'
            f'<text x="{x + 4:.1f}" y="{margin_top + 72}" '
            f'class="marker-label" transform="rotate(-90 {x + 4:.1f} {margin_top + 72})">{label}</text>'
        )
    return "".join(marker_svg)


def make_plot(title, rows, columns, unit, colors=PLOT_COLORS, markers=None):
    width = 980
    height = 300
    margin_left = 58
    margin_right = 18
    margin_top = 18
    margin_bottom = 34

    times = time_seconds(rows)
    x_min = min(times)
    x_max = max(times)

    all_values = []
    for column in columns:
        all_values.extend(values_for(rows, column))

    y_min, y_max = y_domain(all_values)

    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    x_axis_y = margin_top + plot_height

    polylines = []
    legend = []

    for index, column in enumerate(columns):
        color = colors[index % len(colors)]
        polylines.append(
            svg_polyline(
                times,
                values_for(rows, column),
                x_min,
                x_max,
                y_min,
                y_max,
                width,
                height,
                color,
            )
        )
        legend.append(
            f'<span class="legend-item"><span class="swatch" '
            f'style="background:{color}"></span>{html.escape(display_label(column))}</span>'
        )

    y_grid_svg, y_label_svg = make_y_grid_svg(
        y_min,
        y_max,
        margin_left,
        margin_top,
        plot_width,
        plot_height,
    )
    marker_svg = make_marker_svg(
        markers or [],
        x_min,
        x_max,
        margin_left,
        margin_top,
        plot_width,
        plot_height,
    )

    return f"""
    <section class="plot-section">
      <div class="plot-header">
        <h2>{html.escape(title)}</h2>
        <div class="legend">{"".join(legend)}</div>
      </div>
      <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} plot">
        <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
        <line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{x_axis_y}" class="axis" />
        <line x1="{margin_left}" y1="{x_axis_y}" x2="{margin_left + plot_width}" y2="{x_axis_y}" class="axis" />
        {y_grid_svg}
        {y_label_svg}
        <text x="{margin_left}" y="{height - 10}" class="tick">{x_min:.1f}s</text>
        <text x="{margin_left + plot_width - 48}" y="{height - 10}" class="tick">{x_max:.1f}s</text>
        <text x="{width / 2 - 25:.1f}" y="{height - 10}" class="tick">time</text>
        <text x="{width - 70}" y="{margin_top + 14}" class="tick">{html.escape(unit)}</text>
        {marker_svg}
        {"".join(polylines)}
      </svg>
    </section>
    """


def moving_average(values, window):
    """Return a causal moving average over the latest `window` values."""
    if window <= 1:
        return list(values)

    smoothed = []
    running_sum = 0.0
    queue = []

    for value in values:
        queue.append(value)
        running_sum += value

        if len(queue) > window:
            running_sum -= queue.pop(0)

        smoothed.append(running_sum / len(queue))

    return smoothed


def centered(values):
    """Subtract the initial baseline so offsets/mounting angle are reduced."""
    if not values:
        return []
    baseline_count = min(200, len(values))
    baseline = mean(values[:baseline_count])
    return [value - baseline for value in values]


def detect_positive_peaks(times, values, min_distance_s=0.85):
    """Detect stroke candidates as positive local peaks above a threshold."""
    if len(values) < 3:
        return []

    signal_std = pstdev(values) if len(values) > 1 else 0.0
    threshold = max(0.35 * signal_std, 0.8)
    peaks = []
    last_peak_time = -1e9

    for index in range(1, len(values) - 1):
        value = values[index]
        if value < threshold:
            continue
        if value < values[index - 1] or value < values[index + 1]:
            continue
        if times[index] - last_peak_time < min_distance_s:
            if peaks and value > values[peaks[-1]]:
                peaks[-1] = index
                last_peak_time = times[index]
            continue
        peaks.append(index)
        last_peak_time = times[index]

    return peaks


def interpolate_segment(times, values, start_index, end_index, phase_count=101):
    start_time = times[start_index]
    end_time = times[end_index]
    if end_time <= start_time:
        return [values[start_index]] * phase_count

    segment_times = times[start_index:end_index + 1]
    segment_values = values[start_index:end_index + 1]
    result = []
    cursor = 0

    for phase in range(phase_count):
        target = start_time + (end_time - start_time) * (phase / (phase_count - 1))

        while cursor < len(segment_times) - 2 and segment_times[cursor + 1] < target:
            cursor += 1

        left_time = segment_times[cursor]
        right_time = segment_times[cursor + 1] if cursor + 1 < len(segment_times) else left_time
        left_value = segment_values[cursor]
        right_value = segment_values[cursor + 1] if cursor + 1 < len(segment_values) else left_value

        if right_time == left_time:
            result.append(left_value)
        else:
            ratio = (target - left_time) / (right_time - left_time)
            result.append(left_value + ratio * (right_value - left_value))

    return result


def integrate_velocity(times, acceleration):
    """Integrate acceleration into a drift-corrected velocity proxy."""
    if not acceleration:
        return []

    velocity = [0.0]
    for index in range(1, len(acceleration)):
        dt = times[index] - times[index - 1]
        area = 0.5 * (acceleration[index] + acceleration[index - 1]) * dt
        velocity.append(velocity[-1] + area)

    if len(velocity) > 1:
        # Remove a straight-line end drift. This keeps the proxy useful for
        # shape comparison without claiming exact physical speed.
        drift_per_sample = velocity[-1] / (len(velocity) - 1)
        velocity = [value - drift_per_sample * index for index, value in enumerate(velocity)]

    return velocity


def speed_proxy_metrics(times, acceleration):
    """Calculate speed proxy, peak phase, and jerk-based smoothness."""
    smoothed = moving_average(centered(acceleration), 9)
    velocity = integrate_velocity(times, smoothed)
    if not velocity or not times:
        return {
            "acceleration": smoothed,
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
        "acceleration": smoothed,
        "velocity": velocity,
        "speed_proxy": abs(velocity[peak_index]),
        "peak_time_s": times[peak_index],
        "peak_phase_pct": (times[peak_index] - times[0]) / duration_s * 100.0,
        "smoothness_jerk_rms": rms(jerks),
    }


def average_segments(segments):
    if not segments:
        return []

    length = len(segments[0])
    return [
        mean(segment[index] for segment in segments)
        for index in range(length)
    ]


def rms(values):
    if not values:
        return 0.0
    return math.sqrt(mean(value * value for value in values))


def analyze_strokes(rows, forward_column):
    """Detect strokes and build average 0-100% phase curves."""
    times = time_seconds(rows)
    raw_forward = values_for(rows, forward_column)
    centered_forward = centered(raw_forward)
    smoothed_forward = moving_average(centered_forward, 9)
    peaks = detect_positive_peaks(times, smoothed_forward)
    stroke_segments = list(zip(peaks, peaks[1:]))
    durations = [times[end] - times[start] for start, end in stroke_segments]

    avg_acc_segments = []
    avg_velocity_segments = []
    avg_roll_segments = []
    avg_pitch_segments = []
    avg_yaw_segments = []

    roll = moving_average(centered(values_for(rows, "gyro_x_rads")), 9)
    pitch = moving_average(centered(values_for(rows, "gyro_y_rads")), 9)
    yaw = moving_average(centered(values_for(rows, "gyro_z_rads")), 9)

    for start, end in stroke_segments:
        segment_times = times[start:end + 1]
        local_times = [time_s - segment_times[0] for time_s in segment_times]
        segment_acc = smoothed_forward[start:end + 1]
        segment_velocity = integrate_velocity(local_times, segment_acc)

        avg_acc_segments.append(interpolate_segment(times, smoothed_forward, start, end))
        avg_velocity_segments.append(
            interpolate_segment(times, [0.0] * start + segment_velocity + [0.0] * (len(times) - end - 1), start, end)
        )
        avg_roll_segments.append(interpolate_segment(times, roll, start, end))
        avg_pitch_segments.append(interpolate_segment(times, pitch, start, end))
        avg_yaw_segments.append(interpolate_segment(times, yaw, start, end))

    duration_s = times[-1] - times[0] if len(times) > 1 else 0.0
    stroke_count = len(stroke_segments)
    stroke_rate = (stroke_count / duration_s * 60.0) if duration_s > 0 else 0.0

    avg_velocity = average_segments(avg_velocity_segments)
    speed_proxy = max(abs(value) for value in avg_velocity) if avg_velocity else 0.0
    full_speed = speed_proxy_metrics(times, raw_forward)

    return {
        "times": times,
        "raw_forward": raw_forward,
        "centered_forward": centered_forward,
        "smoothed_forward": smoothed_forward,
        "peaks": peaks,
        "stroke_segments": stroke_segments,
        "stroke_count": stroke_count,
        "stroke_rate": stroke_rate,
        "avg_duration": mean(durations) if durations else 0.0,
        "min_duration": min(durations) if durations else 0.0,
        "max_duration": max(durations) if durations else 0.0,
        "avg_phase": [phase for phase in range(101)],
        "avg_acc": average_segments(avg_acc_segments),
        "avg_velocity": avg_velocity,
        "avg_roll": average_segments(avg_roll_segments),
        "avg_pitch": average_segments(avg_pitch_segments),
        "avg_yaw": average_segments(avg_yaw_segments),
        "speed_proxy": speed_proxy,
        "peak_phase_pct": full_speed["peak_phase_pct"],
        "smoothness_jerk_rms": full_speed["smoothness_jerk_rms"],
    }


def make_series_plot(title, series, unit, description=""):
    width = 980
    height = 300
    margin_left = 58
    margin_right = 18
    margin_top = 18
    margin_bottom = 34

    x_values = []
    y_values = []
    for _, xs, ys, _ in series:
        x_values.extend(xs)
        y_values.extend(ys)

    if not x_values or not y_values:
        return ""

    x_min = min(x_values)
    x_max = max(x_values)
    y_min, y_max = y_domain(y_values)

    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    x_axis_y = margin_top + plot_height
    y_grid_svg, y_label_svg = make_y_grid_svg(
        y_min,
        y_max,
        margin_left,
        margin_top,
        plot_width,
        plot_height,
    )

    polylines = []
    legend = []
    for label, xs, ys, color in series:
        polylines.append(svg_polyline(xs, ys, x_min, x_max, y_min, y_max, width, height, color))
        legend.append(
            f'<span class="legend-item"><span class="swatch" '
            f'style="background:{color}"></span>{html.escape(label)}</span>'
        )

    description_html = f'<p class="note">{html.escape(description)}</p>' if description else ""

    return f"""
    <section class="plot-section">
      <div class="plot-header">
        <h2>{html.escape(title)}</h2>
        <div class="legend">{"".join(legend)}</div>
      </div>
      {description_html}
      <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)} plot">
        <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" />
        <line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{x_axis_y}" class="axis" />
        <line x1="{margin_left}" y1="{x_axis_y}" x2="{margin_left + plot_width}" y2="{x_axis_y}" class="axis" />
        {y_grid_svg}
        {y_label_svg}
        <text x="{margin_left}" y="{height - 10}" class="tick">{x_min:.1f}</text>
        <text x="{margin_left + plot_width - 48}" y="{height - 10}" class="tick">{x_max:.1f}</text>
        <text x="{width / 2 - 25:.1f}" y="{height - 10}" class="tick">phase/time</text>
        <text x="{width - 88}" y="{margin_top + 14}" class="tick">{html.escape(unit)}</text>
        {"".join(polylines)}
      </svg>
    </section>
    """


def make_phase_explanation():
    return """
    <section class="table-section">
      <h2>Rowing interpretation</h2>
      <p class="note">
        The stroke metrics are estimated from the mounted X axis, defined here as
        seat forward/back motion. A rowing stroke is normalized from one detected
        acceleration peak to the next. In a real rowing motion, the leg drive is
        expected to dominate the strongest seat acceleration interval, while body
        swing and arm draw appear later and often show up more clearly in pitch
        and yaw rates. These labels are therefore an interpretation aid, not yet a
        validated biomechanics measurement.
      </p>
      <table>
        <thead>
          <tr><th>Phase</th><th>Approx. meaning</th><th>What to inspect</th></tr>
        </thead>
        <tbody>
          <tr><td>0-25%</td><td>Start / catch-side transition</td><td>Early forward acceleration and pitch change</td></tr>
          <tr><td>25-60%</td><td>Leg-dominant drive candidate</td><td>Largest seat acceleration and velocity proxy</td></tr>
          <tr><td>60-80%</td><td>Body swing / finish candidate</td><td>Pitch and yaw stability</td></tr>
          <tr><td>80-100%</td><td>Recovery back to next stroke</td><td>Smoother acceleration and lower rotation</td></tr>
        </tbody>
      </table>
    </section>
    """


def make_calculation_explanation():
    return """
    <section class="table-section">
      <h2>How the metrics are calculated</h2>
      <p class="note">
        This table defines the report metrics so the live dashboard and offline
        analysis can be interpreted consistently.
      </p>
      <table>
        <thead><tr><th>Metric</th><th>Calculation</th><th>Represents</th></tr></thead>
        <tbody>
          <tr><td>Mean / Mittel</td><td><code>sum(values) / count(values)</code></td><td>Average level of the signal. For acceleration, this can include gravity and mounting angle.</td></tr>
          <tr><td>Std</td><td><code>sqrt(mean((value - mean)^2))</code></td><td>How strongly the signal varies around its mean. Higher usually means more movement or more noise.</td></tr>
          <tr><td>Range</td><td><code>max(value) - min(value)</code></td><td>Total spread of the signal in the recording.</td></tr>
          <tr><td>RMS</td><td><code>sqrt(mean(value^2))</code></td><td>Overall signal magnitude, useful when positive and negative values both matter.</td></tr>
          <tr><td>Detected strokes</td><td>Peaks in smoothed, centered forward acceleration with a minimum time distance.</td><td>First-pass stroke count. It must be validated with real rowing video or manual labels.</td></tr>
          <tr><td>Stroke rate</td><td><code>detected strokes / duration seconds * 60</code></td><td>Estimated strokes per minute over the file.</td></tr>
          <tr><td>Average stroke</td><td><code>mean(time between detected peaks)</code></td><td>Typical duration of one detected stroke cycle.</td></tr>
          <tr><td>Relative speed proxy</td><td>Trapezoid integration of smoothed forward acceleration, with simple drift correction.</td><td>Shape/intensity proxy for seat speed. It is not calibrated to real m/s yet.</td></tr>
          <tr><td>Power transfer proxy</td><td><code>max(0, forward acceleration * relative speed)</code></td><td>Relative timing/intensity of the drive. It is not watts.</td></tr>
          <tr><td>Sequence gaps</td><td><code>last sequence - first sequence + 1 - rows</code></td><td>Missing samples in the logger stream.</td></tr>
        </tbody>
      </table>
    </section>
    """


def build_reference_analysis(seat_rows, reference_rows, reference_file):
    """Build SEAT - BOAT metrics from two CSV files aligned by row index."""
    count = min(len(seat_rows), len(reference_rows))
    if count == 0:
        return None

    seat = seat_rows[:count]
    reference = reference_rows[:count]
    start_time_us = seat[0]["time_us"]
    times = [(row["time_us"] - start_time_us) / 1_000_000.0 for row in seat]

    def rel(column):
        return [seat[index][column] - reference[index][column] for index in range(count)]

    seat_forward = [row["acc_x_ms2"] for row in seat]
    reference_forward = [row["acc_x_ms2"] for row in reference]
    relative_forward = rel("acc_x_ms2")
    seat_plus_boat_forward = [
        seat_forward[index] + reference_forward[index]
        for index in range(count)
    ]
    boat_speed = speed_proxy_metrics(times, reference_forward)
    relative_speed = speed_proxy_metrics(times, relative_forward)
    seat_std = pstdev(seat_forward) if len(seat_forward) > 1 else 0.0
    boat_std = pstdev(reference_forward) if len(reference_forward) > 1 else 0.0
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
        "reference_file": reference_file,
        "reference_device": reference[0]["device_id"],
        "count": count,
        "times": times,
        "seat_forward": seat_forward,
        "reference_forward": reference_forward,
        "seat_plus_boat_forward": seat_plus_boat_forward,
        "relative_forward": relative_forward,
        "boat_velocity_proxy": boat_speed["velocity"],
        "relative_velocity_proxy": relative_speed["velocity"],
        "relative_lateral": rel("acc_y_ms2"),
        "relative_vertical": rel("acc_z_ms2"),
        "relative_pitch": rel("gyro_y_rads"),
        "relative_yaw": rel("gyro_z_rads"),
        "relative_roll": rel("gyro_x_rads"),
        "std_seat_forward": seat_std,
        "std_boat_forward": boat_std,
        "std_relative_forward": rel_std,
        "rms_relative_forward": rms(relative_forward),
        "rms_seat_plus_boat_forward": rms(seat_plus_boat_forward),
        "subtraction_reduction_pct": subtraction_reduction_pct,
        "relative_dominance_pct": relative_dominance_pct,
        "boat_speed_proxy": boat_speed["speed_proxy"],
        "boat_speed_peak_time_s": boat_speed["peak_time_s"],
        "boat_speed_peak_phase_pct": boat_speed["peak_phase_pct"],
        "boat_smoothness_jerk_rms": boat_speed["smoothness_jerk_rms"],
        "relative_speed_proxy": relative_speed["speed_proxy"],
        "relative_speed_peak_time_s": relative_speed["peak_time_s"],
        "relative_speed_peak_phase_pct": relative_speed["peak_phase_pct"],
        "relative_smoothness_jerk_rms": relative_speed["smoothness_jerk_rms"],
    }


def make_reference_section(reference_analysis):
    if not reference_analysis:
        return ""

    rel_forward = reference_analysis["relative_forward"]
    rel_pitch = reference_analysis["relative_pitch"]
    rel_yaw = reference_analysis["relative_yaw"]
    metrics = [
        ("Reference file", str(reference_analysis["reference_file"])),
        ("Reference device", reference_analysis["reference_device"]),
        ("Aligned samples", str(reference_analysis["count"])),
        ("Mean relative forward", f"{mean(rel_forward):.3f} m/s^2"),
        ("Std SEAT forward", f"{reference_analysis['std_seat_forward']:.3f} m/s^2"),
        ("Std BOAT forward", f"{reference_analysis['std_boat_forward']:.3f} m/s^2"),
        ("Std relative forward", f"{reference_analysis['std_relative_forward']:.3f} m/s^2"),
        ("RMS relative forward", f"{reference_analysis['rms_relative_forward']:.3f} m/s^2"),
        ("SEAT + BOAT RMS", f"{reference_analysis['rms_seat_plus_boat_forward']:.3f} m/s^2"),
        ("Subtraction effect", f"{reference_analysis['subtraction_reduction_pct']:.1f}%"),
        ("Relative dominance", f"{reference_analysis['relative_dominance_pct']:.1f}%"),
        ("BOAT speed proxy", f"{reference_analysis['boat_speed_proxy']:.3f} rel."),
        ("BOAT peak phase", f"{reference_analysis['boat_speed_peak_phase_pct']:.1f}%"),
        ("BOAT smoothness", f"{reference_analysis['boat_smoothness_jerk_rms']:.3f} jerk RMS"),
        ("Relative speed proxy", f"{reference_analysis['relative_speed_proxy']:.3f} rel."),
        ("Relative peak phase", f"{reference_analysis['relative_speed_peak_phase_pct']:.1f}%"),
        ("Relative smoothness", f"{reference_analysis['relative_smoothness_jerk_rms']:.3f} jerk RMS"),
        ("Mean relative pitch", f"{mean(rel_pitch):.3f} rad/s"),
        ("Mean relative yaw", f"{mean(rel_yaw):.3f} rad/s"),
    ]
    metric_html = "".join(
        f'<div class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in metrics
    )
    times = reference_analysis["times"]

    return f"""
    <section class="table-section">
      <h2>Two-IMU Relative Analysis</h2>
      <p class="note">
        This section compares the primary SEAT file with a reference/BOAT file by sample index.
        The core calculation is <strong>relative = SEAT - BOAT</strong>. For final experiments,
        start both sensors as closely together as possible or synchronize them more carefully later.
      </p>
      <section class="metrics">{metric_html}</section>
      <table>
        <thead><tr><th>Value</th><th>Calculation</th><th>Meaning</th></tr></thead>
        <tbody>
          <tr><td>Relative forward</td><td><code>SEAT acc_x - BOAT acc_x</code></td><td>Forward/back seat acceleration after removing boat reference motion.</td></tr>
          <tr><td>SEAT + BOAT RMS</td><td><code>sqrt(mean((SEAT acc_x + BOAT acc_x)^2))</code></td><td>Diagnostic only: shows same-direction/shared movement or mounting drift. It is not the main rowing score.</td></tr>
          <tr><td>Subtraction effect</td><td><code>(1 - std(relative) / std(SEAT)) * 100</code></td><td>Positive means boat subtraction reduced the raw seat signal; negative means the relative signal is stronger than raw SEAT motion.</td></tr>
          <tr><td>Relative dominance</td><td><code>std(relative) / (std(relative) + std(BOAT)) * 100</code></td><td>Higher means the file is dominated by seat-relative movement rather than boat reference motion.</td></tr>
          <tr><td>BOAT speed proxy</td><td>Drift-corrected integration of centered, smoothed BOAT forward acceleration.</td><td>Approximate boat-only velocity shape. It is useful for comparing strokes, not calibrated boat speed in m/s.</td></tr>
          <tr><td>Relative speed proxy</td><td>Drift-corrected integration of centered, smoothed <code>SEAT - BOAT</code> forward acceleration.</td><td>Approximate seat-to-boat velocity shape.</td></tr>
          <tr><td>Peak phase</td><td><code>time of max(abs(speed proxy)) / recording duration * 100</code></td><td>Shows when the strongest speed proxy occurs inside the analyzed window.</td></tr>
          <tr><td>Smoothness</td><td><code>RMS(diff(smoothed acceleration) / diff(time))</code></td><td>Jerk-based roughness indicator. Lower values mean a smoother acceleration signal.</td></tr>
        </tbody>
      </table>
    </section>
    {make_series_plot(
        "SEAT vs BOAT/reference forward acceleration",
        (
            ("seat_forward_acc", times, reference_analysis["seat_forward"], "#7c2d12"),
            ("boat_forward_acc", times, reference_analysis["reference_forward"], "#0f766e"),
            ("relative_forward_acc", times, reference_analysis["relative_forward"], "#1d4ed8"),
        ),
        "m/s^2",
        "The blue line is the relative forward signal: SEAT minus BOAT/reference.",
    )}
    {make_series_plot(
        "Forward diagnostic: subtraction and sum",
        (
            ("relative_forward_acc", times, reference_analysis["relative_forward"], "#1d4ed8"),
            ("seat_plus_boat_forward", times, reference_analysis["seat_plus_boat_forward"], "#64748b"),
        ),
        "m/s^2",
        "The relative signal is the useful rowing signal. SEAT + BOAT is a diagnostic for shared/same-direction movement or mounting drift.",
    )}
    {make_series_plot(
        "BOAT and relative velocity proxies",
        (
            ("boat_velocity_proxy", times, reference_analysis["boat_velocity_proxy"], "#0f766e"),
            ("relative_velocity_proxy", times, reference_analysis["relative_velocity_proxy"], "#1d4ed8"),
        ),
        "relative",
        "These curves are drift-corrected speed proxies from acceleration integration. They show timing and smoothness, not calibrated m/s.",
    )}
    {make_series_plot(
        "Relative acceleration axes",
        (
            ("relative_forward_acc", times, reference_analysis["relative_forward"], "#7c2d12"),
            ("relative_lateral_acc", times, reference_analysis["relative_lateral"], "#0f766e"),
            ("relative_vertical_acc", times, reference_analysis["relative_vertical"], "#1d4ed8"),
        ),
        "m/s^2",
        "These signals show what remains after subtracting the BOAT/reference acceleration from the SEAT acceleration.",
    )}
    {make_series_plot(
        "Relative rotation rates",
        (
            ("relative_roll_rate", times, reference_analysis["relative_roll"], "#0f766e"),
            ("relative_pitch_rate", times, reference_analysis["relative_pitch"], "#b91c1c"),
            ("relative_yaw_rate", times, reference_analysis["relative_yaw"], "#1d4ed8"),
        ),
        "rad/s",
        "Rotation-rate differences help show whether SEAT motion differs from the BOAT/reference orientation changes.",
    )}
    """


def make_marker_table(plot_markers):
    if not plot_markers:
        return ""

    rows_html = []
    for plot_marker in plot_markers:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(plot_marker['label'])}</td>"
            f"<td>{plot_marker['time_s']:.3f} s</td>"
            f"<td>{plot_marker['sequence']}</td>"
            "</tr>"
        )

    return f"""
    <section class="table-section">
      <h2>Markers</h2>
      <p class="note">Markers are written during live recording and shown here as vertical lines in the time-based plots.</p>
      <table>
        <thead><tr><th>Label</th><th>Time</th><th>Sequence</th></tr></thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </section>
    """


def write_html_report(path, csv_file, rows, strongest_acc, interval_summary, stroke_analysis, markers, reference_analysis):
    summaries = {column: summarize_column(rows, column) for column in ACC_COLUMNS + GYRO_COLUMNS}
    duration_s = (rows[-1]["time_us"] - rows[0]["time_us"]) / 1_000_000.0
    sequence_gap_count = len(check_sequences(rows))
    plot_markers = markers_to_plot_times(rows, markers)

    cards = [
        ("Datei", str(csv_file)),
        ("Messwerte", str(len(rows))),
        ("Dauer", f"{duration_s:.3f} s"),
        ("Sampling", f"{interval_summary['rate_hz']:.2f} Hz" if interval_summary else "n/a"),
        ("Sequenzluecken", str(sequence_gap_count)),
        ("Hauptachse", display_label(strongest_acc)),
        ("Strokes", str(stroke_analysis["stroke_count"])),
        ("Stroke Rate", f"{stroke_analysis['stroke_rate']:.1f} spm"),
        ("Avg Stroke", f"{stroke_analysis['avg_duration']:.2f} s"),
        ("Speed Proxy", f"{stroke_analysis['speed_proxy']:.2f} rel."),
        ("Peak Phase", f"{stroke_analysis['peak_phase_pct']:.1f}%"),
        ("Smoothness", f"{stroke_analysis['smoothness_jerk_rms']:.2f} jerk RMS"),
    ]

    card_html = "".join(
        f'<div class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        for label, value in cards
    )
    marker_table = make_marker_table(plot_markers)
    reference_section = make_reference_section(reference_analysis)

    summary_rows = []
    for column, summary in summaries.items():
        description = display_description(column)
        label = display_label(column)
        summary_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(label)}</strong><br><span>{html.escape(description)}</span></td>"
            f"<td>{html.escape(column)}</td>"
            f"<td>{summary['min']:.3f}</td>"
            f"<td>{summary['max']:.3f}</td>"
            f"<td>{summary['mean']:.3f}</td>"
            f"<td>{summary['std']:.3f}</td>"
            f"<td>{summary['range']:.3f}</td>"
            "</tr>"
        )

    report = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Rowing Seat Motion Report</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f2;
      color: #202124;
    }}
    body {{
      margin: 0;
      padding: 28px;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
    }}
    h1 {{
      font-size: 28px;
      margin: 0 0 18px;
    }}
    h2 {{
      font-size: 18px;
      margin: 0;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }}
    .metric {{
      border: 1px solid #d9ddd0;
      border-radius: 8px;
      background: #fff;
      padding: 12px;
    }}
    .metric span {{
      display: block;
      color: #62675d;
      font-size: 12px;
      margin-bottom: 6px;
    }}
    .metric strong {{
      display: block;
      font-size: 16px;
      overflow-wrap: anywhere;
    }}
    .plot-section, .table-section {{
      border: 1px solid #d9ddd0;
      border-radius: 8px;
      background: #fff;
      padding: 14px;
      margin-top: 14px;
    }}
    .note {{
      color: #4f554b;
      line-height: 1.45;
      margin: 4px 0 12px;
    }}
    .plot-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    .legend {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      font-size: 13px;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }}
    .swatch {{
      width: 12px;
      height: 12px;
      border-radius: 2px;
      display: inline-block;
    }}
    svg {{
      width: 100%;
      height: auto;
      display: block;
    }}
    .axis {{
      stroke: #40443c;
      stroke-width: 1.2;
    }}
    .grid {{
      stroke: #e2e5dc;
      stroke-width: 1;
    }}
    .zero-grid {{
      stroke: #9aa08f;
      stroke-width: 1.4;
      stroke-dasharray: 5 4;
    }}
    .tick {{
      fill: #62675d;
      font-size: 12px;
    }}
    .marker-label {{
      fill: #202124;
      font-size: 12px;
      font-weight: 650;
    }}
    code {{
      background: #eef0e8;
      border-radius: 4px;
      padding: 2px 4px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #e4e6df;
      padding: 8px;
      text-align: right;
    }}
    td span {{
      color: #62675d;
      font-size: 12px;
    }}
    th:first-child, td:first-child {{
      text-align: left;
    }}
    th {{
      color: #62675d;
      font-weight: 600;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Rowing Seat Motion Report</h1>
    <section class="metrics">{card_html}</section>
    {make_calculation_explanation()}
    {marker_table}
    {reference_section}
    <section class="table-section">
      <h2>Technique metrics</h2>
      <p class="note">
        These values are first-pass estimates from the IMU signal. Stroke count is
        based on smoothed forward-seat acceleration peaks. The speed value is a
        relative velocity proxy from integrated, drift-corrected acceleration, not
        an absolute seat speed yet.
      </p>
      <table>
        <thead>
          <tr><th>Metric</th><th>Value</th><th>Meaning</th></tr>
        </thead>
        <tbody>
          <tr><td>Detected strokes</td><td>{stroke_analysis["stroke_count"]}</td><td>Estimated complete cycles in this recording</td></tr>
          <tr><td>Stroke rate</td><td>{stroke_analysis["stroke_rate"]:.1f} spm</td><td>Strokes per minute over the full file</td></tr>
          <tr><td>Average stroke duration</td><td>{stroke_analysis["avg_duration"]:.2f} s</td><td>Mean time from one detected forward acceleration peak to the next</td></tr>
          <tr><td>Duration range</td><td>{stroke_analysis["min_duration"]:.2f}-{stroke_analysis["max_duration"]:.2f} s</td><td>Consistency of the detected stroke timing</td></tr>
          <tr><td>Relative speed proxy</td><td>{stroke_analysis["speed_proxy"]:.2f}</td><td>Higher means stronger/faster seat motion in this file; not calibrated to m/s yet</td></tr>
          <tr><td>Peak phase</td><td>{stroke_analysis["peak_phase_pct"]:.1f}%</td><td>When the strongest speed proxy occurs in the analyzed recording</td></tr>
          <tr><td>Smoothness</td><td>{stroke_analysis["smoothness_jerk_rms"]:.2f} jerk RMS</td><td>Lower values mean the smoothed acceleration changes less abruptly</td></tr>
        </tbody>
      </table>
    </section>
    {make_series_plot(
        "Seat forward acceleration: raw vs smoothed",
        (
            ("raw seat_forward_acc", stroke_analysis["times"], stroke_analysis["centered_forward"], "#94a3b8"),
            ("smoothed seat_forward_acc", stroke_analysis["times"], stroke_analysis["smoothed_forward"], "#7c2d12"),
        ),
        "m/s^2",
        "The smoothed line is used for the first stroke detection. Peaks in this signal represent strong forward/back seat acceleration events.",
    )}
    {make_series_plot(
        "Average normalized stroke: acceleration and relative speed",
        (
            ("avg forward acceleration", stroke_analysis["avg_phase"], stroke_analysis["avg_acc"], "#7c2d12"),
            ("relative seat speed proxy", stroke_analysis["avg_phase"], stroke_analysis["avg_velocity"], "#0f766e"),
        ),
        "relative",
        "Each detected stroke is stretched to 0-100 percent phase and averaged. This helps show where acceleration and relative speed are strongest within the rowing cycle.",
    )}
    {make_series_plot(
        "Average normalized stroke: rotation rates",
        (
            ("roll rate", stroke_analysis["avg_phase"], stroke_analysis["avg_roll"], "#0f766e"),
            ("pitch rate", stroke_analysis["avg_phase"], stroke_analysis["avg_pitch"], "#b91c1c"),
            ("yaw rate", stroke_analysis["avg_phase"], stroke_analysis["avg_yaw"], "#1d4ed8"),
        ),
        "rad/s",
        "Rotation should ideally stay controlled on the seat. Pitch can indicate body swing or sensor tilt; yaw/roll can indicate twisting or unstable mounting.",
    )}
    {make_phase_explanation()}
    {make_plot("Seat forward/back candidate axis", rows, (strongest_acc,), "m/s^2", ("#7c2d12",), plot_markers)}
    {make_plot("Seat acceleration in rowing coordinates", rows, ACC_COLUMNS, "m/s^2", PLOT_COLORS, plot_markers)}
    {make_plot("Seat rotation rates", rows, GYRO_COLUMNS, "rad/s", PLOT_COLORS, plot_markers)}
    <section class="table-section">
      <h2>Statistik</h2>
      <table>
        <thead>
          <tr>
            <th>Rowing Label</th>
            <th>Raw CSV</th>
            <th>Min</th>
            <th>Max</th>
            <th>Mittel</th>
            <th>Std</th>
            <th>Range</th>
          </tr>
        </thead>
        <tbody>{"".join(summary_rows)}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    path.write_text(report, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze rowing IMU CSV logs from the Feather logger."
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        type=Path,
        default=DEFAULT_CSV_FILE,
        help=f"Path to LOGxxx.CSV, default: {DEFAULT_CSV_FILE}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_FILE,
        help=f"HTML report path, default: {DEFAULT_REPORT_FILE}",
    )
    parser.add_argument(
        "--markers",
        type=Path,
        help="Optional marker CSV. Defaults to <csv_stem>_markers.csv when present.",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        help=(
            "Optional BOAT/reference CSV. Adds SEAT - BOAT relative plots. "
            f"If omitted, uses {DEFAULT_BOAT_CSV_FILE} when that file exists."
        ),
    )
    args = parser.parse_args()

    rows = load_rows(args.csv_file)
    if not rows:
        raise SystemExit("CSV contains no measurements.")

    start_time = rows[0]["time_us"]
    end_time = rows[-1]["time_us"]
    duration_s = format_seconds(end_time - start_time)

    print(f"File: {args.csv_file}")
    print(f"Device: {rows[0]['device_id']}")
    print(f"Measurements: {len(rows)}")
    print(f"Sequence: {rows[0]['sequence']} -> {rows[-1]['sequence']}")
    print(f"Duration: {duration_s:.3f} s")

    gaps = check_sequences(rows)
    if gaps:
        print(f"Sequence gaps: {len(gaps)}")
        for expected, actual in gaps[:10]:
            print(f"  expected {expected}, got {actual}")
    else:
        print("Sequence gaps: none")

    interval_summary = check_sample_intervals(rows)
    if interval_summary:
        print(
            "Sampling: "
            f"mean={interval_summary['mean_us']:.1f} us, "
            f"std={interval_summary['std_us']:.1f} us, "
            f"min={interval_summary['min_us']} us, "
            f"max={interval_summary['max_us']} us, "
            f"rate={interval_summary['rate_hz']:.2f} Hz"
        )

    ax, ay, az, magnitude = estimate_static_tilt(rows)
    print(
        "Initial acceleration mean: "
        f"seat_forward={ax:.3f}, "
        f"seat_lateral={ay:.3f}, "
        f"seat_vertical={az:.3f} m/s^2, "
        f"|a|={magnitude:.3f} m/s^2"
    )

    strongest_acc = print_group_summary("Seat Acceleration", rows, ACC_COLUMNS, "m/s^2")
    print_group_summary("Seat Rotation Rates", rows, GYRO_COLUMNS, "rad/s")

    stroke_analysis = analyze_strokes(rows, strongest_acc)
    marker_file = args.markers or default_marker_file(args.csv_file)
    markers = load_markers(marker_file)
    reference_file = args.reference
    if reference_file is None and DEFAULT_BOAT_CSV_FILE.exists():
        reference_file = DEFAULT_BOAT_CSV_FILE

    reference_analysis = None
    if reference_file:
        reference_rows = load_rows(reference_file)
        reference_analysis = build_reference_analysis(rows, reference_rows, reference_file)

    print()
    print("Stroke Metrics")
    print("--------------")
    print(f"Detected strokes: {stroke_analysis['stroke_count']}")
    print(f"Stroke rate: {stroke_analysis['stroke_rate']:.1f} spm")
    print(f"Average stroke duration: {stroke_analysis['avg_duration']:.2f} s")
    print(f"Relative speed proxy: {stroke_analysis['speed_proxy']:.2f}")
    print(f"Peak phase: {stroke_analysis['peak_phase_pct']:.1f}%")
    print(f"Smoothness: {stroke_analysis['smoothness_jerk_rms']:.2f} jerk RMS")
    print(f"Markers: {len(markers)} from {marker_file if marker_file.exists() else 'none'}")
    if reference_analysis:
        print(
            f"Reference comparison: {reference_analysis['count']} aligned samples "
            f"from {reference_file}"
        )
    else:
        print(f"Reference comparison: none; default file not found at {DEFAULT_BOAT_CSV_FILE}")

    write_html_report(
        args.output,
        args.csv_file,
        rows,
        strongest_acc,
        interval_summary,
        stroke_analysis,
        markers,
        reference_analysis,
    )

    print()
    print("Next interpretation")
    print("-------------------")
    print(
        f"Using the mounting convention X=forward/back, Y=lateral, Z=vertical, "
        f"treat {display_label(strongest_acc)} as the main candidate signal for "
        "seat travel. Open the HTML report to inspect the signal shape over time."
    )
    print("Stroke counting is a first-pass peak estimate and should be validated with a real rowing test.")
    print(f"HTML report: {args.output}")


if __name__ == "__main__":
    main()
