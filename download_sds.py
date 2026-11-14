#!/usr/bin/env python3  # Run this file with Python 3 when executed directly on Linux or macOS
# Download all SDS / MSDS PDFs from Agilent's chemical-standards search page using Chromium via Playwright.
#
# How it works:
#   1. Starts your installed Chromium as a normal window and attaches Playwright to it.
#   2. Visits the Agilent home page briefly, then searches the keywords a-z and 0-9.
#   3. After every result page it downloads the PDFs found there right away (in a second tab).
#   4. A SQLite database remembers every URL and file name, so nothing is downloaded twice.
#   5. If Chromium crashes or closes, it is restarted and the work continues where it stopped.
#   6. Pages that say "The page may be temporarily unavailable or may no longer exist" are skipped.
#   7. Random pauses, back-off waits and a stop-after-repeated-blocks rule keep the traffic gentle.
#   8. Everything that happens is logged to the console.
#
# Setup: pip install playwright && playwright install chromium
# Run: python download_sds.py
#
# File layout:
#   1. Imports
#   2. Settings
#   3. Patterns and constants
#   4. Logging
#   5. Database
#   6. File names
#   7. Chromium process
#   8. Polite pacing and block tracking
#   9. Page checks
#  10. Page actions
#  11. Downloading
#  12. Scraping
#  13. Browser session and restarts
#  14. Main program

# ============================================================== 1. IMPORTS
import json  # Reads and writes the Chromium preferences file
import logging  # Prints detailed log lines to the console
import os  # Reads environment variables and checks whether files exist
import random  # Creates random pause lengths
import re  # Regular expressions for link matching and text cleaning
import sqlite3  # Built-in SQLite database used to remember what was already downloaded
import string  # Provides the lowercase letters and digits used as search keywords
import subprocess  # Starts the Chromium program as a normal process
import sys  # Gives access to the console output stream
import time  # Provides sleep() and the current time
import urllib.request  # Used to check whether Chromium is ready to accept a connection
from pathlib import Path  # Object-oriented file system paths
from typing import Optional  # Type hint for values that may be None
from urllib.parse import unquote, urlparse  # Helpers to read a file name out of a URL

from playwright.sync_api import (
    Dialog,
    Download,
    Frame,
    Page,
    Response,
)  # Playwright types used in type hints
from playwright.sync_api import (
    Error as PlaywrightError,
)  # Base error class raised by Playwright
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)  # Error raised when Playwright waits too long
from playwright.sync_api import (
    sync_playwright,
)  # Starts Playwright using the synchronous API

# ============================================================== 2. SETTINGS
# ---- Search settings
SEARCH_URL_TEMPLATE = (  # Search page URL with a placeholder where the keyword goes
    "https://www.agilent.com/en/product/chemical-standards/search-results"  # First half of the URL
    "?tabname=related_part_number&keyword={search_keyword}"  # Second half, with the keyword placeholder
)  # End of the URL template
SEARCH_KEYWORDS = list(
    string.ascii_lowercase + string.digits
)  # Keywords a to z followed by 0 to 9
HOME_PAGE_URL = (
    "https://www.agilent.com/"  # Page visited first, like a normal visitor would do
)
HOME_PAGE_WARM_UP_SECONDS = (
    3  # How long to stay on the home page before scraping starts
)

# ---- Folders and files
DOWNLOAD_FOLDER = Path(
    "PDFs"
).resolve()  # Absolute path of the folder where the PDFs are saved
DATABASE_FILE_PATH = Path(
    "download_history.sqlite3"
).resolve()  # Absolute path of the SQLite database file
CHROMIUM_PROFILE_FOLDER = Path(
    "chromium_profile"
).resolve()  # Folder where this script's own Chromium profile lives
MAXIMUM_FILE_NAME_STEM_LENGTH = (
    150  # Longest allowed file name, not counting the .pdf extension
)

# ---- Limits and timeouts
MAXIMUM_RESULT_PAGES_PER_KEYWORD = (
    200  # Safety limit on how many result pages are followed per keyword
)
NAVIGATION_TIMEOUT_IN_MILLISECONDS = 30000  # How long to wait for a page to open
DOWNLOAD_TIMEOUT_IN_MILLISECONDS = (
    60000  # How long to wait for Chromium to start one download
)

# ---- Polite pacing and block handling
PAUSE_BETWEEN_RESULT_PAGES_IN_SECONDS = (
    2.0,
    5.0,
)  # Random pause range before moving to the next result page
PAUSE_BETWEEN_DOWNLOADS_IN_SECONDS = (
    1.0,
    3.0,
)  # Random pause range between two downloads
PAUSE_BETWEEN_KEYWORDS_IN_SECONDS = (
    5.0,
    10.0,
)  # Random pause range before starting the next keyword
MAXIMUM_ACCESS_DENIED_ATTEMPTS = (
    3  # How many times to try a page that shows "Access Denied"
)
WAIT_AFTER_ACCESS_DENIED_IN_SECONDS = (
    30  # First back-off wait, which doubles on every further attempt
)
MAXIMUM_CONSECUTIVE_BLOCKS = 5  # Stop the whole run after this many blocks in a row
MAXIMUM_BROWSER_RESTARTS = (
    5  # How many times Chromium may be restarted after closing or crashing
)

# ---- Chromium settings
WEBRTC_IP_HANDLING_POLICY = "default_public_interface_only"  # Hides local IPs from WebRTC (or "disable_non_proxied_udp")
CHROMIUM_DEBUGGING_PORT = (
    9222  # Local port Playwright uses to talk to the running Chromium
)
CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS = (
    30  # How long to wait for Chromium to be ready after starting it
)

# ============================================================== 3. PATTERNS AND CONSTANTS
PDF_OR_SDS_LINK_PATTERN = re.compile(  # Matches links that look like PDFs or safety data sheets
    r"\.pdf(\?|#|$)|msds|/sds|sds[_/\-.]|safety[-_ ]?data",  # Alternative patterns joined by |
    re.IGNORECASE,  # Ignore upper and lower case differences
)  # End of the pattern definition
RAW_PDF_URL_PATTERN = re.compile(  # Matches raw .pdf URLs hidden anywhere in the page source
    r"https?://[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?",  # Full URL that ends in .pdf
    re.IGNORECASE,  # Ignore upper and lower case differences
)  # End of the pattern definition
NEXT_PAGE_BUTTON_SELECTORS = [  # Different ways the "next page" button might be found
    "button[aria-label*='next' i]",  # Button whose label mentions "next"
    "a[aria-label*='next' i]",  # Link whose label mentions "next"
    "li.next:not(.disabled) a",  # Enabled "next" item inside a pagination list
    "a.next",  # Link that has the class "next"
    "button.next",  # Button that has the class "next"
    "[class*='pagination'] [class*='next']",  # "next" element inside pagination
    "xpath=//button[normalize-space()='Next' or normalize-space()='>']",  # Button with text Next or >
    "xpath=//a[normalize-space()='Next' or normalize-space()='>']",  # Link with text Next or >
]  # End of the selector list

PAGE_UNAVAILABLE_MESSAGE = "the page may be temporarily unavailable or may no longer exist"  # Text of the error page

PAGE_STATUS_OPENED = "opened"  # The page opened normally
PAGE_STATUS_UNAVAILABLE = (
    "unavailable"  # The page shows the "page may no longer exist" message
)
PAGE_STATUS_BLOCKED = "blocked"  # The page kept showing "Access Denied"
PAGE_STATUS_FAILED = "failed"  # The page could not be opened at all

BROWSER_CLOSED_MESSAGES = (  # Pieces of Playwright error text that mean Chromium or its tab is gone
    "has been closed",  # "Target page, context or browser has been closed"
    "target closed",  # Older wording of the same problem
    "browser closed",  # The browser window was closed
    "connection closed",  # The link to Chromium was lost
    "browser has disconnected",  # Playwright lost its connection to Chromium
)  # End of the list

