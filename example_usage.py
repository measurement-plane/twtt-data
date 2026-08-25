"""Executable examples for one-shot and continuous TWTT measurements."""

from __future__ import annotations

import argparse
import queue
import time
from pathlib import Path

from twtt_data import TwttClient


CONFIG_PATH = Path(__file__).parent / "config" / "twtt_measurement.example.json"
BROKER_URL = "nats://10.10.10.200:4222"
REDIRECT_TO_STORAGE = False
ONE_SHOT_TIMEOUT_S = 180


def _corrected_peak(result: dict, way_name: str) -> float:
    """Read a way's corrected peak from current or compatible result layouts."""
    corrected_peak = result.get(f"{way_name}_corrected_peak_ps")
    if corrected_peak is None:
        way_result = result.get(way_name) or {}
        corrected_peak = way_result.get("corrected_peak_ps")
    if corrected_peak is None:
        raise KeyError(f"Result does not contain the corrected peak for {way_name}")
    return float(corrected_peak)


def extract_twtt_values(result: dict) -> dict[str, float]:
    """Extract the values normally displayed by the TWTT GUI."""
    return {
        "time_of_flight_ps": float(result["time_of_flight_ps"]),
        "way_1_time_of_flight_ps": abs(_corrected_peak(result, "way_1")),
        "way_2_time_of_flight_ps": abs(_corrected_peak(result, "way_2")),
        "offset_ps": float(result["offset_ps"]),
    }


def print_twtt_values(result: dict) -> None:
    values = extract_twtt_values(result)
    print(
        " | ".join(
            (
                f"time_of_flight={values['time_of_flight_ps']:.3f} ps",
                f"way_1_time_of_flight={values['way_1_time_of_flight_ps']:.3f} ps",
                f"way_2_time_of_flight={values['way_2_time_of_flight_ps']:.3f} ps",
                f"offset={values['offset_ps']:.3f} ps",
            )
        ),
        flush=True,
    )


def wait_for_result(client: TwttClient, timeout_s: float | None = None) -> dict:
    """Wait in short intervals so Ctrl+C remains responsive on Windows."""
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"No TWTT result received within {timeout_s:g} seconds")
        try:
            return client.get_result(timeout=1)
        except queue.Empty:
            continue


def run_one_shot() -> None:
    """Start one measurement, receive one result, and print its TWTT values."""
    with TwttClient(CONFIG_PATH, broker_url=BROKER_URL) as client:
        try:
            client.start_once(redirect_to_storage=REDIRECT_TO_STORAGE)
            result = wait_for_result(client, timeout_s=ONE_SHOT_TIMEOUT_S)
            print_twtt_values(result)
        finally:
            # Safe when the one-shot has already completed; otherwise interrupts it.
            client.stop()


def run_continuous() -> None:
    """Print every streaming result until Ctrl+C interrupts the measurement."""
    with TwttClient(CONFIG_PATH, broker_url=BROKER_URL) as client:
        try:
            client.start_stream(redirect_to_storage=REDIRECT_TO_STORAGE)
            print("TWTT stream started. Press Ctrl+C to stop.")
            while True:
                result = wait_for_result(client)
                print_twtt_values(result)
        except KeyboardInterrupt:
            print("\nStopping the active TWTT measurement...")
        finally:
            # Uses MeasurementPlaneClient.interrupt_measurement().
            client.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="TWTT client usage example")
    parser.add_argument("mode", choices=("one-shot", "continuous"))
    arguments = parser.parse_args()

    if arguments.mode == "one-shot":
        run_one_shot()
    else:
        run_continuous()


if __name__ == "__main__":
    main()
