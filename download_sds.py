#!/usr/bin/env python3  # Run this file with Python 3 when executed directly on Linux or macOS
# Download all SDS / MSDS PDFs from Agilent's chemical-standards search page using Chromium via Playwright.
#
# How it works:
#   1. Starts your installed Chromium as a normal window and attaches Playwright to it.
#   2. Visits the Agilent home page briefly, then searches the keywords a-z and 0-9.
#   3. After every result page it downloads the PDFs found there right away. The second tab used for
#      downloading is opened only when something needs downloading, and closed again afterwards.
#   4. A SQLite database remembers every URL and file name, so nothing is downloaded twice.
#   5. If Chromium crashes or closes, it is restarted and the work continues where it stopped.
#   6. Pages that say "The page may be temporarily unavailable or may no longer exist" are skipped.
#   7. Random pauses, back-off waits and a stop-after-repeated-blocks rule keep the traffic gentle.
#   8. A background check pings 1.1.1.1 and 8.8.8.8. If neither answers, Chromium is closed and only
#      started again (and scraping resumed) once the internet is back.
#   9. Only USA English files are downloaded (file names ending in _NAEnglish.pdf).
#  10. Tabs: only the results tab stays open; the download tab exists only while downloading, and any
#      other tab or popup is closed at once.
#  11. Everything that happens is logged to the console.
#
# Setup: pip install playwright   and then   playwright install chromium
# Run:   python download_sds.py
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
#  13. Internet monitor
#  14. Browser session and restarts
#  15. Main program

# ============================================================== 1. IMPORTS
import json  # Reads and writes the Chromium preferences file
import logging  # Prints detailed log lines to the console
import os  # Reads environment variables and checks whether files exist
import random  # Creates random pause lengths
import re  # Regular expressions for link matching and text cleaning
import shutil  # Finds programs such as ping on the system PATH
import socket  # Opens a plain network connection when ping is not available
import sqlite3  # Built-in SQLite database used to remember what was already downloaded
import string  # Provides the lowercase letters and digits used as search keywords
import subprocess  # Starts the Chromium program as a normal process
import sys  # Gives access to the console output stream
import threading  # Runs the internet check in the background
import time  # Provides sleep() and the current time
from contextlib import contextmanager  # Lets a method be used inside a "with" block
from pathlib import Path  # Object-oriented file system paths
from typing import (
    Iterator,
    Optional,
)  # Type hints for generators and for values that may be None
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
REQUIRED_FILE_NAME_ENDING = "_NAEnglish.pdf"  # Only files whose name ends like this (USA English) are downloaded

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

# ---- Internet check
INTERNET_CHECK_HOSTS = (
    "1.1.1.1",
    "8.8.8.8",
)  # Addresses that are pinged to find out whether the internet works
INTERNET_CHECK_INTERVAL_IN_SECONDS = 10  # How often the background check runs
PING_TIMEOUT_IN_SECONDS = 5  # How long to wait for one ping answer
TCP_FALLBACK_PORT = 53  # Port used for a plain connection test when ping is missing or blocked (DNS port)
TCP_FALLBACK_TIMEOUT_IN_SECONDS = 2  # How long to wait for that connection test

# ---- Chromium settings
WEBRTC_IP_HANDLING_POLICY = "default_public_interface_only"  # Hides local IPs from WebRTC (or "disable_non_proxied_udp")
CHROMIUM_DEBUGGING_PORT = 0  # Port Playwright uses to talk to Chromium (0 = Chromium picks a free port by itself)
CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS = (
    60  # How long to wait for Chromium to be ready after starting it
)
CHROMIUM_DISABLE_SANDBOX_IF_NEEDED = True  # Start again with --no-sandbox when this system does not allow Chromium's sandbox

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


class InternetOutageError(
    Exception
):  # Raised when the internet is down and Chromium must wait for it
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


def ignore_non_usa_english_urls(
    database_connection: sqlite3.Connection,
) -> None:  # Drop old pending URLs we no longer want
    ignored_count = 0  # Counts how many pending URLs are ignored
    for pending_url in load_unprocessed_urls(
        database_connection
    ):  # Look at every URL that is still pending
        if not is_usa_english_pdf_url(
            pending_url
        ):  # Check whether it is NOT a USA English file
            mark_url_as_processed(
                database_connection, pending_url
            )  # Mark it as done so it is never downloaded
            ignored_count += 1  # Count it
    logger.info(
        "Ignored %d pending URL(s) that do not end in %s",
        ignored_count,
        REQUIRED_FILE_NAME_ENDING,
    )  # Report


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


