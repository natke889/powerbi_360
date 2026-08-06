"""
powerbi_360.py
--------------
Selects a serial number in the SERIAL NUMBER slicer of a Power BI report and
scrapes the resulting table row (Part Number, Status, warranty/support dates,
etc.), plus a computed Alert column, to CSV/text output files.

Usage
-----
First run  : python powerbi_360.py --login
             A browser window opens; complete the login, then press Enter.
             The session is saved to ~/.config/powerbi-profile and reused
             afterwards.

Later runs : python powerbi_360.py --serials 792349000111
             The saved session is used; the browser runs headless by
             default — pass --no-headless to see it.

Bulk mode  : python powerbi_360.py --serials 792349000111,951946000252
             Comma-separated serials are processed one after another in a
             single browser session, writing one row per serial to the
             output files.

             python powerbi_360.py --serials-file serials.txt
             Same bulk behavior, reading serials from a file (one per line,
             blank lines / lines starting with '#' ignored) instead.

             python powerbi_360.py --raw-dir raw
             Same bulk behavior, recursively scanning raw/ for .txt/.log files
             and extracting every serial from lines like:
             "System Serial Number: 951946000252 (prlpr01)"

Options
-------
  --login          Force an interactive login (use when session has expired)
  --headless       Run without a visible browser window (default)
  --no-headless    Run with a visible browser window
  --verbose        Show detailed step-by-step progress logs (default: minimal
                   output, one line per serial with success/failure)
  --resume         Skip serials already present in --out and append new
                   results to it instead of overwriting, so you can stop and
                   re-run later and pick up where you left off
  --alert MONTHS   Number of months ahead to flag the Alert column as 'Y'
                   (default: 12)
  --serials NUMBER Serial number(s) to select in the SERIAL NUMBER slicer,
                   comma-separated for bulk mode e.g. 792349000111,951946000252
                   (default: 792349000111)
  --serials-file FILE  Path to a text file with one serial number per line to
                   process in bulk (overrides --serials)
  --raw-dir DIR    Directory to recursively scan for .txt/.log files containing
                   lines like 'System Serial Number: 951946000252 (host)';
                   every serial found is processed in bulk (overrides --serials;
                   --serials-file takes precedence if both are given)
  --field NAME     Column/field name(s) to scrape, space-separated for multiple
                   (default: Part Number, Status, Hw End Of Support Date,
                   Hw Or Sw Service Or Warranty End Date, Hardware Service End Date,
                   Software Service End Date)
  --url URL        Power BI report URL (default: read from config.ini's
                   [powerbi] url setting, falling back to the built-in
                   360 report if config.ini is missing)
  --timeout MS     Navigation/wait timeout in milliseconds (default: 60000)
  --out FILE       CSV file to write the result to
                   (default: data/results.csv, overwritten each run)
  --out-text FILE  Text file to write the result to
                   (default: data/results.txt, overwritten each run)
"""

import argparse
import configparser
import csv
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CONFIG_PATH = Path(__file__).with_name("config.ini")


def _load_url_from_config(default: str) -> str:
    """Read the report URL from config.ini's [powerbi] url setting, if present."""
    if not CONFIG_PATH.exists():
        return default
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    return config.get("powerbi", "url", fallback=default)


POWERBI_URL = _load_url_from_config(
    "https://app.powerbi.com/groups/me/apps/551d7ce0-0237-4f1c-9928-c2024459c8ee/"
    "reports/23307175-4d78-4746-8a3c-3384bb79519d/890c2229854a81030663"
    "?pane=help&experience=power-bi"
)
PROFILE_DIR = Path.home() / ".config" / "powerbi-profile"
LOGIN_MARKER = "powerbi_logged_in"

VERBOSE = False


def vprint(*args, **kwargs):
    """print() that only outputs when --verbose is set."""
    if VERBOSE:
        print(*args, **kwargs)