OUTCOME_DOWNLOAD_STARTED = "download started"  # Chromium started downloading a file
OUTCOME_PAGE_UNAVAILABLE = (
    "page unavailable"  # The link led to the "page may no longer exist" message
)
OUTCOME_ACCESS_DENIED = "access denied"  # The link led to the "Access Denied" page
OUTCOME_NOT_A_DOWNLOAD = (
    "not a download"  # The link led to some other web page instead of a file
)
OUTCOME_TIMEOUT = "timeout"  # Nothing happened before the time limit

logger = logging.getLogger(
    "sds_downloader"
)  # Logger used by every function in this script


class AccessBlockedError(
    Exception
):  # Raised when the website keeps blocking us and we should stop
    pass  # No extra behaviour is needed


# ============================================================== 4. LOGGING
def configure_console_logging() -> None:  # Send every log message to the console
    console_handler = logging.StreamHandler(
        sys.stdout
    )  # Create a handler that writes to standard output
    console_handler.setFormatter(  # Choose how each log line looks
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
        )  # Time, level, text
    )  # End of the formatter setup
    logger.setLevel(
        logging.DEBUG
    )  # Show everything, including the most detailed messages
    logger.addHandler(console_handler)  # Attach the console handler to our logger
    logger.propagate = False  # Prevent every line from being printed twice


def log_browser_console_message(
    console_message,
) -> None:  # Log text the website prints to its own console
    logger.debug(
        "BROWSER CONSOLE [%s] %s", console_message.type, console_message.text
    )  # Write type and text


def log_browser_page_error(
    page_error,
) -> None:  # Log JavaScript errors raised by the website
    logger.error("BROWSER PAGE ERROR %s", page_error)  # Write the error text


def log_failed_request(failed_request) -> None:  # Log network requests that failed
    logger.warning(  # Write one warning line
        "REQUEST FAILED %s %s -> %s",  # Message template
        failed_request.method,  # HTTP method such as GET
        failed_request.url,  # Address that failed
        failed_request.failure,  # Reason for the failure
    )  # End of the warning call


def log_response_if_error(
    response: Response,
) -> None:  # Log HTTP responses that are errors
    if response.status >= 400:  # Check for client errors (4xx) and server errors (5xx)
        logger.warning(
            "HTTP %s %s", response.status, response.url
        )  # Write the status code and address


def log_main_frame_navigation(
    frame: Frame,
) -> None:  # Log every time the main page changes address
    if (
        frame.parent_frame is None
    ):  # Only the top-level page counts, not embedded frames
        logger.info("NAVIGATED to %s", frame.url)  # Write the new address


def log_download_started(
    download: Download,
) -> None:  # Log every download that Chromium starts
    logger.info(
        "CHROMIUM DOWNLOAD STARTED url=%s suggested_name=%s",
        download.url,
        download.suggested_filename,
    )  # Details


def handle_browser_dialog(
    dialog: Dialog,
) -> None:  # Deal with alert/confirm popups so they cannot block us
    logger.warning(
        "BROWSER DIALOG [%s] %s", dialog.type, dialog.message
    )  # Log what the popup said
    dialog.dismiss()  # Close the popup


def attach_browser_event_logging(
    page: Page,
) -> None:  # Connect all the log functions above to a tab
    page.on("console", log_browser_console_message)  # Log website console output
    page.on("pageerror", log_browser_page_error)  # Log JavaScript errors
    page.on("requestfailed", log_failed_request)  # Log failed network requests
    page.on("response", log_response_if_error)  # Log HTTP error responses
    page.on("framenavigated", log_main_frame_navigation)  # Log page navigations
    page.on("download", log_download_started)  # Log downloads
    page.on("dialog", handle_browser_dialog)  # Log and dismiss popups


# ============================================================== 5. DATABASE
def connect_to_database() -> sqlite3.Connection:  # Open (or create) the SQLite database file
    logger.info(
        "Opening database: %s", DATABASE_FILE_PATH
    )  # Tell the user which database file is used
    database_connection = sqlite3.connect(
        DATABASE_FILE_PATH
    )  # Connect to the database file on disk
    return database_connection  # Give the open connection back to the caller


def create_database_tables(
    database_connection: sqlite3.Connection,
) -> None:  # Create the tables if missing
    logger.debug(
        "Creating database tables if they do not exist yet"
    )  # Explain the next step
    with database_connection:  # Commit automatically when this block finishes
        database_connection.execute(  # Table holding every URL found on the website
            "CREATE TABLE IF NOT EXISTS collected_urls ("  # Table name
            "source_url TEXT PRIMARY KEY, "  # Each URL can appear only once
            "is_processed INTEGER NOT NULL DEFAULT 0)"  # 0 means still to do, 1 means finished
        )  # End of the first table definition
        database_connection.execute(  # Table holding every file that was saved
            "CREATE TABLE IF NOT EXISTS downloaded_files ("  # Table name
            "file_name TEXT PRIMARY KEY COLLATE NOCASE, "  # Each file name can appear only once, ignoring case
            "source_url TEXT NOT NULL, "  # The URL the file came from
            "downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"  # When the file was saved
        )  # End of the second table definition


def register_existing_pdf_files(
    database_connection: sqlite3.Connection,
) -> None:  # Remember PDFs already on disk
    registered_count = 0  # Counts how many existing files were newly registered
    with database_connection:  # Commit automatically when this block finishes
        for existing_pdf_path in DOWNLOAD_FOLDER.glob(
            "*.pdf"
        ):  # Look at every PDF already in the folder
            insert_cursor = database_connection.execute(  # Insert the file name unless it is already recorded
                "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",  # SQL statement
                (
                    existing_pdf_path.name,
                    "found on disk before first run",
                ),  # Values for the two placeholders
            )  # End of the insert
            registered_count += (
                insert_cursor.rowcount
            )  # Add 1 when a new row was really inserted
    logger.info(
        "Registered %d existing PDF file(s) from %s", registered_count, DOWNLOAD_FOLDER
    )  # Report the result


def save_collected_urls(
    database_connection: sqlite3.Connection, collected_urls: set
) -> None:  # Store new URLs
    with database_connection:  # Commit automatically when this block finishes
        insert_cursor = database_connection.executemany(  # Insert many URLs in one call
            "INSERT OR IGNORE INTO collected_urls (source_url) VALUES (?)",  # Ignore URLs we already know
            [
                (collected_url,) for collected_url in collected_urls
            ],  # One single-value tuple per URL
        )  # End of the insert
    logger.info(  # Report how many of the collected URLs were new to the database
        "Saved %d new URL(s) to the database (%d were already known)",  # Message template
        insert_cursor.rowcount,  # Number of rows really inserted
        len(collected_urls)
        - insert_cursor.rowcount,  # Number of URLs that were already stored
    )  # End of the log call


def load_unprocessed_urls(
    database_connection: sqlite3.Connection,
) -> list:  # Get URLs that still need work
    query_result = database_connection.execute(  # Ask the database for the pending URLs
        "SELECT source_url FROM collected_urls WHERE is_processed = 0 ORDER BY source_url"  # SQL statement
    )  # End of the query
    return [
        database_row[0] for database_row in query_result.fetchall()
    ]  # Return just the URL text of each row


def filter_unprocessed_urls(
    database_connection: sqlite3.Connection, candidate_urls: list
) -> list:  # Keep pending URLs
    unprocessed_urls = []  # Collects the URLs that still need to be handled
    for candidate_url in candidate_urls:  # Check each candidate URL
        query_result = database_connection.execute(  # Ask the database about this URL
            "SELECT is_processed FROM collected_urls WHERE source_url = ?",  # SQL statement
            (candidate_url,),  # Value for the placeholder
        )  # End of the query
        database_row = query_result.fetchone()  # Read the answer
        if (
            database_row is not None and database_row[0] == 0
        ):  # Known URL that has not been processed yet
            unprocessed_urls.append(candidate_url)  # Keep it
    return unprocessed_urls  # Give back only the URLs that still need work