def is_usa_english_pdf_url(
    source_url: str,
) -> bool:  # Check whether a URL points to a USA English file
    url_path = unquote(
        urlparse(source_url).path
    )  # Take the path part of the URL, without the ?query
    return url_path.lower().endswith(
        REQUIRED_FILE_NAME_ENDING.lower()
    )  # True when the name ends in _NAEnglish.pdf


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


def allow_local_connections_without_proxy() -> None:  # Make sure a proxy setting cannot break the link to Chromium
    local_addresses = [
        "127.0.0.1",
        "localhost",
        "::1",
    ]  # Addresses that must never go through a proxy
    for variable_name in (
        "NO_PROXY",
        "no_proxy",
    ):  # Both spellings are used by different programs
        existing_value = os.environ.get(variable_name, "")  # Read the current value
        existing_entries = [
            entry.strip() for entry in existing_value.split(",") if entry.strip()
        ]  # Split it into entries
        for local_address in local_addresses:  # Check each local address
            if (
                local_address not in existing_entries
            ):  # The address is missing from the list
                existing_entries.append(local_address)  # Add it
        os.environ[variable_name] = ",".join(existing_entries)  # Save the new value
    logger.debug(
        "NO_PROXY is now: %s", os.environ["NO_PROXY"]
    )  # Record the final value


def forward_chromium_output_to_log(
    chromium_process: subprocess.Popen,
) -> None:  # Copy Chromium's own messages into our log
    if chromium_process.stdout is None:  # Check whether Chromium's output is available
        return  # Nothing to copy
    for (
        output_line
    ) in chromium_process.stdout:  # Read Chromium's output line by line until it closes
        logger.debug(
            "CHROMIUM OUTPUT: %s", output_line.rstrip()
        )  # Write the line to the log
        chromium_process.recent_output_lines.append(
            output_line.rstrip()
        )  # Keep the line for later checks
        del chromium_process.recent_output_lines[
            :-200
        ]  # Keep only the latest 200 lines


def read_debugging_endpoint_from_profile() -> Optional[
    str
]:  # Read the address Chromium publishes when it is ready
    active_port_file_path = (
        CHROMIUM_PROFILE_FOLDER / "DevToolsActivePort"
    )  # File Chromium writes once it is listening
    if not active_port_file_path.exists():  # Chromium has not written it yet
        return None  # Not ready
    try:  # Reading can fail while Chromium is still writing
        file_lines = active_port_file_path.read_text(
            encoding="utf-8"
        ).splitlines()  # Read the file line by line
    except OSError:  # The file could not be read right now
        return None  # Try again later
    if (
        len(file_lines) < 2 or not file_lines[0].strip().isdigit()
    ):  # The file is incomplete
        return None  # Try again later
    return f"ws://127.0.0.1:{int(file_lines[0].strip())}{file_lines[1].strip()}"  # Line 1 is the port, line 2 the path


def start_normal_chromium_process(
    chromium_executable_path: str, use_no_sandbox: bool = False
) -> subprocess.Popen:  # Start Chromium
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
    running_as_root = (
        hasattr(os, "geteuid") and os.geteuid() == 0
    )  # Linux refuses to run Chromium as root with its sandbox
    if running_as_root:  # Chromium needs --no-sandbox when it runs as root
        logger.warning(
            "Running as root, adding --no-sandbox because Chromium requires it"
        )  # Tell the user
    if running_as_root or use_no_sandbox:  # The sandbox must be switched off
        if (
            use_no_sandbox and not running_as_root
        ):  # The reason is not root, but a system that blocks the sandbox
            logger.warning(
                "Starting Chromium WITHOUT its security sandbox (--no-sandbox) because this system blocks it"
            )  # Warn
        chromium_command.append("--no-sandbox")  # Switch the sandbox off
    if (
        sys.platform.startswith("linux")
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    ):  # No screen
        logger.warning(
            "No display found (DISPLAY and WAYLAND_DISPLAY are not set), Chromium cannot open a window"
        )  # Warn
    else:  # A display seems to be available
        logger.debug(
            "Display settings: DISPLAY=%s WAYLAND_DISPLAY=%s",
            os.environ.get("DISPLAY"),
            os.environ.get("WAYLAND_DISPLAY"),
        )  # Record
    CHROMIUM_PROFILE_FOLDER.mkdir(
        parents=True, exist_ok=True
    )  # Make sure the profile folder exists
    (CHROMIUM_PROFILE_FOLDER / "DevToolsActivePort").unlink(
        missing_ok=True
    )  # Remove the old file so we never read stale data
    logger.info(
        "Starting Chromium: %s", " ".join(chromium_command)
    )  # Record the exact command
    chromium_process = subprocess.Popen(  # Start Chromium as a separate normal process
        chromium_command,  # The command line
        stdout=subprocess.PIPE,  # Capture Chromium's normal output
        stderr=subprocess.STDOUT,  # Capture Chromium's error output in the same stream
        text=True,  # Read the output as text
        errors="replace",  # Never fail on strange characters
        bufsize=1,  # Deliver the output line by line
    )  # End of the start call
    chromium_process.recent_output_lines = (
        []
    )  # Keeps Chromium's messages so we can look for known problems
    chromium_process.output_thread = (
        threading.Thread(  # Copy Chromium's messages into our log in the background
            target=forward_chromium_output_to_log,  # The function that does the copying
            args=(chromium_process,),  # The process to read from
            name="chromium-output",  # A name that shows up in debugging tools
            daemon=True,  # Do not keep the script alive because of this thread
        )
    )  # End of the thread definition
    chromium_process.output_thread.start()  # Start the thread
    return chromium_process  # Give the running process back to the caller


