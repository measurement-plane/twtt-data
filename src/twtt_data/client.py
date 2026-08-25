"""Thin, synchronous interface to the official Measurement Plane client."""

from __future__ import annotations

import atexit
import asyncio
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ONE_SHOT = "one_shot"
FINITE_STREAM = "finite_stream"
INFINITE_STREAM = "infinite_stream"
STREAM_MODES = {FINITE_STREAM, INFINITE_STREAM}
_END_OF_RESULTS = object()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_gui_config(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a configuration exported by the GUI."""
    config_path = Path(path).expanduser().resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GUI configuration must be a JSON object")
    if not isinstance(payload.get("parameters_flat"), dict):
        raise ValueError("GUI configuration is missing 'parameters_flat'")
    if not isinstance(payload.get("schedule"), dict):
        raise ValueError("GUI configuration is missing 'schedule'")
    if not payload.get("capability_endpoint"):
        raise ValueError("GUI configuration is missing 'capability_endpoint'")
    return payload


def reconstruct_parameters(parameters_flat: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild nested measurement parameters from the GUI's flattened fields."""
    parameters: dict[str, Any] = {}
    array_pattern = re.compile(r"^(?P<base>.+)\[(?P<index>\d+)\]$")

    for flat_key, value in parameters_flat.items():
        # This is the same omission performed by the GUI form reconstruction.
        if value is None or value == "" or not flat_key:
            continue

        current: Any = parameters
        keys = str(flat_key).split(".")
        for position, key in enumerate(keys):
            array_match = array_pattern.match(key)
            if array_match:
                base = array_match.group("base")
                array_index = int(array_match.group("index"))
                current.setdefault(base, [])
                while len(current[base]) <= array_index:
                    current[base].append({})
                if position == len(keys) - 1:
                    current[base][array_index] = value
                else:
                    current = current[base][array_index]
            elif position == len(keys) - 1:
                current[key] = value
            else:
                current.setdefault(key, {})
                current = current[key]
    return parameters


def build_schedule(schedule_config: Mapping[str, Any]) -> str:
    """Build the schedule string exactly as the GUI does before configure()."""
    if schedule_config.get("start_option", "now") == "now":
        start = "now"
    else:
        start_date = schedule_config.get("start_date")
        if not start_date:
            raise ValueError("Custom start selected but 'start_date' is missing")
        start = (
            f"{start_date} "
            f"{schedule_config.get('start_hour') or '00'}:"
            f"{schedule_config.get('start_minute') or '00'}:"
            f"{schedule_config.get('start_second') or '00'}"
        )

    end_date = schedule_config.get("end_date")
    end = ""
    if end_date:
        end = (
            f"{end_date} "
            f"{schedule_config.get('end_hour') or '00'}:"
            f"{schedule_config.get('end_minute') or '00'}:"
            f"{schedule_config.get('end_second') or '00'}"
        )
    options = schedule_config.get("options") or []
    return f"{start}|{end}|{'|'.join(options)}"


class _GuiJsonlWriter:
    """Write the same JSONL record shape consumed by twtt_analysis.py."""

    def __init__(self, directory: Path, specification: Mapping[str, Any]):
        directory.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(timezone.utc)
        stem = f"twtt_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
        self.path = directory / f"{stem}.jsonl"
        suffix = 1
        while self.path.exists():
            self.path = directory / f"{stem}_{suffix}.jsonl"
            suffix += 1
        self._lock = threading.Lock()
        metadata = {
            "record_type": "measurement",
            "timestamp": _utc_timestamp(),
            "capability": "twtt",
            "specification": dict(specification),
        }
        self._append(metadata, create=True)

    def _append(self, record: Mapping[str, Any], *, create: bool = False) -> None:
        mode = "x" if create else "a"
        with self._lock, self.path.open(mode, encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")

    def append_result(self, result: Mapping[str, Any]) -> None:
        self._append(
            {
                "record_type": "result",
                "timestamp": _utc_timestamp(),
                "result": result,
            }
        )


class TwttClient:
    """Load a GUI config and control one TWTT measurement through MPClient.

    This class runs the asynchronous Measurement Plane API on a private
    background event loop, giving instrument-control scripts a simple
    synchronous interface. Use it as a context manager whenever possible.
    ``close()`` and normal interpreter shutdown both interrupt an active
    measurement before disconnecting.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        broker_url: str | None = None,
        output_directory: str | Path | None = None,
        discovery_timeout_s: float = 20,
        operation_timeout_s: float = 15,
        mp_client: Any | None = None,
    ):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = load_gui_config(self.config_path)
        self.broker_url = (
            broker_url
            or os.getenv("BROKER_URL")
            or "nats://10.10.10.200:4222"
        )
        configured_output = output_directory or "twtt-results"
        output_path = Path(configured_output).expanduser()
        if not output_path.is_absolute():
            output_path = self.config_path.parent / output_path
        self.output_directory = output_path.resolve()
        self.discovery_timeout_s = float(discovery_timeout_s)
        self.operation_timeout_s = float(operation_timeout_s)
        self.mp_client = mp_client

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._measurement = None
        self._result_queue: queue.Queue[Any] = queue.Queue()
        self._writer: _GuiJsonlWriter | None = None
        self._external_result_callback: Callable[[dict[str, Any]], Any] | None = None
        self._connected = False
        self._closed = False
        self._atexit_registered = True
        atexit.register(self.close)

    @property
    def active(self) -> bool:
        return bool(self._measurement and getattr(self._measurement, "active", False))

    @property
    def output_path(self) -> Path | None:
        return self._writer.path if self._writer else None

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coroutine, timeout: float | None = None):
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("TWTT client event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout or self.operation_timeout_s)

    def connect(self) -> "TwttClient":
        if self._connected:
            return self
        if self._closed:
            raise RuntimeError("A closed TWTT client cannot be reconnected")

        if self.mp_client is None:
            try:
                from measurement_plane.measurement_plane_client.mp_client import (
                    MeasurementPlaneClient,
                )
            except ImportError as error:
                raise RuntimeError(
                    "The measurement_plane library is required. Install the sibling "
                    "repository with 'python -m pip install -e ../measurement_plane'."
                ) from error
            self.mp_client = MeasurementPlaneClient(self.broker_url)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="twtt-measurement-plane", daemon=True
        )
        self._thread.start()
        self._submit(self.mp_client.connect())
        self._connected = True
        self._wait_for_capability()
        return self

    def _find_capability(self) -> dict[str, Any] | None:
        capabilities = self.mp_client.get_capabilities() or {}
        saved_id = self.config.get("capability_id")
        if saved_id in capabilities:
            candidate = capabilities[saved_id]
            if candidate.get("capability") == "measure-twtt":
                return candidate

        endpoint = self.config.get("capability_endpoint")
        capability_name = self.config.get("capability_name")
        for candidate in capabilities.values():
            if candidate.get("capability") != "measure-twtt":
                continue
            if candidate.get("endpoint") != endpoint:
                continue
            aliases = ((candidate.get("metadata") or {}).get("aliases") or [])
            if candidate.get("capabilityName") == capability_name or capability_name in aliases:
                return candidate
        return None

    def _wait_for_capability(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.discovery_timeout_s
        while time.monotonic() < deadline:
            capability = self._find_capability()
            if capability is not None:
                return capability
            time.sleep(0.1)
        raise TimeoutError(
            "TWTT capability was not advertised at "
            f"{self.config.get('capability_endpoint')} within "
            f"{self.discovery_timeout_s:g} seconds"
        )

    def _on_result(self, results: Any) -> None:
        result = results[0] if isinstance(results, list) and results else results
        if not isinstance(result, dict):
            return
        if self._writer is not None:
            self._writer.append_result(result)
        self._result_queue.put(result)
        if self._external_result_callback is not None:
            try:
                callback_result = self._external_result_callback(result)
                if asyncio.iscoroutine(callback_result) and self._loop is not None:
                    asyncio.run_coroutine_threadsafe(callback_result, self._loop)
            except Exception:
                # A user callback must not break MPClient's result handler.
                import logging

                logging.exception("TWTT result callback failed")

    def _on_completion(self) -> None:
        self._result_queue.put(_END_OF_RESULTS)

    def start(
        self,
        *,
        execution_mode: str | None = None,
        redirect_to_storage: bool | None = None,
        result_callback: Callable[[dict[str, Any]], Any] | None = None,
    ):
        """Start a measurement using the saved GUI configuration."""
        self.connect()
        if self.active:
            raise RuntimeError("A TWTT measurement is already active")

        capability = self._wait_for_capability()
        schedule_config = self.config["schedule"]
        mode = execution_mode or schedule_config.get("execution_mode") or ONE_SHOT
        if mode not in {ONE_SHOT, FINITE_STREAM, INFINITE_STREAM}:
            raise ValueError(f"Unsupported execution mode: {mode}")
        options = schedule_config.get("options") or []
        store_results = (
            "redirect" in options
            if redirect_to_storage is None
            else bool(redirect_to_storage)
        )
        schedule = build_schedule(schedule_config)
        parameters = reconstruct_parameters(self.config["parameters_flat"])

        self._result_queue = queue.Queue()
        self._external_result_callback = result_callback
        self._writer = (
            _GuiJsonlWriter(
                self.output_directory,
                {
                    "endpoint": capability.get("endpoint"),
                    "schedule": schedule,
                    "execution_mode": mode,
                    "parameters": parameters,
                },
            )
            if store_results
            else None
        )

        measurement = self.mp_client.create_measurement(capability)
        measurement.configure(
            schedule=schedule,
            parameters=parameters,
            result_callback=self._on_result,
            stream_results=mode in STREAM_MODES,
            redirect_to_storage=store_results,
            completion_callback=self._on_completion,
            execution_mode=mode,
        )
        if not measurement.valid:
            self._writer = None
            raise ValueError("TWTT parameters failed the advertised capability schema")

        self._measurement = measurement
        self._submit(
            self.mp_client.send_measurement(measurement),
            timeout=self.operation_timeout_s,
        )
        if not measurement.active:
            self._measurement = None
            raise RuntimeError("TWTT specification was not accepted by the agent")
        return measurement

    def start_once(
        self,
        *,
        redirect_to_storage: bool | None = None,
        result_callback: Callable[[dict[str, Any]], Any] | None = None,
    ):
        return self.start(
            execution_mode=ONE_SHOT,
            redirect_to_storage=redirect_to_storage,
            result_callback=result_callback,
        )

    def start_stream(
        self,
        *,
        redirect_to_storage: bool | None = None,
        result_callback: Callable[[dict[str, Any]], Any] | None = None,
    ):
        return self.start(
            execution_mode=INFINITE_STREAM,
            redirect_to_storage=redirect_to_storage,
            result_callback=result_callback,
        )

    def get_result(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next result, blocking until it arrives or timeout expires."""
        item = self._result_queue.get(timeout=timeout)
        if item is _END_OF_RESULTS:
            raise StopIteration("TWTT measurement has completed")
        return item

    def measure_once(
        self,
        *,
        redirect_to_storage: bool | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Start a one-shot measurement and block until its result arrives."""
        self.start_once(redirect_to_storage=redirect_to_storage)
        return self.get_result(timeout=timeout)

    def stop(self) -> bool:
        """Interrupt the active measurement through MeasurementPlaneClient."""
        measurement = self._measurement
        if measurement is None:
            return True
        if not getattr(measurement, "active", False):
            self._measurement = None
            return True
        confirmed = bool(
            self._submit(
                self.mp_client.interrupt_measurement(measurement),
                timeout=self.operation_timeout_s,
            )
        )
        if confirmed or not getattr(measurement, "active", False):
            self._measurement = None
        return confirmed

    interrupt = stop

    def close(self) -> None:
        """Interrupt any active measurement, disconnect, and stop the loop."""
        if self._closed:
            return
        try:
            if self._connected:
                try:
                    self.stop()
                finally:
                    self._submit(self.mp_client.close())
        finally:
            self._connected = False
            self._measurement = None
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2)
            if self._loop is not None and not self._loop.is_running():
                self._loop.close()
            self._loop = None
            self._thread = None
            self._closed = True
            if self._atexit_registered:
                atexit.unregister(self.close)
                self._atexit_registered = False

    def __enter__(self) -> "TwttClient":
        return self.connect()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def start_twtt_measurement(
    config_path: str | Path,
    *,
    broker_url: str | None = None,
    stream: bool | None = None,
    redirect_to_storage: bool | None = None,
    result_callback: Callable[[dict[str, Any]], Any] | None = None,
    output_directory: str | Path | None = None,
) -> TwttClient:
    """Create a client and start a measurement; call ``stop()``/``close()`` later."""
    client = TwttClient(
        config_path,
        broker_url=broker_url,
        output_directory=output_directory,
    )
    try:
        if stream is True:
            client.start_stream(
                redirect_to_storage=redirect_to_storage,
                result_callback=result_callback,
            )
        elif stream is False:
            client.start_once(
                redirect_to_storage=redirect_to_storage,
                result_callback=result_callback,
            )
        else:
            client.start(
                redirect_to_storage=redirect_to_storage,
                result_callback=result_callback,
            )
        return client
    except BaseException:
        client.close()
        raise


def run_twtt_once(
    config_path: str | Path,
    *,
    broker_url: str | None = None,
    redirect_to_storage: bool | None = None,
    output_directory: str | Path | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run one TWTT measurement with guaranteed cleanup."""
    with TwttClient(
        config_path,
        broker_url=broker_url,
        output_directory=output_directory,
    ) as client:
        return client.measure_once(
            redirect_to_storage=redirect_to_storage,
            timeout=timeout,
        )