def mark_url_as_processed(
    database_connection: sqlite3.Connection, source_url: str
) -> None:  # Mark URL as done
    with database_connection:  # Commit automatically when this block finishes
        database_connection.execute(  # Update the row for this URL
            "UPDATE collected_urls SET is_processed = 1 WHERE source_url = ?",  # SQL statement
            (source_url,),  # Value for the placeholder
        )  # End of the update
    logger.debug(
        "Database: marked URL as processed: %s", source_url
    )  # Record the database change


def is_file_name_already_downloaded(
    database_connection: sqlite3.Connection, file_name: str
) -> bool:  # Name check
    query_result = (
        database_connection.execute(  # Look for this file name in the database
            "SELECT 1 FROM downloaded_files WHERE file_name = ?",  # SQL statement
            (file_name,),  # Value for the placeholder
        )
    )  # End of the query
    return query_result.fetchone() is not None  # True when a matching row exists


def record_downloaded_file(
    database_connection: sqlite3.Connection, file_name: str, source_url: str
) -> None:  # Save
    with database_connection:  # Commit automatically when this block finishes
        database_connection.execute(  # Insert a row describing the saved file
            "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",  # SQL statement
            (file_name, source_url),  # Values for the two placeholders
        )  # End of the insert
    logger.debug(
        "Database: recorded downloaded file %s from %s", file_name, source_url
    )  # Record the change


# ============================================================== 6. FILE NAMES
def guess_file_name_from_url(
    source_url: str,
) -> Optional[str]:  # Predict the file name before downloading
    url_path = unquote(
        urlparse(source_url).path
    )  # Take the path part of the URL and decode %20 style codes
    last_path_segment = Path(url_path).name  # Take the last part of the path
    if last_path_segment.lower().endswith(
        ".pdf"
    ):  # Only trust names that really end in .pdf
        return last_path_segment  # Give the guessed name back
    return None  # The URL does not reveal the real file name, so do not guess


def build_safe_file_name(
    raw_file_name: str,
) -> str:  # Clean a file name to follow the naming rules
    if raw_file_name.lower().endswith(
        ".pdf"
    ):  # Check whether the name already ends with .pdf
        raw_file_name_stem = raw_file_name[
            :-4
        ]  # Remove the .pdf extension so only the stem is cleaned
    else:  # The name has no .pdf extension
        raw_file_name_stem = raw_file_name  # Clean the whole name as the stem
    lowercase_stem = (
        raw_file_name_stem.lower()
    )  # Make letters lowercase so A-Z becomes a-z instead of being removed
    cleaned_stem = re.sub(
        r"[^a-z0-9_]+", "_", lowercase_stem
    )  # Replace everything except a-z, 0-9 and _ with _
    cleaned_stem = re.sub(
        r"_+", "_", cleaned_stem
    )  # Collapse repeated underscores into a single one
    cleaned_stem = cleaned_stem[
        :MAXIMUM_FILE_NAME_STEM_LENGTH
    ]  # Cut very long names down to the limit
    cleaned_stem = cleaned_stem.strip(
        "_"
    )  # Remove underscores from the start and the end
    if not cleaned_stem:  # Check whether nothing is left after cleaning
        cleaned_stem = "document"  # Use a fallback name so the file name is never empty
    return (
        cleaned_stem + ".pdf"
    )  # Add the .pdf extension back and return the finished name


# ============================================================== 7. CHROMIUM PROCESS
def prepare_chromium_profile_preferences() -> None:  # Tell Chromium to download PDFs instead of previewing them
    default_profile_folder = (
        CHROMIUM_PROFILE_FOLDER / "Default"
    )  # Folder that holds Chromium's profile settings
    default_profile_folder.mkdir(
        parents=True, exist_ok=True
    )  # Create the folder if it does not exist
    preferences_file_path = (
        default_profile_folder / "Preferences"
    )  # Chromium's preferences file
    chromium_preferences = {}  # Start with empty preferences
    if (
        preferences_file_path.exists()
    ):  # Check whether Chromium already created a preferences file
        try:  # The existing file might be damaged
            chromium_preferences = json.loads(
                preferences_file_path.read_text(encoding="utf-8")
            )  # Load it
        except json.JSONDecodeError:  # The file could not be parsed
            logger.warning(
                "Chromium preferences file was unreadable and will be replaced"
            )  # Tell the user
            chromium_preferences = {}  # Start again with empty preferences
    chromium_preferences.setdefault("plugins", {})[
        "always_open_pdf_externally"
    ] = True  # Download PDFs, no viewer
    chromium_preferences.setdefault("download", {})[
        "prompt_for_download"
    ] = False  # Never ask where to save
    preferences_file_path.write_text(
        json.dumps(chromium_preferences), encoding="utf-8"
    )  # Save the preferences
    logger.debug(
        "Chromium profile preferences written to %s", preferences_file_path
    )  # Confirm the step


def find_chromium_executable_path(
    playwright_instance,
) -> str:  # Locate the Chromium program that Playwright installed
    chromium_executable_path = (
        playwright_instance.chromium.executable_path
    )  # Ask Playwright where its Chromium is
    if not Path(
        chromium_executable_path
    ).is_file():  # Check whether the program really exists
        raise FileNotFoundError(  # Stop with a clear message
            f"Chromium was not found at {chromium_executable_path}. Run this once: playwright install chromium"  # Fix
        )  # End of the error
    logger.debug(
        "Found Chromium at %s", chromium_executable_path
    )  # Record where it was found
    return chromium_executable_path  # Use this Chromium


def start_normal_chromium_process(
    chromium_executable_path: str,
) -> subprocess.Popen:  # Start Chromium the same way you would start it yourself
    chromium_command = [  # The command line used to start Chromium
        chromium_executable_path,  # The Chromium program itself
        f"--remote-debugging-port={CHROMIUM_DEBUGGING_PORT}",  # Allow Playwright to connect to this Chromium
        f"--user-data-dir={CHROMIUM_PROFILE_FOLDER}",  # Use this script's own profile folder
        f"--force-webrtc-ip-handling-policy={WEBRTC_IP_HANDLING_POLICY}",  # Do not expose local IPs through WebRTC
        "--no-first-run",  # Skip the first-run welcome screens
        "--no-default-browser-check",  # Do not ask to become the default browser
        "--disable-dev-shm-usage",  # Use disk instead of the small shared memory in containers, which prevents crashes
        "--window-size=1400,1000",  # Use a large window so the page lays out normally
    ]  # End of the command line
    if (
        hasattr(os, "geteuid") and os.geteuid() == 0
    ):  # Linux refuses to run Chromium as root without this switch
        logger.warning(
            "Running as root, adding --no-sandbox because Chromium requires it"
        )  # Tell the user
        chromium_command.append("--no-sandbox")  # Allow Chromium to start as root
    logger.info(
        "Starting Chromium: %s", " ".join(chromium_command)
    )  # Record the exact command
    chromium_process = subprocess.Popen(
        chromium_command
    )  # Start Chromium as a separate normal process
    return chromium_process  # Give the running process back to the caller