def chromium_failed_because_of_sandbox(
    chromium_process: subprocess.Popen,
) -> bool:  # Did Chromium stop over its sandbox?
    chromium_process.output_thread.join(
        timeout=3
    )  # Give the output thread a moment to read the last messages
    return any(  # Look through everything Chromium printed
        "no usable sandbox"
        in output_line.lower()  # The message Chromium prints when its sandbox cannot start
        for output_line in chromium_process.recent_output_lines  # Check each saved line
    )  # End of the search


def wait_for_chromium_debugging_endpoint(
    chromium_process: subprocess.Popen,
) -> Optional[str]:  # Wait until Chromium is ready
    started_timestamp = time.time()  # Moment when we started waiting
    deadline_timestamp = (
        started_timestamp + CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS
    )  # Moment when we give up waiting
    next_report_timestamp = (
        started_timestamp + 5
    )  # Moment of the next "still waiting" message
    while time.time() < deadline_timestamp:  # Keep checking until the deadline passes
        if (
            chromium_process.poll() is not None
        ):  # Check whether Chromium closed by itself
            logger.error(
                "Chromium exited early with code %s, see the CHROMIUM OUTPUT lines above",
                chromium_process.returncode,
            )  # Say
            return None  # Chromium is not running
        debugging_endpoint = (
            read_debugging_endpoint_from_profile()
        )  # Look for the address Chromium publishes
        if debugging_endpoint is not None:  # Chromium is ready
            logger.info(
                "Chromium is ready after %.1f seconds: %s",
                time.time() - started_timestamp,
                debugging_endpoint,
            )  # Record
            return debugging_endpoint  # Hand the address back
        if time.time() >= next_report_timestamp:  # Time for a progress message
            logger.info(
                "Still waiting for Chromium to start (%.0f seconds so far, the process is running)",
                time.time() - started_timestamp,
            )  # Say
            next_report_timestamp += 5  # Schedule the next message
        time.sleep(0.5)  # Wait a moment and look again
    logger.error(
        "Chromium was not ready after %d seconds, but its process is still running",
        CHROMIUM_STARTUP_TIMEOUT_IN_SECONDS,
    )  # Say
    logger.error(
        "Check the CHROMIUM OUTPUT lines above for the reason. Common causes: no display, a damaged profile folder, or a very slow disk"
    )  # Hint
    return None  # Chromium never became ready


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
    usa_english_urls = {
        found_url for found_url in found_urls if is_usa_english_pdf_url(found_url)
    }  # Keep USA English only
    ignored_url_count = len(found_urls) - len(
        usa_english_urls
    )  # Count the links that are ignored
    logger.debug(
        "Ignored %d link(s) that do not end in %s",
        ignored_url_count,
        REQUIRED_FILE_NAME_ENDING,
    )  # Record it
    return usa_english_urls  # Give only the USA English URLs back


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


def is_url_already_downloaded_by_name(
    database_connection: sqlite3.Connection, source_url: str
) -> bool:  # Name check
    guessed_file_name = guess_file_name_from_url(
        source_url
    )  # Predict the name when the URL shows it
    if guessed_file_name is None:  # The URL does not reveal a trustworthy file name
        return False  # We cannot tell, so assume it still needs downloading
    guessed_safe_file_name = build_safe_file_name(
        guessed_file_name
    )  # Apply the naming rules
    return (  # The file counts as downloaded when it is known in either place
        is_file_name_already_downloaded(
            database_connection, guessed_safe_file_name
        )  # Known in the database
        or (
            DOWNLOAD_FOLDER / guessed_safe_file_name
        ).exists()  # Or already present on disk
    )  # End of the check