def _without_help_pane(url: str) -> str:
    """Drop the 'pane=help' query param so the floating Help panel doesn't open
    and intercept clicks/typing meant for the report."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs.pop("pane", None)
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

# xpath fragment used to climb from a visual's text element up to its
# container, regardless of which Power BI DOM revision is rendering it.
_VISUAL_CONTAINER_XPATH = (
    "xpath=ancestor::*[contains(@class, 'visualContainer') or contains(@class, 'visual-container')][1]"
)


# ---------------------------------------------------------------------------
# Login / session helpers
# ---------------------------------------------------------------------------

def wait_for_login(page, url: str):
    """Open the report page and wait for the user to complete authentication."""
    vprint("[*] Opening Power BI report page …")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    print()
    print("=" * 60)
    print("  Please log in to Power BI in the browser window.")
    print("  When the report is fully loaded, come back here and")
    print("  press Enter.")
    print("=" * 60)
    input()

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / LOGIN_MARKER).touch()
    print("[+] Session saved to", PROFILE_DIR)


def is_session_saved() -> bool:
    return (PROFILE_DIR / LOGIN_MARKER).exists()


_SERIAL_NUMBER_LINE = re.compile(r"System Serial Number:\s*(\S+)", re.I)


def extract_serials_from_raw_dir(raw_dir: str) -> list[str]:
    """Recursively scan raw_dir for .txt/.log files and pull out every serial
    number from lines like 'System Serial Number: 951946000252 (host)'.

    Returns unique serials in first-seen order, sorted by file path so runs
    are reproducible.
    """
    seen = set()
    serials = []
    for path in sorted(Path(raw_dir).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".txt", ".log"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _SERIAL_NUMBER_LINE.finditer(text):
            serial = match.group(1).strip()
            if serial and serial not in seen:
                seen.add(serial)
                serials.append(serial)
    return serials


def backup_data_folder(data_dir: str = "data", backup_root: str = "backup"):
    """Copy the data folder into backup/data_<timestamp> before this run touches it."""
    src = Path(data_dir)
    if not src.exists():
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(backup_root) / f"data_{timestamp}"
    shutil.copytree(src, dest)
    vprint(f"[+] Backed up '{src}' -> {dest}")


def _dump_visual_titles(page, label: str):
    """Save every visual title found on the page so the right one can be identified."""
    try:
        titles = page.locator("[class*='visualTitle']")
        texts = [titles.nth(i).inner_text().strip() for i in range(titles.count())]
        Path(f"powerbi_debug_{label}_titles.txt").write_text("\n".join(texts), encoding="utf-8")
        vprint(f"[!] Available visual titles saved to powerbi_debug_{label}_titles.txt")
    except Exception:
        pass
    try:
        page.screenshot(path=f"powerbi_debug_{label}.png", full_page=True)
        vprint(f"    Screenshot  : powerbi_debug_{label}.png")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Report interaction
# ---------------------------------------------------------------------------

def _topmost_match(loc):
    """Return the locator match with the smallest Y coordinate (topmost on screen)."""
    best, best_y = None, None
    for i in range(loc.count()):
        el = loc.nth(i)
        box = el.bounding_box()
        if box is None:
            continue
        if best_y is None or box["y"] < best_y:
            best_y, best = box["y"], el
    return best or loc.first


def _closest_above(loc, anchor_box):
    """Return the locator match positioned just above anchor_box (largest Y that's still <= anchor's top)."""
    best, best_y = None, None
    anchor_top = anchor_box["y"]
    for i in range(loc.count()):
        el = loc.nth(i)
        box = el.bounding_box()
        if box is None or box["y"] > anchor_top:
            continue
        if best_y is None or box["y"] > best_y:
            best_y, best = box["y"], el
    return best


def _find_slicer_container(page, label_text: str, timeout: int, anchor_text: str = "Controller Details"):
    """Return the container of the visual whose visible text matches label_text exactly.

    Uses accessible text instead of guessed class names, since the report's
    slicer/table headers didn't match any of the usual Power BI class names.
    The same text can appear more than once (e.g. also as a table column
    header), so `anchor_text` (the report title known to sit right below the
    slicer) is used to pick the correct occurrence positioned just above it.
    """
    pattern = re.compile(rf"^\s*{re.escape(label_text)}\s*$", re.I)
    loc = page.get_by_text(pattern)
    try:
        loc.first.wait_for(state="visible", timeout=timeout)
    except PWTimeout:
        return None

    anchor_loc = page.locator("[class*='visualTitle']").filter(
        has_text=re.compile(rf"^\s*{re.escape(anchor_text)}\s*$", re.I)
    )
    label = None
    if anchor_loc.count() > 0:
        anchor_box = anchor_loc.first.bounding_box()
        if anchor_box is not None:
            label = _closest_above(loc, anchor_box)
    if label is None:
        label = _topmost_match(loc)
    return label.locator(_VISUAL_CONTAINER_XPATH)


def _find_dropdown_left_of(page, anchor_text: str):
    """Return the 'All' dropdown positioned immediately to the left of anchor_text (same row).

    'Clear all slicers' sits right next to the Serial Number dropdown, and its
    text is unique on the page, making it a reliable spatial anchor to
    disambiguate from other elements that also happen to show 'All'
    (e.g. the page-tab bar).
    """
    anchor_loc = page.get_by_text(anchor_text, exact=True)
    try:
        anchor_loc.first.wait_for(state="visible", timeout=5_000)
    except PWTimeout:
        return None
    anchor_box = anchor_loc.first.bounding_box()
    if anchor_box is None:
        return None
    anchor_cy = anchor_box["y"] + anchor_box["height"] / 2

    all_loc = page.get_by_text(re.compile(r"^\s*All\s*$"))
    best, best_dx = None, None
    for i in range(all_loc.count()):
        el = all_loc.nth(i)
        box = el.bounding_box()
        if box is None:
            continue
        cy = box["y"] + box["height"] / 2
        if abs(cy - anchor_cy) > 40 or box["x"] + box["width"] > anchor_box["x"]:
            continue  # not on the same row, or not to the left of the anchor
        dx = anchor_box["x"] - (box["x"] + box["width"])
        if best_dx is None or dx < best_dx:
            best_dx, best = dx, el
    return best


def _frames(page):
    """Main frame plus any child frames (Power BI can sandbox individual visuals in iframes)."""
    return [page.main_frame] + [f for f in page.frames if f != page.main_frame]


def _find_input_near(page, anchor_box, max_below: int = 400, max_side: int = 50):
    """Find an input positioned just below/around anchor_box, searching every frame."""
    best, best_y = None, None
    for frame in _frames(page):
        try:
            inputs = frame.locator("input")
            count = inputs.count()
        except Exception:
            continue
        for i in range(count):
            el = inputs.nth(i)
            try:
                box = el.bounding_box()
            except Exception:
                box = None
            if box is None:
                continue
            if box["y"] < anchor_box["y"] or box["y"] > anchor_box["y"] + max_below:
                continue
            if box["x"] < anchor_box["x"] - 20 or box["x"] > anchor_box["x"] + anchor_box["width"] + max_side:
                continue
            if best_y is None or box["y"] < best_y:
                best_y, best = box["y"], el
    return best


def _find_text_in_any_frame(page, text: str, exact: bool = False):
    """Return the first element matching `text`, searching every frame."""
    for frame in _frames(page):
        try:
            loc = frame.get_by_text(text, exact=exact)
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def clear_all_slicers(page, timeout: int) -> None:
    """Click the 'Clear all slicers' button to reset filters before selecting the next serial."""
    page.keyboard.press("Escape")  # dismiss any open dropdown first
    page.wait_for_timeout(500)
    box = None
    for _ in range(10):
        # Some frames have a hidden duplicate (box=None) alongside the real,
        # visible element, so scan all matches instead of taking the first.
        for frame in _frames(page):
            try:
                loc = frame.get_by_text("Clear all slicers", exact=False)
                for i in range(loc.count()):
                    candidate_box = loc.nth(i).bounding_box()
                    if candidate_box is not None:
                        box = candidate_box
                        break
            except Exception:
                continue
            if box is not None:
                break
        if box is not None:
            break
        page.wait_for_timeout(500)
    if box is None:
        vprint("[!] Could not find 'Clear all slicers' — continuing anyway")
        page.screenshot(path="powerbi_debug_clear_slicers_failed.png", full_page=True)
        return
    # Raw mouse click, like the slicer opener: Power BI intercepts pointer
    # events in ways that don't always satisfy Playwright's actionability checks.
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    page.mouse.click(cx, cy)
    vprint("[*] Clicked 'Clear all slicers'")
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass
    time.sleep(2)


def fill_serial_number(page, serial: str, timeout: int) -> bool:
    """Select `serial` in the 'Serial Number' slicer (text box or dropdown/list style)."""
    vprint("[*] Looking for the 'Serial Number' box …")
    page.keyboard.press("Escape")  # dismiss any stray context menu left over from a previous run

    opener = _find_dropdown_left_of(page, "Clear all slicers")
    if opener is None:
        # Fallback: locate the slicer via its own container instead of the anchor.
        container = _find_slicer_container(page, "Serial Number", timeout)
        if container is None:
            vprint("[!] Could not find the 'Serial Number' slicer.")
            _dump_visual_titles(page, "serial_number")
            return False

        input_loc = container.locator("input, textarea")
        if input_loc.count() > 0:
            box = input_loc.first
            box.click()
            box.fill(serial)
            box.press("Enter")
            vprint(f"[+] Entered serial number '{serial}' into text box")
            return True

        all_loc = container.get_by_text(re.compile(r"^\s*All\s*$"))
        opener = all_loc.first if all_loc.count() > 0 else container.first

    vprint("[*] 'Serial Number' looks like a dropdown/list slicer – expanding it …")
    try:
        opener.scroll_into_view_if_needed(timeout=timeout)
    except PWTimeout:
        pass

    opener_box = opener.bounding_box()
    if opener_box is None:
        vprint("[!] Could not determine the position of the 'Serial Number' dropdown.")
        _dump_visual_titles(page, "serial_number")
        return False

    # A raw mouse click at the widget's pixel coordinates is more reliable than
    # Playwright's element click here, since Power BI's dropdown intercepts
    # pointer events in ways that don't always satisfy standard actionability checks.
    cx = opener_box["x"] + opener_box["width"] / 2
    cy = opener_box["y"] + opener_box["height"] / 2
    page.mouse.click(cx, cy)
    page.wait_for_timeout(500)

    # Locate the search box by position rather than class/placeholder, since the
    # popup's search input doesn't expose either attribute with "search" in it,
    # and it may live inside a sandboxed visual iframe invisible to page.locator().
    # Poll for it since the popup's contents can load in asynchronously.
    search_loc = None
    for _ in range(10):
        search_loc = _find_input_near(page, opener_box)
        if search_loc is not None:
            break
        page.wait_for_timeout(500)

    page.screenshot(path="powerbi_debug_dropdown_open.png", full_page=True)
    vprint("    Screenshot after opening dropdown : powerbi_debug_dropdown_open.png")

    if search_loc is not None:
        try:
            search_loc.click()
            # Clear any leftover text from a previous serial (this box persists
            # across calls within the same browser session/run) before typing.
            # fill() sets the value directly, which doesn't trigger the list's
            # keystroke-driven filtering, so use select-all + Delete instead.
            search_loc.press("Control+A")
            search_loc.press("Delete")
            search_loc.press_sequentially(serial, delay=80)
            try:
                vprint(f"    [debug] search box value after typing: {search_loc.input_value()!r}")
            except Exception as e:
                vprint(f"    [debug] could not read search box value: {e}")
            search_loc.press("Enter")
        except PWTimeout:
            pass

    # The backend filter query latency is unpredictable (seen anywhere from
    # ~10s to ~60s), so poll for the matching option instead of a fixed sleep.
    vprint("[*] Waiting for the slicer list to filter …")
    option_el = None
    for _ in range(30):
        option_el = _find_text_in_any_frame(page, serial, exact=False)
        if option_el is not None:
            break
        page.wait_for_timeout(2_000)
    if option_el is not None:
        try:
            option_el.click(timeout=15_000)
            vprint(f"[+] Selected '{serial}' from the dropdown list")
        except PWTimeout:
            option_el = None
    if option_el is None:
        vprint(f"[!] Could not find an option matching '{serial}' in the dropdown.")
        _dump_visual_titles(page, "serial_number")
        page.screenshot(path="powerbi_debug_serial_number_dropdown.png", full_page=True)
        return False

    page.keyboard.press("Escape")  # collapse the dropdown, committing the selection
    return True



def get_os_version(page, field: str, timeout: int) -> str | None:
    """Read the value of the `field` column from the report's data table."""
    vprint(f"[*] Looking for the '{field}' column …")
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*$", re.I)

    # This table visual wraps into several stacked row-groups (each block has
    # its own header + data row), and aria-colindex is reused/offset across
    # them, so matching by aria-colindex can silently grab a cell from the
    # wrong block/column. Match by horizontal alignment with the header
    # text instead — it stays scoped to the header's own row-group.
    header_text_loc = page.get_by_text(pattern)
    try:
        header_text_loc.first.wait_for(state="visible", timeout=timeout)
    except PWTimeout:
        vprint(f"[!] Could not find a '{field}' column.")
        _dump_visual_titles(page, "os_version")
        return None

    header_box = _topmost_match(header_text_loc).bounding_box()
    if header_box is None:
        vprint(f"[!] Could not read the position of the '{field}' column header.")
        return None
    target_x = header_box["x"] + header_box["width"] / 2
    header_bottom = header_box["y"] + header_box["height"]

    # Prefer the more specific 'gridcell' role first — the broader
    # "[class*='cell']" selector also matches row-wrapper elements whose
    # inner_text concatenates every column's value (with newlines), which
    # would otherwise get picked up as if it were this column's value.
    best_text, best_y = None, None
    for selector in ("[role='gridcell']", "[role='cell'], [class*='cell']"):
        candidates = page.locator(selector)
        for i in range(candidates.count()):
            box = candidates.nth(i).bounding_box()
            if box is None or box["y"] < header_bottom:
                continue
            cx = box["x"] + box["width"] / 2
            dx = abs(cx - target_x)
            # Tight tolerance: half the narrower of the two widths, so a
            # neighboring column's cell can't be mistaken for this one.
            if dx > min(box["width"], header_box["width"]) / 2 + 4:
                continue
            # A real single-value cell doesn't span multiple lines; wrapper
            # elements holding an entire row's values do.
            if box["width"] > header_box["width"] * 3:
                continue
            text = candidates.nth(i).inner_text().strip()
            if "\n" in text or not text:
                continue
            if best_y is None or box["y"] < best_y:
                best_text, best_y = text, box["y"]
        if best_text:
            break

    if best_text:
        return best_text

    vprint(f"[!] Could not find a data value aligned under '{field}'.")
    _dump_visual_titles(page, "os_version")
    return None


