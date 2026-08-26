import json
import tempfile
import unittest
from pathlib import Path

from twtt_data import (
    INFINITE_STREAM,
    TwttClient,
    build_schedule,
    load_gui_config,
    reconstruct_parameters,
)


CAPABILITY = {
    "capability": "measure-twtt",
    "capabilityName": "twtt",
    "endpoint": "/twtt",
    "metadata": {"aliases": ["Two-Way Time Transfer"]},
    "parameters_schema": {"type": "object"},
}

GUI_CONFIG = {
    "version": 1,
    "capability_id": "test-twtt-capability",
    "capability_endpoint": "/twtt",
    "capability_name": "twtt",
    "parameters_flat": {
        "way_1.coincidences_endpoint": "coincidences_analyzer",
        "way_1.channels[0].endpoint": "/timetagger/alice",
        "way_1.channels[0].channel": "1",
        "way_1.channels[1].endpoint": "/timetagger/bob",
        "way_1.channels[1].channel": "2",
        "way_1.peak0": 1790000,
        "way_1.range_ns": 10,
        "way_1.time_bin": 0.1,
        "way_1.time_integration_ms": None,
        "way_2.coincidences_endpoint": "coincidences_analyzer",
        "way_2.channels[0].endpoint": "/timetagger/bob",
        "way_2.channels[0].channel": "1",
        "way_2.channels[1].endpoint": "/timetagger/alice",
        "way_2.channels[1].channel": "2",
        "way_2.peak0": 1746000,
        "way_2.range_ns": 10,
        "way_2.time_bin": 0.1,
        "way_2.time_integration_ms": None,
    },
    "schedule": {
        "start_option": "now",
        "execution_mode": "infinite_stream",
        "options": [],
    },
}


class FakeMeasurement:
    def __init__(self):
        self.valid = False
        self.active = False
        self.configure_arguments = {}

    def configure(self, **arguments):
        self.configure_arguments = arguments
        self.valid = True


class FakeMeasurementPlaneClient:
    def __init__(self, capability_id):
        self.capabilities = {capability_id: CAPABILITY}
        self.measurement = None
        self.connect_calls = 0
        self.close_calls = 0
        self.send_calls = 0
        self.interrupt_calls = 0

    async def connect(self):
        self.connect_calls += 1

    async def close(self):
        self.close_calls += 1

    def get_capabilities(self):
        return self.capabilities

    def create_measurement(self, capability):
        self.measurement = FakeMeasurement()
        return self.measurement

    async def send_measurement(self, measurement):
        self.send_calls += 1
        measurement.active = True

    async def interrupt_measurement(self, measurement):
        self.interrupt_calls += 1
        measurement.active = False
        callback = measurement.configure_arguments.get("completion_callback")
        if callback:
            callback()
        return True


class TwttClientTests(unittest.TestCase):
    def setUp(self):
        self.config_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.config_directory.name) / "twtt_config.json"
        self.config_path.write_text(json.dumps(GUI_CONFIG), encoding="utf-8")
        self.config = load_gui_config(self.config_path)

    def tearDown(self):
        self.config_directory.cleanup()

    def make_client(self, output_directory):
        fake = FakeMeasurementPlaneClient(self.config["capability_id"])
        client = TwttClient(
            self.config_path,
            output_directory=output_directory,
            discovery_timeout_s=0.2,
            mp_client=fake,
        )
        return client, fake

    def test_reconstructs_gui_flat_parameters_and_omits_empty_values(self):
        parameters = reconstruct_parameters(self.config["parameters_flat"])

        self.assertEqual(parameters["way_1"]["peak0"], 1790000)
        self.assertEqual(
            parameters["way_1"]["channels"],
            [
                {"endpoint": "/timetagger/alice", "channel": "1"},
                {"endpoint": "/timetagger/bob", "channel": "2"},
            ],
        )
        self.assertNotIn("time_integration_ms", parameters["way_1"])
        self.assertEqual(parameters["way_2"]["coincidences_endpoint"], "coincidences_analyzer")

    def test_builds_the_same_schedule_as_gui(self):
        self.assertEqual(build_schedule(self.config["schedule"]), "now||")

    def test_redirect_is_not_inserted_into_agent_schedule(self):
        schedule = dict(self.config["schedule"])
        schedule["options"] = ["redirect"]

        self.assertEqual(build_schedule(schedule), "now||")

    def test_non_storage_schedule_option_is_preserved(self):
        schedule = dict(self.config["schedule"])
        schedule["options"] = ["redirect", "5s"]

        self.assertEqual(build_schedule(schedule), "now||5s")

    def test_stream_uses_mpclient_and_writes_gui_compatible_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            client, fake = self.make_client(directory)
            client.start_stream(redirect_to_storage=True)

            arguments = fake.measurement.configure_arguments
            self.assertEqual(arguments["execution_mode"], INFINITE_STREAM)
            self.assertTrue(arguments["stream_results"])
            self.assertTrue(arguments["redirect_to_storage"])
            self.assertEqual(arguments["schedule"], "now||")
            self.assertEqual(arguments["parameters"]["way_2"]["peak0"], 1746000)

            result = {
                "offset_ps": 12.5,
                "time_of_flight_ps": 42.0,
                "ways_synchronized": True,
            }
            arguments["result_callback"]([result])
            self.assertEqual(client.get_result(timeout=0.1), result)

            output_path = client.output_path
            self.assertIsNotNone(output_path)
            records = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[0]["record_type"], "measurement")
            self.assertEqual(records[1]["record_type"], "result")
            self.assertEqual(records[1]["result"], result)

            client.close()
            self.assertEqual(fake.interrupt_calls, 1)
            self.assertEqual(fake.close_calls, 1)

    def test_context_exit_interrupts_active_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            client, fake = self.make_client(directory)
            with client:
                client.start_stream(redirect_to_storage=False)
                self.assertTrue(client.active)

            self.assertEqual(fake.interrupt_calls, 1)
            self.assertFalse(client.active)