def download_pdf_urls(  # Download a list of PDF URLs, one after the other
    download_page: Page,  # Tab used only for downloading
    database_connection: sqlite3.Connection,  # Database that tracks URLs and files
    pdf_urls: list,  # The URLs to download now
    run_statistics: dict,  # Running totals that this function updates
    internet_monitor: "InternetMonitor",  # Tells us whether the internet works
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
        internet_monitor.raise_if_internet_is_down()  # Stop here and wait if the internet is down
        if is_url_already_downloaded_by_name(
            database_connection, source_url
        ):  # Skip when the predicted file exists
            logger.info(
                "  SKIP: the file for this URL already exists"
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
            if (
                internet_monitor.confirm_internet_is_down()
            ):  # Check whether the internet being down caused the error
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
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
            if (
                internet_monitor.confirm_internet_is_down()
            ):  # Check whether the internet being down is the reason
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
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


def download_pdf_urls_in_temporary_tab(  # Download URLs, opening the second tab only if it is really needed
    browser_session: "BrowserSession",  # Owns the tabs and can open the temporary download tab
    database_connection: sqlite3.Connection,  # Database that tracks URLs and files
    pdf_urls: list,  # The URLs that might need downloading
    run_statistics: dict,  # Running totals that this function updates
) -> None:  # Nothing is returned, the totals are updated in place
    urls_needing_download = []  # Collects the URLs whose files are really missing
    for source_url in pdf_urls:  # Check each URL before opening any tab
        if is_url_already_downloaded_by_name(
            database_connection, source_url
        ):  # The file already exists
            logger.info(
                "  SKIP: the file for %s already exists", source_url
            )  # Explain the skip
            mark_url_as_processed(
                database_connection, source_url
            )  # Remember this URL is finished
            run_statistics["skipped"] += 1  # Count the skip
        else:  # The file is missing
            urls_needing_download.append(source_url)  # Keep the URL for downloading
    if not urls_needing_download:  # Check whether anything is left to download
        logger.info(
            "Nothing to download in this batch, so no second tab is opened"
        )  # Record the decision
        return  # Leave without opening a tab
    browser_session.internet_monitor.raise_if_internet_is_down()  # Do not open a tab while the internet is down
    with browser_session.temporary_download_tab() as download_page:  # Open the second tab and close it afterwards
        download_pdf_urls(  # Download the missing files
            download_page,
            database_connection,
            urls_needing_download,
            run_statistics,
            browser_session.internet_monitor,  # Args
        )  # End of the call


# ============================================================== 12. SCRAPING
def collect_and_download_pdf_urls_for_keyword(  # Scrape one keyword and download each page's PDFs right away
    browser_session: "BrowserSession",  # Owns the results tab and opens the download tab when needed
    database_connection: sqlite3.Connection,  # Database that tracks URLs and files
    search_keyword: str,  # Keyword to search for
    run_statistics: dict,  # Running totals of downloaded, skipped and failed files
) -> set:  # Returns every URL found for this keyword
    page = browser_session.results_page  # The tab used for browsing the search results
    browser_session.internet_monitor.raise_if_internet_is_down()  # Wait for the internet before opening the search page
    search_url = SEARCH_URL_TEMPLATE.format(
        search_keyword=search_keyword
    )  # Build the search address
    logger.info(
        "Opening search page for keyword '%s': %s", search_keyword, search_url
    )  # Record it
    page_status = open_page_politely(
        page, search_url, run_statistics
    )  # Open the page, backing off if blocked
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):  # No internet?
        raise InternetOutageError(
            "The internet went down while opening the search page"
        )  # Wait for it, then retry
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
        browser_session.close_unneeded_tabs()  # Close any tab other than the results tab
        browser_session.internet_monitor.raise_if_internet_is_down()  # Stop here and wait if the internet is down
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
            download_pdf_urls_in_temporary_tab(
                browser_session,
                database_connection,
                urls_to_download_now,
                run_statistics,
            )  # Now
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


# ============================================================== 13. INTERNET MONITOR
def is_host_responding(
    host_address: str,
) -> bool:  # Check whether one address answers us
    if shutil.which("ping") is not None:  # Check whether the ping program is installed
        if sys.platform.startswith("win"):  # Windows uses different ping options
            ping_command = [
                "ping",
                "-n",
                "1",
                "-w",
                str(PING_TIMEOUT_IN_SECONDS * 1000),
                host_address,
            ]  # One ping, time in ms
        elif sys.platform.startswith(
            "linux"
        ):  # Linux uses -W for the timeout in seconds
            ping_command = [
                "ping",
                "-c",
                "1",
                "-W",
                str(PING_TIMEOUT_IN_SECONDS),
                host_address,
            ]  # One ping
        else:  # macOS and other systems
            ping_command = [
                "ping",
                "-c",
                "1",
                host_address,
            ]  # One ping, the time limit is enforced below
        try:  # Running ping can fail or take too long
            ping_result = subprocess.run(  # Run the ping program
                ping_command,  # The command to run
                stdout=subprocess.DEVNULL,  # Hide the normal output
                stderr=subprocess.DEVNULL,  # Hide the error output
                timeout=PING_TIMEOUT_IN_SECONDS + 2,  # Never wait longer than this
            )  # End of the run call
            if ping_result.returncode == 0:  # Exit code 0 means the host answered
                return True  # The host responded
        except (
            subprocess.TimeoutExpired,
            OSError,
        ) as ping_error:  # Ping took too long or could not run
            logger.debug(
                "Ping to %s failed to run: %s", host_address, ping_error
            )  # Record it
    else:  # The ping program is not installed
        logger.debug(
            "The ping program is not installed, using a plain network connection instead"
        )  # Record it
    try:  # Some networks block ping, so also try a plain connection
        with socket.create_connection(
            (host_address, TCP_FALLBACK_PORT), timeout=TCP_FALLBACK_TIMEOUT_IN_SECONDS
        ):  # Connect
            return True  # The connection worked, so the host responded
    except OSError:  # The connection did not work
        return False  # The host did not respond


def check_internet_connection() -> bool:  # The internet works when at least one of the addresses answers
    for host_address in INTERNET_CHECK_HOSTS:  # Try each address in turn
        if is_host_responding(host_address):  # Check whether this address answers
            logger.debug("Internet check: %s responded", host_address)  # Record it
            return True  # One answer is enough
        logger.debug("Internet check: %s did not respond", host_address)  # Record it
    return False  # Neither address answered


class InternetMonitor:  # Checks the internet in a background thread and closes Chromium when it goes down
    def __init__(self) -> None:  # Prepare the shared state
        self.internet_available = threading.Event()  # Set while the internet works
        self.stop_requested = threading.Event()  # Set when the monitor should stop
        self.state_lock = (
            threading.Lock()
        )  # Prevents two threads from changing the state at the same time
        self.outage_close_function = (
            None  # Function that closes Chromium when the internet goes down
        )
        self.outage_occurred = (
            False  # True when Chromium was closed because of an outage
        )
        self.monitor_thread = None  # The background thread

    def start(
        self, outage_close_function
    ) -> None:  # Do a first check and start the background thread
        self.outage_close_function = (
            outage_close_function  # Remember how to close Chromium
        )
        if check_internet_connection():  # First check, done right now
            self.internet_available.set()  # The internet works
            logger.info("Internet check: the internet is working")  # Record it
        else:  # No answer from either address
            logger.warning(
                "Internet check: no answer from %s, waiting for the internet",
                " or ".join(INTERNET_CHECK_HOSTS),
            )  # Say
        self.monitor_thread = threading.Thread(
            target=self.monitor_loop, name="internet-monitor", daemon=True
        )  # Create it
        self.monitor_thread.start()  # Start checking in the background

    def stop(self) -> None:  # Stop the background thread
        self.stop_requested.set()  # Ask the thread to finish
        if self.monitor_thread is not None:  # Check whether it was started
            self.monitor_thread.join(timeout=15)  # Wait a little for it to finish

    def monitor_loop(self) -> None:  # The work done by the background thread
        while not self.stop_requested.is_set():  # Keep going until we are told to stop
            self.update_state(
                check_internet_connection()
            )  # Check the internet and react to any change
            self.stop_requested.wait(
                INTERNET_CHECK_INTERVAL_IN_SECONDS
            )  # Sleep, but wake up early when stopping

    def update_state(
        self, internet_works: bool
    ) -> None:  # React when the internet goes down or comes back
        should_close_chromium = (
            False  # Becomes True when the internet has just gone down
        )
        with self.state_lock:  # Only one thread may change the state at a time
            was_available = (
                self.internet_available.is_set()
            )  # What we believed until now
            if internet_works and not was_available:  # The internet came back
                self.internet_available.set()  # Record that it works again
                logger.warning("INTERNET IS BACK")  # Tell the user
            elif not internet_works and was_available:  # The internet just went down
                self.internet_available.clear()  # Record that it does not work
                self.outage_occurred = (
                    True  # Remember that Chromium must be restarted afterwards
                )
                should_close_chromium = True  # Chromium has to be closed
                logger.error(
                    "INTERNET IS DOWN: neither %s responded",
                    " nor ".join(INTERNET_CHECK_HOSTS),
                )  # Tell the user
        if (
            should_close_chromium and self.outage_close_function is not None
        ):  # Close Chromium outside the lock
            try:  # Closing can fail
                self.outage_close_function()  # Close Chromium
            except Exception:  # Anything unexpected
                logger.exception(
                    "Could not close Chromium after the internet went down"
                )  # Log the details

    def confirm_internet_is_down(
        self,
    ) -> bool:  # Check right now whether the internet is down
        internet_works = check_internet_connection()  # Do a live check
        self.update_state(
            internet_works
        )  # Update the shared state and close Chromium if needed
        return not internet_works  # True when the internet is down

    def raise_if_internet_is_down(
        self,
    ) -> None:  # Cheap check used before every request
        if (
            not self.internet_available.is_set()
        ):  # The monitor says the internet is down
            raise InternetOutageError(
                "The internet is down"
            )  # Make the restart logic wait for it

    def consume_outage_flag(
        self,
    ) -> bool:  # Tell whether an outage happened since the last restart
        with self.state_lock:  # Only one thread may change the state at a time
            outage_happened = self.outage_occurred  # Read the flag
            self.outage_occurred = False  # Reset it
        return outage_happened  # Give the old value back

    def wait_until_available(self) -> None:  # Block until the internet works
        waited_seconds = 0  # Counts how long we have waited
        while not self.internet_available.wait(
            timeout=INTERNET_CHECK_INTERVAL_IN_SECONDS
        ):  # Wait for the "works" signal
            waited_seconds += (
                INTERNET_CHECK_INTERVAL_IN_SECONDS  # Add the time we just waited
            )
            logger.info(
                "Still waiting for the internet (%.0f seconds so far)", waited_seconds
            )  # Show progress
        if waited_seconds > 0:  # Check whether we really had to wait
            logger.info(
                "The internet is back after about %.0f seconds", waited_seconds
            )  # Record it


# ============================================================== 14. BROWSER SESSION AND RESTARTS
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
        self,
        playwright_instance,
        chromium_executable_path: str,
        internet_monitor: InternetMonitor,
    ) -> None:  # Setup
        self.internet_monitor = internet_monitor  # Tells us whether the internet works
        self.use_no_sandbox = False  # Becomes True once we learn that this system blocks Chromium's sandbox
        self.download_page = (
            None  # The temporary download tab, only set while it is open
        )
        self.is_opening_download_tab = (
            False  # True for a moment while we open the download tab ourselves
        )
        self.playwright_instance = playwright_instance  # The running Playwright
        self.chromium_executable_path = (
            chromium_executable_path  # Where Chromium is installed
        )
        self.chromium_process = None  # The Chromium program, once it has been started
        self.connected_browser = None  # Playwright's connection to Chromium
        self.results_page = None  # Tab used for browsing the search results
        self.restart_count = 0  # How many times Chromium had to be restarted

    def start(self) -> None:  # Start Chromium and open both tabs
        self.chromium_process = start_normal_chromium_process(
            self.chromium_executable_path, self.use_no_sandbox
        )  # Start it
        debugging_endpoint = wait_for_chromium_debugging_endpoint(
            self.chromium_process
        )  # Wait until Chromium is ready
        if debugging_endpoint is None and chromium_failed_because_of_sandbox(
            self.chromium_process
        ):  # The sandbox is the problem
            if (
                not CHROMIUM_DISABLE_SANDBOX_IF_NEEDED
            ):  # The user does not allow starting without the sandbox
                stop_chromium_process(
                    self.chromium_process
                )  # Make sure nothing is left running
                raise RuntimeError(
                    "Chromium cannot use its sandbox here. Set CHROMIUM_DISABLE_SANDBOX_IF_NEEDED = True or allow user namespaces"
                )  # Stop
            logger.warning(
                "Chromium cannot use its sandbox on this system, starting it again with --no-sandbox"
            )  # Explain
            stop_chromium_process(
                self.chromium_process
            )  # Make sure the failed process is gone
            self.use_no_sandbox = True  # Remember this choice for every later start
            self.chromium_process = start_normal_chromium_process(
                self.chromium_executable_path, True
            )  # Start it again
            debugging_endpoint = wait_for_chromium_debugging_endpoint(
                self.chromium_process
            )  # Wait until it is ready
        if debugging_endpoint is None:  # Chromium did not become ready
            stop_chromium_process(
                self.chromium_process
            )  # Do not leave a stuck Chromium behind
            raise RuntimeError(
                "Chromium did not become ready, read the CHROMIUM OUTPUT lines above for the reason"
            )  # Stop
        logger.info("Connecting Playwright to the running Chromium")  # Record it
        self.connected_browser = self.playwright_instance.chromium.connect_over_cdp(
            debugging_endpoint
        )  # Attach directly
        browser_context = self.connected_browser.contexts[
            0
        ]  # Use Chromium's normal browsing context
        open_tabs = list(browser_context.pages)  # The tabs Chromium opened by itself
        logger.info(
            "Chromium started with %d tab(s)", len(open_tabs)
        )  # Record how many tabs there are
        if open_tabs:  # Chromium opened at least one tab
            self.results_page = open_tabs[
                0
            ]  # Use the first tab for browsing the search results
        else:  # Chromium opened no tab at all
            self.results_page = (
                browser_context.new_page()
            )  # Open one tab for browsing the search results
        for extra_tab in open_tabs[1:]:  # Every further tab is not needed
            logger.info(
                "Closing an unneeded extra tab: %s", extra_tab.url
            )  # Record which tab is closed
            try:  # Closing can fail if the tab is already gone
                extra_tab.close()  # Close the tab
            except PlaywrightError as close_error:  # The tab could not be closed
                logger.debug(
                    "Could not close the extra tab (ignored): %s", close_error
                )  # Record it
        self.results_page.set_default_timeout(
            NAVIGATION_TIMEOUT_IN_MILLISECONDS
        )  # Default time limit for page actions
        attach_browser_event_logging(
            self.results_page
        )  # Start logging everything the results tab does
        browser_context.on(
            "page", self.close_unexpected_new_tab
        )  # Close any popup or extra tab the moment it opens
        self.results_page.bring_to_front()  # Show the results tab

    def close_unexpected_new_tab(
        self, new_tab: Page
    ) -> None:  # Close tabs that nobody asked for
        if (
            self.is_opening_download_tab
            or new_tab is self.results_page
            or new_tab is self.download_page
        ):  # A wanted tab
            return  # Leave it open
        logger.info(
            "Closing an unexpected new tab: %s", new_tab.url
        )  # Record which tab is closed
        try:  # Closing can fail if the tab is already gone
            new_tab.close()  # Close the tab
        except PlaywrightError as close_error:  # The tab could not be closed
            logger.debug(
                "Could not close the unexpected tab (ignored): %s", close_error
            )  # Record it

    def close_unneeded_tabs(
        self,
    ) -> None:  # Close every tab except the results tab and the download tab
        browser_context = self.connected_browser.contexts[
            0
        ]  # Chromium's normal browsing context
        closed_tab_count = 0  # Counts the tabs that were closed
        for open_tab in list(browser_context.pages):  # Look at every open tab
            if (
                open_tab is self.results_page or open_tab is self.download_page
            ):  # A tab we need
                continue  # Keep it
            logger.info(
                "Closing an unneeded tab: %s", open_tab.url
            )  # Record which tab is closed
            try:  # Closing can fail if the tab is already gone
                open_tab.close()  # Close the tab
                closed_tab_count += 1  # Count it
            except PlaywrightError as close_error:  # The tab could not be closed
                logger.debug(
                    "Could not close the unneeded tab (ignored): %s", close_error
                )  # Record it
        logger.debug(
            "Tab check: closed %d tab(s), %d tab(s) remain open",
            closed_tab_count,
            len(browser_context.pages),
        )  # Summary

    def close_chromium_for_outage(
        self,
    ) -> None:  # Close Chromium because the internet is down (runs in the monitor thread)
        logger.error("Closing Chromium because the internet is down")  # Record it
        stop_chromium_process(
            self.chromium_process
        )  # Close the Chromium program, Playwright is not touched here

    @contextmanager  # Turns the method below into something that can be used in a "with" block
    def temporary_download_tab(
        self,
    ) -> Iterator[Page]:  # Open a second tab only while downloads need it
        browser_context = self.connected_browser.contexts[
            0
        ]  # Chromium's normal browsing context
        logger.info("Opening a temporary second tab for downloading")  # Record it
        self.is_opening_download_tab = (
            True  # Tell the popup guard that this new tab is wanted
        )
        try:  # Make sure the flag is always reset
            download_page = browser_context.new_page()  # Open the second tab
        finally:  # Always runs
            self.is_opening_download_tab = (
                False  # The popup guard may close unexpected tabs again
            )
        self.download_page = (
            download_page  # Remember it so the guards leave it alone while it is open
        )
        download_page.set_default_timeout(
            NAVIGATION_TIMEOUT_IN_MILLISECONDS
        )  # Default time limit for it
        attach_browser_event_logging(
            download_page
        )  # Start logging everything the download tab does
        try:  # Make sure the tab is always closed again
            yield download_page  # Hand the tab to the code inside the "with" block
        finally:  # Always runs, even after an error
            self.download_page = None  # The download tab is going away
            logger.info("Closing the temporary download tab")  # Record it
            try:  # Closing can fail if Chromium itself is gone
                download_page.close()  # Close the second tab
                self.results_page.bring_to_front()  # Show the results tab again
            except PlaywrightError as close_error:  # The tab or the browser was already gone
                logger.debug(
                    "Could not close the download tab cleanly (ignored): %s",
                    close_error,
                )  # Record it

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
        if (
            self.internet_monitor.consume_outage_flag()
        ):  # Check whether this restart is only because the internet was down
            logger.info(
                "This restart is caused by an internet outage, so it does not count towards the limit"
            )  # Say so
        else:  # The restart has another reason, such as a crash
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
        self.internet_monitor.wait_until_available()  # Do not start Chromium until the internet works
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
        except InternetOutageError as internet_outage_error:  # The internet went down
            logger.warning(
                "Internet outage during %s: %s", step_description, internet_outage_error
            )  # Record it
            browser_session.restart()  # Wait for the internet, then start a fresh Chromium
            continue  # Run the same step again, finished URLs are skipped through the database
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
            if (
                browser_session.internet_monitor.confirm_internet_is_down()
            ):  # Check whether the internet being down is the cause
                logger.warning(
                    "The internet is down, so the error during %s is expected",
                    step_description,
                )  # Say so
                browser_session.restart()  # Wait for the internet, then start a fresh Chromium
                continue  # Run the same step again
            logger.exception(
                "Browser error during %s, skipping this step", step_description
            )  # Log the full error
            return  # Go on with the next step instead of crashing the whole run


