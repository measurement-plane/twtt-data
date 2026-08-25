# TWTT data tools

This repository contains an importable Measurement Plane TWTT client and
analysis utilities for saved TWTT JSONL acquisitions.

## Repository layout

```text
twtt-data/
├── data/                              # Recorded TWTT JSONL acquisitions
├── config/
│   └── twtt_measurement.example.json # Configuration exported by the GUI
├── example_usage.py                   # Executable one-shot/stream example
├── notebooks/
│   └── twtt_analysis.ipynb            # Interactive analysis and plots
├── src/twtt_data/
│   ├── __init__.py                    # Public imports
│   ├── client.py                      # Reusable Measurement Plane interface
│   └── analysis.py                    # Loading, summaries, and plotting
├── tests/
├── pyproject.toml
└── requirements.txt
```

The reusable package contains no CLI or `main()` function. The executable
`example_usage.py` program lives at the repository root, outside the package.
The `data/` and `config/` directories are intentionally ignored by Git so
measurement data and local hardware configuration are never published.

## Installation

Create an environment and install the package with its notebook dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows Git Bash:

```bash
source .venv/Scripts/activate
```

For development with the sibling Measurement Plane repository, install that
checkout first and then install this package without resolving dependencies
again:

```bash
python -m pip install -e ../measurement_plane
python -m pip install -e ".[analysis]" --no-deps
```

## Use the client from another script

The client accepts the JSON produced by the GUI's **Save Config** button. It
reconstructs `parameters_flat`, finds the saved TWTT capability, and delegates
measurement creation, sending, result handling, and interruption to the
official `MeasurementPlaneClient`.

### One-shot

```python
from twtt_data import run_twtt_once

result = run_twtt_once(
    "config/twtt_measurement.example.json",
    broker_url="nats://10.10.10.200:4222",
    redirect_to_storage=False,
    timeout=180,
)
print(result["offset_ps"], result["time_of_flight_ps"])
```

### Stream

```python
from twtt_data import TwttClient

with TwttClient(
    "config/twtt_measurement.example.json",
    broker_url="nats://10.10.10.200:4222",
) as client:
    client.start_stream(redirect_to_storage=True)
    while client.active:
        result = client.get_result(timeout=30)
        print(result["offset_ps"], result["time_of_flight_ps"])
```

You can also start a stream for a different script to manage:

```python
from twtt_data import start_twtt_measurement

client = start_twtt_measurement(
    "config/twtt_measurement.example.json",
    stream=True,
    redirect_to_storage=True,
)

try:
    result = client.get_result(timeout=30)
finally:
    client.close()
```

`client.stop()` and its alias `client.interrupt()` call
`MeasurementPlaneClient.interrupt_measurement()`. `close()` interrupts an
active measurement before disconnecting. Context-manager exit and normal
interpreter shutdown also call `close()`. A force-kill or power loss cannot run
cleanup, so the context-manager form is preferred.

Passing `redirect_to_storage=None` follows the saved GUI `"redirect"` option.
Passing `True` or `False` overrides it. Enabled storage creates timestamped
JSONL files under `twtt-results/`, using the format accepted by the analysis
utilities.

## Run the complete example

The root-level example prints combined time of flight, time of flight for each
way, and offset. One-shot mode prints one result:

First create `config/` and place a GUI **Save Config** export at:

```text
config/twtt_measurement.example.json
```

```bash
python example_usage.py one-shot
```

Continuous mode prints those four values every time a new result arrives:

```bash
python example_usage.py continuous
```

Press `Ctrl+C` to leave continuous mode. Its `finally` block calls
`client.stop()`, and context-manager exit calls `client.close()`, ensuring the
official Measurement Plane interrupt API is used before disconnecting.

## Analyze recorded data

Recorded acquisitions live in `data/`. Start the notebook with:

```bash
jupyter lab notebooks/twtt_analysis.ipynb
```

The notebook selects the latest `data/twtt_*.jsonl` file. It plots:

1. clock offset;
2. combined two-way time of flight;
3. time of flight for way 1 and way 2;
4. coincidence rates for way 1 and way 2.

It also produces a second four-panel figure using a 100-sample moving average.
The elapsed-time axis automatically uses seconds, minutes, or hours according
to the acquisition duration.

Per-way time of flight is calculated as:

```text
corrected_peak_ps = histogram_peak_position_ps + configured_peak0_ps
way_time_of_flight_ps = abs(corrected_peak_ps)
```

## Tests

```bash
python -m unittest discover -s tests -v
```