def wait_for_chromium_debugging_port(
    chromium_process: subprocess.Popen,
) -> bool:  # Wait until Chromium is ready
    debugging_address = f"http://127.0.0.1:{CHROMIUM_DEBUGGING_PORT}/json/version"  # Page Chromium serves when ready
    deadline_timestamp = (
        time.time() + CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS
    )  # Moment when we give up waiting
    while time.time() < deadline_timestamp:  # Keep checking until the deadline passes
        if (
            chromium_process.poll() is not None
        ):  # Check whether Chromium closed by itself
            logger.error(
                "Chromium exited early with code %s", chromium_process.returncode
            )  # Record the problem
            return False  # Chromium is not running
        try:  # The connection fails until Chromium is ready
            with urllib.request.urlopen(
                debugging_address, timeout=2
            ) as debugging_response:  # Ask Chromium
                logger.info(
                    "Chromium is ready (HTTP %s)", debugging_response.status
                )  # Record that it is ready
                return True  # Chromium is ready
        except OSError:  # Chromium is not accepting connections yet
            time.sleep(0.5)  # Wait a moment and ask again
    logger.error(
        "Chromium was not ready after %d seconds", CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS
    )  # Record the timeout
    return False  # Chromium never became ready


def stop_chromium_process(
    chromium_process: Optional[subprocess.Popen],
) -> None:  # Close the Chromium we started
    if (
        chromium_process is None or chromium_process.poll() is not None
    ):  # Nothing to stop
        return  # Leave the function
    logger.info("Stopping Chromium")  # Record the shutdown
    chromium_process.terminate()  # Ask Chromium to close
    try:  # Chromium may take a moment to close
        chromium_process.wait(timeout=10)  # Wait up to 10 seconds
    except subprocess.TimeoutExpired:  # Chromium did not close in time
        logger.warning(
            "Chromium did not close in time, forcing it to stop"
        )  # Record it
        chromium_process.kill()  # Force Chromium to stop


# ============================================================== 8. POLITE PACING AND BLOCK TRACKING
def pause_like_a_person(
    minimum_seconds: float, maximum_seconds: float, reason: str
) -> None:  # Random pause
    pause_seconds = random.uniform(
        minimum_seconds, maximum_seconds
    )  # Pick a random length inside the range
    logger.debug(
        "Pausing %.1f seconds (%s)", pause_seconds, reason
    )  # Record the pause and why it happens
    time.sleep(pause_seconds)  # Wait for that long


def register_access_block(
    run_statistics: dict,
) -> None:  # Count one more "Access Denied" in a row
    run_statistics["consecutive_blocks"] += 1  # Add one to the blocks-in-a-row counter
    logger.warning(  # Record the current count
        "Consecutive access blocks: %d/%d",  # Message template
        run_statistics["consecutive_blocks"],  # Blocks in a row so far
        MAXIMUM_CONSECUTIVE_BLOCKS,  # Number at which the script stops
    )  # End of the log call
    if (
        run_statistics["consecutive_blocks"] >= MAXIMUM_CONSECUTIVE_BLOCKS
    ):  # Check whether the limit is reached
        raise AccessBlockedError(  # Stop the whole run
            f"The website blocked {MAXIMUM_CONSECUTIVE_BLOCKS} requests in a row. "  # First half of the message
            "Stopping so the block does not get worse. Try again later or contact Agilent with the reference number."  # Advice
        )  # End of the error


def reset_access_blocks(
    run_statistics: dict,
) -> None:  # A page opened fine, so the blocks-in-a-row count restarts
    run_statistics["consecutive_blocks"] = 0  # Set the counter back to zero


# ============================================================== 9. PAGE CHECKS
def is_access_denied_page(
    page: Page,
) -> bool:  # Check whether the website blocked this visit
    try:  # Reading the page can fail while it is changing
        page_title = page.title()  # Read the page title
        page_source = page.content()  # Read the page source
    except PlaywrightError as read_error:  # The page could not be read
        logger.debug(
            "Could not read the page to check for Access Denied: %s", read_error
        )  # Record why
        return False  # Assume the page is fine
    lowercase_page_source = (
        page_source.lower()
    )  # Lowercase copy so the checks ignore capital letters
    access_denied_found = (  # Decide whether this is the "Access Denied" block page
        "access denied" in page_title.lower()  # The title says Access Denied
        or "errors.edgesuite.net"
        in lowercase_page_source  # The page links to the Akamai error site
        or "you don't have permission to access"
        in lowercase_page_source  # The usual block message
    )  # End of the check
    if access_denied_found:  # The website blocked us
        reference_match = re.search(
            r"Reference #([0-9a-fA-F.]+)", page_source
        )  # Look for the reference number
        reference_number = (
            reference_match.group(1) if reference_match else "unknown"
        )  # Read it if present
        logger.warning(
            "ACCESS DENIED page detected (reference %s) at %s",
            reference_number,
            page.url,
        )  # Record it
    return access_denied_found  # Tell the caller whether we were blocked


def is_page_unavailable_page(
    page: Page,
) -> bool:  # Check for the "page may no longer exist" error page
    try:  # Reading the page can fail while it is changing
        page_source = page.content()  # Read the page source
    except PlaywrightError as read_error:  # The page could not be read
        logger.debug(
            "Could not read the page to check for the unavailable message: %s",
            read_error,
        )  # Record why
        return False  # Assume the page is fine
    normalized_page_text = re.sub(
        r"\s+", " ", page_source.lower()
    )  # Lowercase and squeeze all whitespace to one space
    page_is_unavailable = (
        PAGE_UNAVAILABLE_MESSAGE in normalized_page_text
    )  # Look for the known error message
    if page_is_unavailable:  # The page is the error page
        logger.warning("PAGE UNAVAILABLE message found at %s", page.url)  # Record it
    return page_is_unavailable  # Tell the caller what we found


def is_results_page_usable(
    page: Page, run_statistics: dict
) -> bool:  # Check a result page before reading it
    if is_page_unavailable_page(
        page
    ):  # Check for the "page may no longer exist" message
        return False  # Skip this page
    if is_access_denied_page(page):  # Check for the "Access Denied" page
        register_access_block(
            run_statistics
        )  # Count the block, which may stop the whole run
        return False  # Skip this page
    reset_access_blocks(
        run_statistics
    )  # The page is fine, so the blocks-in-a-row count restarts
    return True  # The page can be read


# ============================================================== 10. PAGE ACTIONS
def wait_for_network_to_settle(
    page: Page,
) -> None:  # Wait until the page stops loading data
    try:  # Waiting can time out on pages that never stop loading
        page.wait_for_load_state(
            "networkidle", timeout=20000
        )  # Wait up to 20 seconds for quiet network
        logger.debug("Network became idle")  # Record that the page settled
    except PlaywrightTimeoutError:  # The page kept loading data
        logger.debug(
            "Network never became idle, continuing anyway"
        )  # Record that we gave up waiting
    time.sleep(2)  # Give JavaScript a little extra time to draw the results


def dismiss_cookie_banner(page: Page) -> None:  # Close the cookie popup if it shows up
    for cookie_button_selector in (
        "#onetrust-accept-btn-handler",
        "button[id*='accept' i]",
    ):  # Known buttons
        try:  # Ignore any problem finding or clicking the button
            accept_button = page.locator(
                cookie_button_selector
            ).first  # Find the first matching button
            if accept_button.is_visible():  # Only click it if it is visible
                accept_button.click(timeout=3000)  # Accept the cookies
                logger.info(
                    "Cookie banner dismissed using selector %s", cookie_button_selector
                )  # Record it
                time.sleep(0.5)  # Give the popup a moment to close
                return  # Stop after the first button that worked
        except PlaywrightError:  # The button was missing or not clickable
            continue  # Try the next selector
    logger.debug("No cookie banner found")  # Record that there was nothing to dismiss