# ============================================================== 15. MAIN PROGRAM
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
    ignore_non_usa_english_urls(
        database_connection
    )  # Skip old pending URLs that are not USA English
    prepare_chromium_profile_preferences()  # Make Chromium download PDFs instead of previewing them
    allow_local_connections_without_proxy()  # Never send the local Chromium connection through a proxy
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
            internet_monitor = (
                InternetMonitor()
            )  # Prepare the background internet check
            browser_session = BrowserSession(
                playwright_instance, chromium_executable_path, internet_monitor
            )  # Session
            try:  # Make sure the monitor and Chromium are always stopped at the end
                internet_monitor.start(
                    browser_session.close_chromium_for_outage
                )  # Check the internet in the background
                internet_monitor.wait_until_available()  # Do not start Chromium while the internet is down
                browser_session.start()  # Start Chromium with a single results tab
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
                            browser_session,  # Gives access to the current tabs
                            database_connection,  # Database that tracks URLs and files
                            search_keyword,  # Keyword to search for
                            run_statistics,  # Running totals
                        ),  # End of the keyword work
                    )  # End of the restart wrapper
                logger.info("Retrying URLs that are still pending")  # Record the retry
                run_with_browser_restarts(  # Retry pending URLs, restarting Chromium if it closes
                    browser_session,  # The session that can be restarted
                    "the retry of pending URLs",  # Description used in log messages
                    lambda: download_pdf_urls_in_temporary_tab(  # The work to do
                        browser_session,  # Opens the download tab only if something needs downloading
                        database_connection,  # Database that tracks URLs and files
                        load_unprocessed_urls(
                            database_connection
                        ),  # Pending URLs, read fresh each attempt
                        run_statistics,  # Running totals
                    ),  # End of the download work
                )  # End of the restart wrapper
            finally:  # Always runs, even after an error
                internet_monitor.stop()  # Stop the background internet check
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
