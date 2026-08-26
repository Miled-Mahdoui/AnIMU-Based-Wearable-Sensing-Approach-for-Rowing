#!/usr/bin/env python3
"""Add a shared synthetic boat-motion component to SEAT and BOAT CSV files.

The generated component is intentionally non-negative. It is useful for
demonstrating how a common boat-motion signal appears in both IMUs and should
mostly cancel in a SEAT-BOAT comparison.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


DEFAULT_AXIS = "acc_y_ms2"
DEFAULT_AMPLITUDE_MS2 = 1.2
DEFAULT_OFFSET_MS2 = 1.2
DEFAULT_PERIOD_S = 3.0


def synthetic_boat_motion_acceleration(
    time_s: float,
    *,
    amplitude_ms2: float = DEFAULT_AMPLITUDE_MS2,
    offset_ms2: float = DEFAULT_OFFSET_MS2,
    period_s: float = DEFAULT_PERIOD_S,
) -> float:
    """Return a non-negative sinusoidal acceleration component.

    The curve is:

        a_boat_synthetic(t) = B + A/2 * (1 - cos(2*pi*t/T))

    With the default values it ranges from 1.2 to 2.4 m/s^2.
    """

    if period_s <= 0:
        raise ValueError("period_s must be positive")
    return offset_ms2 + 0.5 * amplitude_ms2 * (
        1.0 - math.cos(2.0 * math.pi * time_s / period_s)
    )


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def add_motion_to_file(
    input_path: Path,
    output_path: Path,
    curve_output_path: Path,
    *,
    axis: str,
    amplitude_ms2: float,
    offset_ms2: float,
    period_s: float,
) -> None:
    fieldnames, rows = read_rows(input_path)
    if axis not in fieldnames:
        raise ValueError(f"{input_path} does not contain axis column {axis!r}")
    if "time_us" not in fieldnames:
        raise ValueError(f"{input_path} does not contain time_us")
    if not rows:
        raise ValueError(f"{input_path} is empty")

    start_us = float(rows[0]["time_us"])
    curve_rows: list[dict[str, str]] = []
    for row in rows:
        time_s = (float(row["time_us"]) - start_us) / 1_000_000.0
        synthetic = synthetic_boat_motion_acceleration(
            time_s,
            amplitude_ms2=amplitude_ms2,
            offset_ms2=offset_ms2,
            period_s=period_s,
        )
        row[axis] = f"{float(row[axis]) + synthetic:.6f}"
        curve_rows.append(
            {
                "time_s": f"{time_s:.6f}",
                f"synthetic_{axis}": f"{synthetic:.6f}",
            }
        )

    write_rows(output_path, fieldnames, rows)
    write_rows(curve_output_path, ["time_s", f"synthetic_{axis}"], curve_rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add a shared non-negative synthetic boat-motion sine curve to an IMU CSV.",
    )
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("curve_csv", type=Path)
    parser.add_argument("--axis", default=DEFAULT_AXIS)
    parser.add_argument("--amplitude-ms2", type=float, default=DEFAULT_AMPLITUDE_MS2)
    parser.add_argument("--offset-ms2", type=float, default=DEFAULT_OFFSET_MS2)
    parser.add_argument("--period-s", type=float, default=DEFAULT_PERIOD_S)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    add_motion_to_file(
        args.input_csv,
        args.output_csv,
        args.curve_csv,
        axis=args.axis,
        amplitude_ms2=args.amplitude_ms2,
        offset_ms2=args.offset_ms2,
        period_s=args.period_s,
    )
    print(f"Wrote {args.output_csv}")
    print(f"Wrote {args.curve_csv}")


if __name__ == "__main__":
    main()