def scroll_to_bottom_of_page(
    page: Page, pause_in_seconds: float = 0.8
) -> None:  # Load lazy content
    previous_page_height = 0  # Remember the page height from the previous round
    for scroll_round_number in range(1, 16):  # Scroll at most 15 times
        page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )  # Jump to the page bottom
        time.sleep(pause_in_seconds)  # Wait for more content to load
        current_page_height = page.evaluate(
            "document.body.scrollHeight"
        )  # Measure the page
        logger.debug(
            "Scroll round %d: page height %s", scroll_round_number, current_page_height
        )  # Record it
        if (
            current_page_height == previous_page_height
        ):  # Check whether the page stopped growing
            break  # Everything is loaded, so stop scrolling
        previous_page_height = (
            current_page_height  # Remember this height for the next comparison
        )


def extract_pdf_urls_from_page(
    page: Page,
) -> set:  # Find every PDF-looking URL on the page
    found_urls = set()  # Collects the URLs found on this page
    all_link_addresses = page.eval_on_selector_all(  # Read the full address of every link on the page
        "a[href]",  # Select every link that has an address
        "anchorElements => anchorElements.map(anchorElement => anchorElement.href)",  # JavaScript that reads them
    )  # End of the call
    logger.debug(
        "Page contains %d link(s) in total", len(all_link_addresses)
    )  # Record the link count
    for link_address in all_link_addresses:  # Check each link address
        if link_address.startswith("http") and PDF_OR_SDS_LINK_PATTERN.search(
            link_address
        ):  # Looks like a PDF
            found_urls.add(link_address)  # Keep it
    for raw_url_match in RAW_PDF_URL_PATTERN.findall(
        page.content()
    ):  # Search the raw page source too
        cleaned_url = raw_url_match.replace("\\u0026", "&").replace(
            "&amp;", "&"
        )  # Decode escaped ampersands
        found_urls.add(cleaned_url)  # Keep the cleaned URL
    return found_urls  # Give all URLs from this page back


def click_next_page_button(
    page: Page,
) -> bool:  # Go to the next result page if possible
    for (
        selector_text
    ) in NEXT_PAGE_BUTTON_SELECTORS:  # Try each known way of finding the button
        matching_locator = page.locator(
            selector_text
        )  # Find every element matching this selector
        try:  # Counting can fail if the page changes at the wrong moment
            matching_count = matching_locator.count()  # Count the matches
        except PlaywrightError as selector_error:  # The selector could not be used
            logger.debug(
                "Selector %s failed: %s", selector_text, selector_error
            )  # Record the problem
            continue  # Try the next selector
        logger.debug(
            "Selector %s matched %d element(s)", selector_text, matching_count
        )  # Record the match count
        for candidate_index in range(matching_count):  # Check each candidate
            candidate_button = matching_locator.nth(
                candidate_index
            )  # Pick the candidate by position
            try:  # The element may go stale or be covered
                if (
                    not candidate_button.is_visible()
                    or not candidate_button.is_enabled()
                ):  # Hidden or disabled
                    continue  # Skip this candidate
                if (  # Check the other ways a button can be marked as disabled
                    candidate_button.get_attribute("aria-disabled")
                    == "true"  # Marked disabled for accessibility
                    or "disabled"
                    in (
                        candidate_button.get_attribute("class") or ""
                    )  # Has a disabled CSS class
                ):  # End of the disabled checks
                    continue  # Skip this candidate
                candidate_button.scroll_into_view_if_needed(
                    timeout=5000
                )  # Bring the button into view
                candidate_button.click(timeout=5000)  # Click the next page button
                logger.info(
                    "Clicked next page button using selector %s", selector_text
                )  # Record the click
                return True  # Report that we moved to another page
            except PlaywrightError as click_error:  # The click did not work
                logger.debug(
                    "Click failed for selector %s: %s", selector_text, click_error
                )  # Record why
                continue  # Try the next candidate
    logger.info(
        "No usable next page button found"
    )  # Record that the last page was reached
    return False  # No usable next page button was found


def warm_up_session_on_home_page(
    page: Page,
) -> bool:  # Visit the home page and stay a short while first
    logger.info(
        "Warm-up: opening the home page %s", HOME_PAGE_URL
    )  # Record the first step
    try:  # Opening the page can fail
        page.goto(HOME_PAGE_URL, wait_until="domcontentloaded")  # Open the home page
    except PlaywrightError as navigation_error:  # The page could not be opened
        logger.error(
            "Warm-up: could not open the home page: %s", navigation_error
        )  # Record the error
        return False  # The warm-up failed
    wait_for_network_to_settle(page)  # Let the home page finish loading
    dismiss_cookie_banner(page)  # Accept the cookie popup like a normal visitor
    if is_access_denied_page(page):  # Check whether even the home page is blocked
        logger.error(
            "Warm-up: the home page itself shows Access Denied"
        )  # Record the problem
        return False  # The warm-up failed
    logger.info(
        "Warm-up: staying on the home page for %d seconds", HOME_PAGE_WARM_UP_SECONDS
    )  # Explain the wait
    for elapsed_seconds in range(
        1, HOME_PAGE_WARM_UP_SECONDS + 1
    ):  # Count the seconds one by one
        time.sleep(1)  # Wait one second
        logger.info(
            "Warm-up: %d/%d seconds", elapsed_seconds, HOME_PAGE_WARM_UP_SECONDS
        )  # Show the progress
    logger.info("Warm-up finished")  # Record that scraping can start
    return True  # The warm-up worked


def open_page_politely(
    page: Page, page_url: str, run_statistics: dict
) -> str:  # Open a page, backing off if blocked
    for attempt_number in range(
        1, MAXIMUM_ACCESS_DENIED_ATTEMPTS + 1
    ):  # Try a limited number of times
        logger.info(
            "Opening %s (attempt %d/%d)",
            page_url,
            attempt_number,
            MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        )  # Record
        try:  # Opening the page can fail
            page.goto(page_url, wait_until="domcontentloaded")  # Open the page
        except PlaywrightError as navigation_error:  # The page could not be opened
            logger.error(
                "Could not open %s: %s", page_url, navigation_error
            )  # Record the error
            return PAGE_STATUS_FAILED  # Give up on this page
        wait_for_network_to_settle(page)  # Wait for the page to finish loading
        if is_page_unavailable_page(
            page
        ):  # Check for the "page may no longer exist" message
            return PAGE_STATUS_UNAVAILABLE  # Skip this page, retrying will not help
        if not is_access_denied_page(page):  # Check whether the website let us in
            reset_access_blocks(run_statistics)  # The blocks-in-a-row count restarts
            return PAGE_STATUS_OPENED  # The page opened normally
        register_access_block(
            run_statistics
        )  # Count the block, which may stop the whole run
        if (
            attempt_number < MAXIMUM_ACCESS_DENIED_ATTEMPTS
        ):  # Check whether another attempt is allowed
            backoff_seconds = WAIT_AFTER_ACCESS_DENIED_IN_SECONDS * (
                2 ** (attempt_number - 1)
            ) + random.uniform(
                0, 5
            )  # Longer each time
            logger.info(
                "Backing off for %.0f seconds, then visiting the home page again",
                backoff_seconds,
            )  # Explain
            time.sleep(backoff_seconds)  # Wait before trying again
            warm_up_session_on_home_page(
                page
            )  # Visit the home page again to refresh the session
    logger.error(
        "Still blocked after %d attempt(s): %s",
        MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        page_url,
    )  # Record failure
    return PAGE_STATUS_BLOCKED  # The page could not be opened