def load_existing_results(path: str) -> list[dict]:
    """Read previously saved rows from a results CSV, or [] if it doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save_result(path: str, results_list: list[dict]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    headers = list(dict.fromkeys(key for results in results_list for key in results))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for results in results_list:
            writer.writerow([results.get(h, "") for h in headers])
    print(f"[+] Wrote result -> {path}")


def save_result_text(path: str, results_list: list[dict]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    headers = list(dict.fromkeys(key for results in results_list for key in results))
    value_rows = [[str(results.get(h, "")) for h in headers] for results in results_list]
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in value_rows)) + 3
        for i in range(len(headers))
    ]
    header_line = "".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()
    separator = "-" * len(header_line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header_line + "\n")
        fh.write(separator + "\n")
        for row in value_rows:
            fh.write("".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() + "\n")
    print(f"[+] Wrote result -> {path}")


ALERT_DATE_FIELDS = [
    "Hw End Of Support Date",
    "Hw Or Sw Service Or Warranty End Date",
    "Hardware Service End Date",
    "Software Service End Date",
]


def compute_alert(results: dict, months: int = 12) -> str:
    """"Y"/"N" based on ALERT_DATE_FIELDS falling within the next `months` months.

    Returns "" if none of the date fields have a usable value (e.g. the serial
    wasn't found), so blank rows don't get a misleading "N".
    """
    threshold = datetime.now() + timedelta(days=round(months * 365 / 12))
    found_date = False
    for field in ALERT_DATE_FIELDS:
        value = results.get(field, "").strip()
        if not value or value.upper() == "N":
            continue
        try:
            date = datetime.strptime(value, "%m/%d/%Y")
        except ValueError:
            continue
        found_date = True
        if date <= threshold:
            return "Y"
    return "N" if found_date else ""


def process_serial(page, serial: str, args) -> dict:
    """Select `serial` in the slicer and scrape all requested fields for it.

    Always returns a row for `serial` — fields that couldn't be found (serial
    not in the dropdown, or a column missing) are left as empty strings so the
    serial still shows up in the output files with no data.
    """
    results = {field: "" for field in args.field}

    if not fill_serial_number(page, serial, args.timeout):
        return {"Serial Number": serial, "Alert": compute_alert(results, args.alert), **results}

    vprint("[*] Waiting for report to refresh with the serial number filter …")
    try:
        page.wait_for_load_state("networkidle", timeout=args.timeout)
    except PWTimeout:
        pass
    time.sleep(3)  # extra settle time for visual re-render

    for field in args.field:
        value = get_os_version(page, field, args.timeout)
        if value is None:
            vprint(f"[!] {field}: not found — leaving blank")
            continue
        results[field] = value
        vprint(f"[+] {field}: {value}")

    # Serial Number is the input, not a scraped field — put it first.
    return {"Serial Number": serial, "Alert": compute_alert(results, args.alert), **results}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fill the Serial Number box on a Power BI report and scrape the OS Version")
    parser.add_argument("--login", action="store_true", help="Force interactive login")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True, help="Run without a visible browser window (default)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Run with a visible browser window")
    parser.add_argument("--verbose", action="store_true", help="Show detailed step-by-step progress logs (default: minimal output)")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip serials already present in --out and append new results to it instead of "
        "overwriting, so you can stop and re-run later and pick up where you left off",
    )
    parser.add_argument(
        "--alert",
        type=int,
        default=12,
        metavar="MONTHS",
        help="Number of months ahead to flag the Alert column as 'Y' (default: 12)",
    )
    parser.add_argument(
        "--serials",
        default="792349000111",
        help="Serial number(s) to enter, comma-separated for bulk mode "
        "e.g. --serials 792349000111,951946000252 (default: 792349000111)",
    )
    parser.add_argument(
        "--serials-file",
        default=None,
        help="Path to a text file with one serial number per line to process in bulk "
        "(blank lines and lines starting with '#' are ignored; overrides --serials)",
    )
    parser.add_argument(
        "--raw-dir",
        default=None,
        help="Directory to recursively scan for .txt/.log files containing lines like "
        "'System Serial Number: 951946000252 (host)'; every serial found is processed "
        "in bulk (overrides --serials; --serials-file takes precedence if both are given)",
    )
    parser.add_argument(
        "--field",
        nargs="+",
        default=[
            "Part Number",
            "Status",
            "Hw End Of Support Date",
            "Hw Or Sw Service Or Warranty End Date",
            "Hardware Service End Date",
            "Software Service End Date",
        ],
        help="Column name(s) to scrape, space-separated",
    )
    parser.add_argument("--url", default=POWERBI_URL, help="Power BI report URL (default: read from config.ini's [powerbi] url, falling back to the built-in 360 report)")
    parser.add_argument("--timeout", type=int, default=60_000, help="Navigation/wait timeout (ms)")
    parser.add_argument(
        "--out",
        default=str(Path("data") / "results.csv"),
        help="CSV file to write the result to (default: data/results.csv, overwritten each run; use '' to disable)",
    )
    parser.add_argument(
        "--out-text",
        default=str(Path("data") / "results.txt"),
        help="Text file to write the result to (default: data/results.txt, overwritten each run; use '' to disable)",
    )
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if args.serials_file:
        lines = Path(args.serials_file).read_text(encoding="utf-8").splitlines()
        serials = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        if not serials:
            print(f"[!] No serial numbers found in {args.serials_file}")
            sys.exit(1)
    elif args.raw_dir:
        serials = extract_serials_from_raw_dir(args.raw_dir)
        if not serials:
            print(f"[!] No serial numbers found under {args.raw_dir}")
            sys.exit(1)
        print(f"[*] Found {len(serials)} serial number(s) under {args.raw_dir}")
    else:
        serials = [s.strip() for s in args.serials.split(",") if s.strip()]

    existing_results = []
    if args.resume:
        if not args.out:
            print("[!] --resume requires --out to track already-processed serials — ignoring --resume.")
        else:
            existing_results = load_existing_results(args.out)
            done_serials = {r["Serial Number"] for r in existing_results}
            before = len(serials)
            serials = [s for s in serials if s not in done_serials]
            skipped = before - len(serials)
            if skipped:
                print(f"[*] Resuming: skipping {skipped} serial(s) already in {args.out}")
            if not serials:
                print("[*] All serials already processed — nothing to do.")
                sys.exit(0)

    backup_data_folder()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if args.login and args.headless:
        print("[*] --login requires a visible browser – ignoring --headless.")
        args.headless = False

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
            channel="msedge",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()

        nav_url = _without_help_pane(args.url)
        if args.login or not is_session_saved():
            wait_for_login(page, nav_url)
        else:
            vprint("[*] Reusing saved session from", PROFILE_DIR)
            page.goto(nav_url, wait_until="domcontentloaded", timeout=args.timeout)

        if args.login:
            context.close()
            return

        if "login" in page.url.lower() or "signin" in page.url.lower():
            print("[!] Redirected to login – session may have expired.")
            print("    Re-run with --login to refresh your session.")
            context.close()
            sys.exit(1)

        vprint("[*] Waiting for report to render …")
        try:
            page.wait_for_selector("[class*='visualTitle']", timeout=args.timeout)
        except PWTimeout:
            print("[!] Report visuals did not appear.")
            page.screenshot(path="powerbi_debug_load.png", full_page=True)
            context.close()
            sys.exit(1)
        time.sleep(2)  # let visuals fully settle

        all_results = list(existing_results)
        for i, serial in enumerate(serials):
            if i > 0:
                clear_all_slicers(page, args.timeout)
            print(f"[*] Pulling data for serial '{serial}' …")
            result = process_serial(page, serial, args)
            if any(result[field] for field in args.field):
                print(f"[+] Serial '{serial}': success")
            else:
                print(f"[!] Serial '{serial}': not found — recorded with blank fields")
            all_results.append(result)

        if not all_results:
            print("[!] No results collected.")
            context.close()
            sys.exit(1)

        if args.out:
            save_result(args.out, all_results)
        if args.out_text:
            save_result_text(args.out_text, all_results)

        context.close()


if __name__ == "__main__":
    main()
