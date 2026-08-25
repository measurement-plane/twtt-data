import json
import tempfile
import unittest
from pathlib import Path

from twtt_data import elapsed_time_axis, load_twtt_jsonl, moving_average, summarize


class TwttAnalysisTests(unittest.TestCase):
    def test_loads_corrected_per_way_time_of_flight(self):
        records = [
            {"record_type": "measurement", "timestamp": "2026-01-01T00:00:00Z"},
            {
                "record_type": "result",
                "timestamp": "2026-01-01T00:00:01Z",
                "result": {
                    "offset_ps": 25,
                    "time_of_flight_ps": 125,
                    "way_1_corrected_peak_ps": -100,
                    "way_2_corrected_peak_ps": -150,
                    "way_1": {"coincidence_rate": 10},
                    "way_2": {"coincidence_rate": 12},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            _, samples = load_twtt_jsonl(path)

        self.assertEqual(samples[0]["way_1_time_of_flight_ps"], 100)
        self.assertEqual(samples[0]["way_2_time_of_flight_ps"], 150)
        self.assertEqual(samples[0]["elapsed_seconds"], 0)
        self.assertEqual(samples[0]["elapsed_minutes"], 0)
        self.assertEqual(summarize(samples)["offset_ps"]["mean"], 25)

    def test_elapsed_axis_uses_seconds_for_short_acquisitions(self):
        values, unit = elapsed_time_axis(
            [{"elapsed_seconds": 0}, {"elapsed_seconds": 45}]
        )
        self.assertEqual(values, [0, 45])
        self.assertEqual(unit, "seconds")

    def test_elapsed_axis_uses_minutes_for_minute_acquisitions(self):
        values, unit = elapsed_time_axis(
            [{"elapsed_seconds": 0}, {"elapsed_seconds": 300}]
        )
        self.assertEqual(values, [0, 5])
        self.assertEqual(unit, "minutes")

    def test_elapsed_axis_uses_hours_for_hour_acquisitions(self):
        values, unit = elapsed_time_axis(
            [{"elapsed_seconds": 0}, {"elapsed_seconds": 7200}]
        )
        self.assertEqual(values, [0, 2])
        self.assertEqual(unit, "hours")

    def test_moving_average_uses_complete_windows(self):
        self.assertEqual(moving_average([1, 2, 3, 4, 5], 3), [2, 3, 4])

    def test_moving_average_rejects_insufficient_samples(self):
        with self.assertRaisesRegex(ValueError, "Need at least 100 samples"):
            moving_average([1, 2, 3])