# ============================================================== 11. DOWNLOADING
def start_chromium_download(
    page: Page, source_url: str
) -> tuple:  # Ask Chromium to download one URL
    received_downloads = []  # Collects the Download objects as Chromium reports them

    def remember_download(
        chromium_download: Download,
    ) -> None:  # Called by Playwright whenever a download starts
        received_downloads.append(
            chromium_download
        )  # Keep the download so we can use it later

    page.on("download", remember_download)  # Start listening for downloads on this tab
    try:  # Make sure the listener is always removed again
        try:  # Opening a download link can raise an error, which is normal
            navigation_response = page.goto(
                source_url,
                wait_until="commit",
                timeout=NAVIGATION_TIMEOUT_IN_MILLISECONDS,
            )  # Open
        except PlaywrightError as navigation_error:  # Chromium reported an error for the navigation
            navigation_message = str(navigation_error)  # Read the error text
            if (
                "Download is starting" in navigation_message
                or "ERR_ABORTED" in navigation_message
            ):  # Expected
                logger.debug(
                    "Navigation turned into a download (normal): %s", source_url
                )  # Record it
                navigation_response = None  # There is no web page, only a download
            else:  # Some other, real error
                raise  # Let the calling code handle it

        if (
            navigation_response is not None
            and "text/html"
            in navigation_response.headers.get("content-type", "").lower()
        ):  # HTML
            logger.debug(
                "The server returned a web page instead of a file: %s", source_url
            )  # Record it
            try:  # Waiting for the page can time out
                page.wait_for_load_state(
                    "domcontentloaded", timeout=15000
                )  # Let the page finish loading
            except PlaywrightTimeoutError:  # The page took too long
                logger.debug(
                    "The web page did not finish loading, checking what we have"
                )  # Record it
            if is_page_unavailable_page(
                page
            ):  # Check for the "page may no longer exist" message
                return None, OUTCOME_PAGE_UNAVAILABLE  # No PDF exists at this address
            if is_access_denied_page(page):  # Check for the "Access Denied" page
                return None, OUTCOME_ACCESS_DENIED  # The website blocked this request
            return None, OUTCOME_NOT_A_DOWNLOAD  # Some other web page, not a file

        deadline_timestamp = (
            time.time() + DOWNLOAD_TIMEOUT_IN_MILLISECONDS / 1000
        )  # Moment when we give up waiting
        while (
            not received_downloads and time.time() < deadline_timestamp
        ):  # Wait for Chromium to report the download
            page.wait_for_timeout(
                200
            )  # Wait a moment while letting Playwright process browser events
        if received_downloads:  # Chromium started a download
            return received_downloads[0], OUTCOME_DOWNLOAD_STARTED  # Hand it back
        logger.warning(
            "Chromium did not start a download within the time limit: %s", source_url
        )  # Record it
        return None, OUTCOME_TIMEOUT  # Nothing happened in time
    finally:  # Always runs, even after an error
        page.remove_listener(
            "download", remember_download
        )  # Stop listening for downloads


