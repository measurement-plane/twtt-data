import unittest

from example_usage import extract_twtt_values


class ExampleUsageTests(unittest.TestCase):
    def test_extracts_combined_per_way_and_offset_values(self):
        values = extract_twtt_values(
            {
                "time_of_flight_ps": 100,
                "way_1_corrected_peak_ps": -120,
                "way_2": {"corrected_peak_ps": 80},
                "offset_ps": -10,
            }
        )

        self.assertEqual(
            values,
            {
                "time_of_flight_ps": 100,
                "way_1_time_of_flight_ps": 120,
                "way_2_time_of_flight_ps": 80,
                "offset_ps": -10,
            },
        )
