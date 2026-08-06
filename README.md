# powerbi_360

Automates selecting a serial number in the "Serial Number" slicer of a Power BI
report and scrapes fields (Part Number, Status, warranty/support dates, etc.)
from the resulting table, saving the results to CSV/text files.

## Prerequisites

This is a Python script — Python 3.9+ must be installed first.

- **Windows**: download the installer from [python.org/downloads](https://www.python.org/downloads/)
  (check "Add python.exe to PATH" during install), or `winget install Python.Python.3`.
- **macOS**: `brew install python3`, or download from [python.org/downloads](https://www.python.org/downloads/).
- **Linux**: usually preinstalled; otherwise use your package manager, e.g.
  `sudo apt install python3 python3-venv` (Debian/Ubuntu).

Verify it's installed and on PATH:

```bash
python --version    # or python3 --version
```

## Download

Download the project as a zip file and extract it:

[https://github.com/natke889/powerbi_360/archive/refs/tags/v1.zip](https://github.com/natke889/powerbi_360/archive/refs/tags/v1.zip)

Then open a terminal in the extracted `powerbi_360-1` folder for the steps below.

## Setup

Create a virtual environment and install dependencies, then have Playwright
download Microsoft Edge (used to drive the browser).

### Windows

PowerShell / cmd:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install msedge
```

git-bash:

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
playwright install msedge
```

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install msedge
```

### Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install msedge
playwright install-deps  # installs OS packages Playwright's browsers need
```

All commands below (`python powerbi_360.py ...`) are the same on every OS once
the virtual environment is activated — just use `python3` instead of `python`
on macOS/Linux if `python` isn't mapped to Python 3.

## Configuration

The Power BI report URL lives in `config.ini`:

```ini
[powerbi]
url = https://app.powerbi.com/...
```

Edit it to point at a different report. Pass `--url` to override it for a
single run without touching the file. If `config.ini` is missing, a built-in
default URL is used.

## First-time login

```bash
python powerbi_360.py --login
```

A visible browser window opens — complete the login, then press Enter in the
terminal. The session is saved to `~/.config/powerbi-profile` and reused by
later runs. Re-run this whenever the saved session expires.

## Single serial

```bash
python powerbi_360.py --serials 792349000111
```

Runs headless by default (no visible window). Add `--no-headless` to watch it
run in a visible browser window.

## Bulk mode

Process multiple serials in one browser session, writing one row per serial:

```bash
# comma-separated list
python powerbi_360.py --serials 792349000111,951946000252

# or from a file (one serial per line; blank lines / lines starting with '#' ignored)
python powerbi_360.py --serials-file serials.txt
```

`--serials-file` overrides `--serials` if both are given.

## Options

| Option | Description |
|---|---|
| `--login` | Force an interactive login (use when the saved session has expired) |
| `--headless` | Run without a visible browser window (default) |
| `--no-headless` | Run with a visible browser window |
| `--serials NUMBER` | Serial number(s) to select, comma-separated for bulk mode (default: `792349000111`) |
| `--serials-file FILE` | Text file with one serial per line to process in bulk (overrides `--serials`) |
| `--field NAME` | Column/field name(s) to scrape, space-separated for multiple (default: Part Number, Status, Hw End Of Support Date, Hw Or Sw Service Or Warranty End Date, Hardware Service End Date, Software Service End Date) |
| `--verbose` | Show detailed step-by-step progress logs (default: minimal, one line per serial with success/failure) |
| `--url URL` | Power BI report URL (default: read from `config.ini`'s `[powerbi] url`, falling back to the built-in 360 report) |
| `--timeout MS` | Navigation/wait timeout in milliseconds (default: `60000`) |
| `--out FILE` | CSV file to write results to (default: `data/results.csv`, overwritten each run; use `''` to disable) |
| `--out-text FILE` | Text file to write results to (default: `data/results.txt`, overwritten each run; use `''` to disable) |

## Output

Each run backs up the existing `data/` folder to `backup/data_<timestamp>/`
before overwriting `data/results.csv` and `data/results.txt` with the new
results (one header + one row per serial processed).