def download_pdf_urls(  # Download a list of PDF URLs, one after the other
    download_page: Page,  # Tab used only for downloading
    database_connection: sqlite3.Connection,  # Database that tracks URLs and files
    pdf_urls: list,  # The URLs to download now
    run_statistics: dict,  # Running totals that this function updates
) -> None:  # Nothing is returned, the totals are updated in place
    if not pdf_urls:  # Check whether there is anything to do
        logger.debug("No PDF URLs to download in this batch")  # Record it
        return  # Leave the function

    has_made_a_request = (
        False  # Tracks whether we already contacted the website in this batch
    )
    for url_position, source_url in enumerate(
        pdf_urls, start=1
    ):  # Handle each URL one at a time
        logger.info(
            "(%d/%d) Processing %s", url_position, len(pdf_urls), source_url
        )  # Record the progress
        guessed_file_name = guess_file_name_from_url(
            source_url
        )  # Predict the name when the URL shows it
        if (
            guessed_file_name is not None
        ):  # Only check when the URL gave us a trustworthy name
            guessed_safe_file_name = build_safe_file_name(
                guessed_file_name
            )  # Apply the naming rules
            if (  # Skip the download when the predicted file already exists
                is_file_name_already_downloaded(
                    database_connection, guessed_safe_file_name
                )  # Known in database
                or (
                    DOWNLOAD_FOLDER / guessed_safe_file_name
                ).exists()  # Or already present on disk
            ):  # End of the skip condition
                logger.info(
                    "  SKIP: %s already exists", guessed_safe_file_name
                )  # Explain the skip
                mark_url_as_processed(
                    database_connection, source_url
                )  # Remember this URL is finished
                run_statistics["skipped"] += 1  # Count the skip
                continue  # Move on to the next URL

        if has_made_a_request:  # Only pause between two real requests
            pause_like_a_person(
                *PAUSE_BETWEEN_DOWNLOADS_IN_SECONDS, "between downloads"
            )  # Random short pause
        has_made_a_request = True  # From now on we have contacted the website

        try:  # Starting a download can raise browser errors
            chromium_download, download_outcome = start_chromium_download(
                download_page, source_url
            )  # Let Chromium download it
        except PlaywrightError as download_error:  # A real browser error happened
            if is_browser_closed_error(
                download_error
            ):  # Check whether Chromium itself has closed
                raise  # Let the restart logic deal with it, this URL stays pending
            logger.error(
                "  FAILED to open %s: %s",
                source_url,
                str(download_error).splitlines()[0],
            )  # Record it
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL

        if (
            download_outcome == OUTCOME_PAGE_UNAVAILABLE
        ):  # The link led to the "page may no longer exist" message
            logger.warning(
                "  SKIP: page unavailable, no PDF at %s", source_url
            )  # Explain the skip
            mark_url_as_processed(
                database_connection, source_url
            )  # Never try this URL again
            run_statistics["unavailable"] += 1  # Count it as unavailable
            reset_access_blocks(
                run_statistics
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if (
            download_outcome == OUTCOME_ACCESS_DENIED
        ):  # The website blocked this request
            logger.error(
                "  FAILED: access denied for %s", source_url
            )  # Record the failure
            run_statistics["failed"] += 1  # Count the failure
            register_access_block(
                run_statistics
            )  # Count the block, which may stop the whole run
            continue  # Move on to the next URL
        if (
            download_outcome == OUTCOME_NOT_A_DOWNLOAD
        ):  # The link is an ordinary web page, not a PDF
            logger.warning(
                "  SKIP: the link is a web page, not a PDF: %s", source_url
            )  # Explain the skip
            mark_url_as_processed(
                database_connection, source_url
            )  # Never try this URL again
            run_statistics["skipped"] += 1  # Count it as skipped
            reset_access_blocks(
                run_statistics
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if chromium_download is None:  # Chromium never produced a download
            logger.error(
                "  FAILED: no download (%s) for %s", download_outcome, source_url
            )  # Record the failure
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL
        reset_access_blocks(
            run_statistics
        )  # A download started, so the blocks-in-a-row count restarts

        suggested_file_name = (
            chromium_download.suggested_filename
        )  # The file name Chromium would have used
        logger.info(
            "  Chromium suggested file name: %s", suggested_file_name
        )  # Record it
        download_failure_reason = (
            chromium_download.failure()
        )  # Wait for the download to end and get any error
        if download_failure_reason:  # Chromium reported that the download failed
            logger.error(
                "  FAILED: Chromium download error '%s' for %s",
                download_failure_reason,
                source_url,
            )  # Log
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL

        temporary_file_path = (
            chromium_download.path()
        )  # Where Chromium put the finished file for now
        with open(
            temporary_file_path, "rb"
        ) as temporary_file:  # Open the downloaded file for reading
            first_file_bytes = temporary_file.read(4)  # Read the first four bytes
        if first_file_bytes != b"%PDF":  # Real PDF files always start with %PDF
            logger.error(
                "  FAILED: downloaded file is not a PDF: %s", source_url
            )  # Record the problem
            chromium_download.delete()  # Throw the file away
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL

        safe_file_name = build_safe_file_name(
            suggested_file_name
        )  # Apply the naming rules to the real name
        safe_file_path = (
            DOWNLOAD_FOLDER / safe_file_name
        )  # Full path where the PDF will be saved
        logger.debug(
            "  Safe file name: %s (from %s)", safe_file_name, suggested_file_name
        )  # Record the cleaning
        if (  # Decide whether this exact file name was already saved
            is_file_name_already_downloaded(
                database_connection, safe_file_name
            )  # Known in the database
            or safe_file_path.exists()  # Or a file with this name is already on disk
        ):  # End of the duplicate check
            logger.info(
                "  DUPLICATE file name, not saved: %s", safe_file_name
            )  # Explain what happens
            chromium_download.delete()  # Discard the extra copy
            mark_url_as_processed(
                database_connection, source_url
            )  # Remember this URL is finished
            run_statistics["skipped"] += 1  # Count it as skipped
            continue  # Move on to the next URL

        chromium_download.save_as(safe_file_path)  # Save the PDF into the PDFs folder
        record_downloaded_file(
            database_connection, safe_file_name, source_url
        )  # Save the file in the database
        mark_url_as_processed(
            database_connection, source_url
        )  # Remember this URL is finished
        run_statistics["downloaded"] += 1  # Count the success
        logger.info(
            "  SAVED: %s (%d bytes)", safe_file_path, safe_file_path.stat().st_size
        )  # Record the result

    log_run_statistics(
        run_statistics, "Totals so far"
    )  # Show the totals after this batch


# ============================================================== 12. SCRAPING
def collect_and_download_pdf_urls_for_keyword(  # Scrape one keyword and download each page's PDFs right away
    page: Page,  # Tab used for browsing the search results
    download_page: Page,  # Second tab used only for downloading
    database_connection: sqlite3.Connection,  # Database that tracks URLs and files
    search_keyword: str,  # Keyword to search for
    run_statistics: dict,  # Running totals of downloaded, skipped and failed files
) -> set:  # Returns every URL found for this keyword
    search_url = SEARCH_URL_TEMPLATE.format(
        search_keyword=search_keyword
    )  # Build the search address
    logger.info(
        "Opening search page for keyword '%s': %s", search_keyword, search_url
    )  # Record it
    page_status = open_page_politely(
        page, search_url, run_statistics
    )  # Open the page, backing off if blocked
    if page_status != PAGE_STATUS_OPENED:  # Check whether the page opened normally
        logger.warning(
            "[%s] skipping this keyword (page status: %s)", search_keyword, page_status
        )  # Explain the skip
        return set()  # Nothing was collected, so go on to the next keyword
    dismiss_cookie_banner(page)  # Close the cookie popup if present

    all_urls_for_keyword = (
        set()
    )  # Collects the URLs from every result page of this keyword
    for result_page_number in range(
        1, MAXIMUM_RESULT_PAGES_PER_KEYWORD + 1
    ):  # Walk through the result pages
        page.bring_to_front()  # Keep the results tab in front so the browser does not slow it down
        scroll_to_bottom_of_page(page)  # Make sure all results on this page are loaded
        urls_on_this_page = extract_pdf_urls_from_page(
            page
        )  # Read the URLs from this page
        new_urls_on_this_page = (
            urls_on_this_page - all_urls_for_keyword
        )  # Work out which ones are new
        all_urls_for_keyword |= (
            urls_on_this_page  # Add this page's URLs to the keyword total
        )
        logger.info(  # Show progress on the screen
            "[%s] page %d: %d PDF link(s), %d new, %d total so far",  # Message template
            search_keyword,  # Current keyword
            result_page_number,  # Current page number
            len(urls_on_this_page),  # Links found on this page
            len(new_urls_on_this_page),  # Links not seen before
            len(all_urls_for_keyword),  # Running total for this keyword
        )  # End of the log call
        for new_url in sorted(new_urls_on_this_page):  # List each new link
            logger.debug("  found: %s", new_url)  # Record the link
        if new_urls_on_this_page:  # Only touch the database when there is something new
            save_collected_urls(
                database_connection, new_urls_on_this_page
            )  # Store the URLs in the database
            urls_to_download_now = filter_unprocessed_urls(
                database_connection, sorted(new_urls_on_this_page)
            )  # Pending
            logger.info(
                "[%s] page %d: downloading %d PDF(s) right away",
                search_keyword,
                result_page_number,
                len(urls_to_download_now),
            )  # Say
            download_pdf_urls(
                download_page, database_connection, urls_to_download_now, run_statistics
            )  # Download now
        if (
            result_page_number > 1 and not new_urls_on_this_page
        ):  # Check whether paging gave us nothing new
            logger.info(
                "[%s] no new links on this page, stopping", search_keyword
            )  # Explain why we stop
            break  # Stop, because we are probably seeing the same page again
        pause_like_a_person(
            *PAUSE_BETWEEN_RESULT_PAGES_IN_SECONDS, "before the next result page"
        )  # Random short pause
        page.bring_to_front()  # Return to the results tab before looking for the next page button
        if not click_next_page_button(page):  # Try to go to the next page
            break  # There is no next page, so this keyword is finished
        wait_for_network_to_settle(page)  # Wait for the next page of results to load
        if not is_results_page_usable(
            page, run_statistics
        ):  # Check for the error page or "Access Denied"
            logger.warning(
                "[%s] the next result page is not usable, moving on", search_keyword
            )  # Explain the skip
            break  # Skip the rest of this keyword and go to the next one
    logger.info(
        "[%s] finished with %d PDF link(s)", search_keyword, len(all_urls_for_keyword)
    )  # Keyword summary
    return all_urls_for_keyword  # Give back every URL found for this keyword


# ============================================================== 13. BROWSER SESSION AND RESTARTS
def is_browser_closed_error(
    playwright_error: Exception,
) -> bool:  # Check whether an error means Chromium is gone
    error_message = str(
        playwright_error
    ).lower()  # Lowercase the error text so the comparison ignores capitals
    return any(
        closed_text in error_message for closed_text in BROWSER_CLOSED_MESSAGES
    )  # True if any known text matches


class BrowserSession:  # Owns Chromium and both tabs, and can restart them if Chromium closes
    def __init__(
        self, playwright_instance, chromium_executable_path: str
    ) -> None:  # Remember how to start Chromium
        self.playwright_instance = playwright_instance  # The running Playwright
        self.chromium_executable_path = (
            chromium_executable_path  # Where Chromium is installed
        )
        self.chromium_process = None  # The Chromium program, once it has been started
        self.connected_browser = None  # Playwright's connection to Chromium
        self.results_page = None  # Tab used for browsing the search results
        self.download_page = None  # Tab used only for downloading
        self.restart_count = 0  # How many times Chromium had to be restarted

    def start(self) -> None:  # Start Chromium and open both tabs
        self.chromium_process = start_normal_chromium_process(
            self.chromium_executable_path
        )  # Start it as a normal window
        if not wait_for_chromium_debugging_port(
            self.chromium_process
        ):  # Wait until Chromium is ready for Playwright
            raise RuntimeError(
                "Chromium did not become ready in time"
            )  # Stop with a clear message
        logger.info(
            "Connecting Playwright to the running Chromium on port %d",
            CHROMIUM_DEBUGGING_PORT,
        )  # Record it
        self.connected_browser = self.playwright_instance.chromium.connect_over_cdp(  # Attach to the running Chromium
            f"http://127.0.0.1:{CHROMIUM_DEBUGGING_PORT}"  # Local address Chromium is listening on
        )  # End of the connect call
        browser_context = self.connected_browser.contexts[
            0
        ]  # Use Chromium's normal browsing context
        self.results_page = (
            browser_context.pages[0]
            if browser_context.pages
            else browser_context.new_page()
        )  # First tab
        self.results_page.set_default_timeout(
            NAVIGATION_TIMEOUT_IN_MILLISECONDS
        )  # Default time limit for page actions
        attach_browser_event_logging(
            self.results_page
        )  # Start logging everything the results tab does
        self.download_page = (
            browser_context.new_page()
        )  # Open a second tab that is used only for downloading
        self.download_page.set_default_timeout(
            NAVIGATION_TIMEOUT_IN_MILLISECONDS
        )  # Default time limit for it
        attach_browser_event_logging(
            self.download_page
        )  # Start logging everything the download tab does
        self.results_page.bring_to_front()  # Show the results tab again

    def stop(self) -> None:  # Disconnect Playwright and close Chromium
        if self.connected_browser is not None:  # Check whether we are connected
            try:  # The connection may already be dead
                logger.info(
                    "Disconnecting Playwright from Chromium"
                )  # Record the disconnect
                self.connected_browser.close()  # Disconnect Playwright from Chromium
            except PlaywrightError as disconnect_error:  # Chromium was already gone
                logger.debug(
                    "Disconnect reported an error (ignored): %s", disconnect_error
                )  # Record it
        stop_chromium_process(
            self.chromium_process
        )  # Close the Chromium program if it is still running

    def restart(
        self,
    ) -> None:  # Start a fresh Chromium after the old one closed or crashed
        self.restart_count += 1  # Count this restart
        if (
            self.restart_count > MAXIMUM_BROWSER_RESTARTS
        ):  # Check whether we have restarted too often
            raise RuntimeError(
                f"Chromium closed more than {MAXIMUM_BROWSER_RESTARTS} times, giving up"
            )  # Stop the run
        exit_code = (
            self.chromium_process.poll() if self.chromium_process is not None else None
        )  # How Chromium ended
        logger.error(  # Record what happened to Chromium
            "Chromium problem detected. Process exit code: %s (None = still running, -9 = killed, -11 = crashed)",  # Text
            exit_code,  # The exit code
        )  # End of the log call
        logger.info(
            "Restarting Chromium (restart %d/%d)",
            self.restart_count,
            MAXIMUM_BROWSER_RESTARTS,
        )  # Record it
        self.stop()  # Close whatever is left of the old Chromium
        time.sleep(3)  # Give the system a moment to free the debugging port
        self.start()  # Start a fresh Chromium with two new tabs
        warm_up_session_on_home_page(
            self.results_page
        )  # Visit the home page again to refresh the session


def run_with_browser_restarts(
    browser_session: BrowserSession, step_description: str, step_function
) -> None:  # Run
    while True:  # Keep trying until the step finishes or we give up
        try:  # The step may fail if Chromium closes
            step_function()  # Run the step
            return  # The step finished, so we are done
        except PlaywrightError as playwright_error:  # A browser error happened
            if is_browser_closed_error(
                playwright_error
            ):  # Check whether Chromium or its tab closed
                logger.error(
                    "Chromium closed during %s: %s",
                    step_description,
                    str(playwright_error).splitlines()[0],
                )  # Log
                browser_session.restart()  # Start a new Chromium, which raises an error if restarts ran out
                continue  # Run the same step again, finished URLs are skipped through the database
            logger.exception(
                "Browser error during %s, skipping this step", step_description
            )  # Log the full error
            return  # Go on with the next step instead of crashing the whole run


# ============================================================== 14. MAIN PROGRAM
def log_run_statistics(
    run_statistics: dict, label: str
) -> None:  # Print the running totals
    logger.info(  # Write one summary line
        "%s: downloaded %d | skipped %d | page unavailable %d | failed %d",  # Message template
        label,  # Text shown before the numbers
        run_statistics["downloaded"],  # Number of new files
        run_statistics["skipped"],  # Number of skipped URLs
        run_statistics["unavailable"],  # Number of links whose page no longer exists
        run_statistics["failed"],  # Number of failures
    )  # End of the log call


def run_downloader() -> None:  # Run the whole job from start to finish
    configure_console_logging()  # Turn on console logging first so everything is shown
    logger.info("Download folder: %s", DOWNLOAD_FOLDER)  # Record where PDFs are saved
    logger.info(
        "Keywords: %s", " ".join(SEARCH_KEYWORDS)
    )  # Record which keywords will be searched
    DOWNLOAD_FOLDER.mkdir(exist_ok=True)  # Make sure the PDFs folder exists
    database_connection = connect_to_database()  # Open the SQLite database
    create_database_tables(database_connection)  # Create the tables on the first run
    register_existing_pdf_files(
        database_connection
    )  # Remember PDFs that are already in the folder
    prepare_chromium_profile_preferences()  # Make Chromium download PDFs instead of previewing them
    run_statistics = {  # Running totals for the whole run
        "downloaded": 0,  # Files saved
        "skipped": 0,  # URLs skipped because the file already exists
        "unavailable": 0,  # Links whose page says it no longer exists
        "failed": 0,  # URLs that did not produce a PDF
        "consecutive_blocks": 0,  # "Access Denied" answers in a row
    }  # End of the statistics
    try:  # Make sure the database is closed even if something fails
        with sync_playwright() as playwright_instance:  # Start Playwright
            chromium_executable_path = find_chromium_executable_path(
                playwright_instance
            )  # Find Playwright's Chromium
            browser_session = BrowserSession(
                playwright_instance, chromium_executable_path
            )  # Prepare the session
            try:  # Make sure Chromium is always closed at the end
                browser_session.start()  # Start Chromium and open both tabs
                if not warm_up_session_on_home_page(
                    browser_session.results_page
                ):  # Visit the home page first
                    logger.warning(
                        "Warm-up did not finish cleanly, continuing anyway"
                    )  # Record the problem
                for keyword_position, search_keyword in enumerate(
                    SEARCH_KEYWORDS, start=1
                ):  # Go through a-z and 0-9
                    if keyword_position > 1:  # Only pause between two keywords
                        pause_like_a_person(
                            *PAUSE_BETWEEN_KEYWORDS_IN_SECONDS,
                            "before the next keyword",
                        )  # Pause
                    run_with_browser_restarts(  # Run the keyword, restarting Chromium if it closes
                        browser_session,  # The session that can be restarted
                        f"keyword '{search_keyword}'",  # Description used in log messages
                        lambda: collect_and_download_pdf_urls_for_keyword(  # The work to do for this keyword
                            browser_session.results_page,  # Current results tab
                            browser_session.download_page,  # Current download tab
                            database_connection,  # Database that tracks URLs and files
                            search_keyword,  # Keyword to search for
                            run_statistics,  # Running totals
                        ),  # End of the keyword work
                    )  # End of the restart wrapper
                logger.info("Retrying URLs that are still pending")  # Record the retry
                run_with_browser_restarts(  # Retry pending URLs, restarting Chromium if it closes
                    browser_session,  # The session that can be restarted
                    "the retry of pending URLs",  # Description used in log messages
                    lambda: download_pdf_urls(  # The work to do
                        browser_session.download_page,  # Current download tab
                        database_connection,  # Database that tracks URLs and files
                        load_unprocessed_urls(
                            database_connection
                        ),  # Pending URLs, read fresh each attempt
                        run_statistics,  # Running totals
                    ),  # End of the download work
                )  # End of the restart wrapper
            finally:  # Always runs, even after an error
                browser_session.stop()  # Disconnect Playwright and close Chromium
    except AccessBlockedError as access_blocked_error:  # The website blocked us too many times in a row
        logger.error(
            "STOPPED EARLY: %s", access_blocked_error
        )  # Explain why the run ended
    except Exception:  # Anything unexpected that escaped the code above
        logger.exception(
            "Unexpected error, stopping"
        )  # Log the full error with its traceback
        raise  # Pass the error on so the script exits with an error code
    finally:  # Always runs at the very end
        log_run_statistics(run_statistics, "FINAL")  # Show the final totals
        database_connection.close()  # Close the database
        logger.info("Database closed, finished")  # Record the final step


if __name__ == "__main__":  # Only run when this file is executed directly
    run_downloader()  # Start the job
