"""Utilities for loading and plotting Measurement Plane TWTT JSONL results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Iterable


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _corrected_peak(result: dict[str, Any], way_name: str) -> float:
    """Return the peak after undoing the configured coincidence shift."""
    top_level = result.get(f"{way_name}_corrected_peak_ps")
    if top_level is not None:
        return float(top_level)

    way = result[way_name]
    nested = way.get("corrected_peak_ps")
    if nested is not None:
        return float(nested)

    peak = way.get("peak_position_ps", result[f"{way_name}_peak_position_ps"])
    peak0 = way.get("configured_peak0_ps", result.get(f"{way_name}_peak0_ps", 0))
    return float(peak) + float(peak0)


def load_twtt_jsonl(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load one GUI-exported TWTT JSONL file into compact analysis rows.

    Per-way time of flight is the magnitude of the corrected histogram peak:

        abs(histogram_peak_position_ps + configured_peak0_ps)
    """
    metadata: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []

    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("record_type") == "measurement":
                metadata = record
                continue
            if record.get("record_type") != "result":
                continue

            result = record["result"]
            way_1 = result["way_1"]
            way_2 = result["way_2"]
            timestamp = _parse_timestamp(record["timestamp"])
            samples.append(
                {
                    "timestamp": timestamp,
                    "offset_ps": float(result["offset_ps"]),
                    "time_of_flight_ps": float(result["time_of_flight_ps"]),
                    "way_1_time_of_flight_ps": abs(_corrected_peak(result, "way_1")),
                    "way_2_time_of_flight_ps": abs(_corrected_peak(result, "way_2")),
                    "way_1_coincidence_rate_cps": float(way_1["coincidence_rate"]),
                    "way_2_coincidence_rate_cps": float(way_2["coincidence_rate"]),
                }
            )

    if not samples:
        raise ValueError(f"No TWTT result records found in {path}")

    start = samples[0]["timestamp"]
    for sample in samples:
        elapsed_seconds = (sample["timestamp"] - start).total_seconds()
        sample["elapsed_seconds"] = elapsed_seconds
        # Retained for compatibility with existing analysis code.
        sample["elapsed_minutes"] = elapsed_seconds / 60
    return metadata, samples


def elapsed_time_axis(
    samples: Iterable[dict[str, Any]],
) -> tuple[list[float], str]:
    """Scale elapsed time to seconds, minutes, or hours for readable plots."""
    rows = list(samples)
    elapsed_seconds = [
        float(row["elapsed_seconds"])
        if "elapsed_seconds" in row
        else float(row["elapsed_minutes"]) * 60
        for row in rows
    ]
    duration_seconds = max(elapsed_seconds, default=0.0)

    if duration_seconds >= 3600:
        divisor, unit = 3600, "hours"
    elif duration_seconds >= 60:
        divisor, unit = 60, "minutes"
    else:
        divisor, unit = 1, "seconds"

    return [value / divisor for value in elapsed_seconds], unit


def summarize(samples: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Return count, mean, standard deviation, minimum, and maximum."""
    rows = list(samples)
    fields = (
        "offset_ps",
        "time_of_flight_ps",
        "way_1_time_of_flight_ps",
        "way_2_time_of_flight_ps",
        "way_1_coincidence_rate_cps",
        "way_2_coincidence_rate_cps",
    )
    output: dict[str, dict[str, float]] = {}
    for field in fields:
        values = [float(row[field]) for row in rows]
        output[field] = {
            "count": len(values),
            "mean": fmean(values),
            "stdev": stdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
        }
    return output


def moving_average(values: Iterable[float], window_size: int = 100) -> list[float]:
    """Return a moving average containing only complete windows."""
    if window_size < 1:
        raise ValueError("window_size must be at least 1")

    numbers = [float(value) for value in values]
    if len(numbers) < window_size:
        raise ValueError(
            f"Need at least {window_size} samples for the moving average; "
            f"got {len(numbers)}"
        )

    output: list[float] = []
    window_sum = sum(numbers[:window_size])
    output.append(window_sum / window_size)
    for index in range(window_size, len(numbers)):
        window_sum += numbers[index] - numbers[index - window_size]
        output.append(window_sum / window_size)
    return output


def _plot_twtt_panels(
    rows: list[dict[str, Any]],
    elapsed: list[float],
    elapsed_unit: str,
    *,
    linewidth: float,
):
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True, constrained_layout=True)

    axes[0, 0].plot(elapsed, [row["offset_ps"] for row in rows], linewidth=linewidth)
    axes[0, 0].set_title("Offset")
    axes[0, 0].set_ylabel("Offset (ps)")

    axes[0, 1].plot(
        elapsed,
        [row["time_of_flight_ps"] for row in rows],
        linewidth=linewidth,
    )
    axes[0, 1].set_title("Combined time of flight")
    axes[0, 1].set_ylabel("Time of flight (ps)")

    axes[1, 0].plot(
        elapsed,
        [row["way_1_time_of_flight_ps"] for row in rows],
        linewidth=linewidth,
        label="Way 1",
    )
    axes[1, 0].plot(
        elapsed,
        [row["way_2_time_of_flight_ps"] for row in rows],
        linewidth=linewidth,
        label="Way 2",
    )
    axes[1, 0].set_title("Time of flight per way")
    axes[1, 0].set_ylabel("Time of flight (ps)")
    axes[1, 0].legend()

    axes[1, 1].plot(
        elapsed,
        [row["way_1_coincidence_rate_cps"] for row in rows],
        linewidth=linewidth,
        label="Way 1",
    )
    axes[1, 1].plot(
        elapsed,
        [row["way_2_coincidence_rate_cps"] for row in rows],
        linewidth=linewidth,
        label="Way 2",
    )
    axes[1, 1].set_title("Coincidence rates")
    axes[1, 1].set_ylabel("Rate (counts/s)")
    axes[1, 1].legend()

    for axis in axes.flat:
        axis.grid(True, alpha=0.25)
        axis.set_xlabel(f"Elapsed time ({elapsed_unit})")
    return figure, axes


def plot_twtt(samples: Iterable[dict[str, Any]]):
    """Create the four raw TWTT time-series panels."""
    rows = list(samples)
    elapsed, elapsed_unit = elapsed_time_axis(rows)
    return _plot_twtt_panels(rows, elapsed, elapsed_unit, linewidth=0.8)


def plot_twtt_smoothed(
    samples: Iterable[dict[str, Any]], window_size: int = 100
):
    """Create four TWTT panels smoothed with a moving-average window."""
    rows = list(samples)
    elapsed, elapsed_unit = elapsed_time_axis(rows)
    fields = (
        "offset_ps",
        "time_of_flight_ps",
        "way_1_time_of_flight_ps",
        "way_2_time_of_flight_ps",
        "way_1_coincidence_rate_cps",
        "way_2_coincidence_rate_cps",
    )
    averaged = {
        field: moving_average((row[field] for row in rows), window_size)
        for field in fields
    }
    averaged_elapsed = moving_average(elapsed, window_size)
    averaged_rows = [
        {field: averaged[field][index] for field in fields}
        for index in range(len(averaged_elapsed))
    ]
    return _plot_twtt_panels(
        averaged_rows,
        averaged_elapsed,
        elapsed_unit,
        linewidth=1.2,
    )
