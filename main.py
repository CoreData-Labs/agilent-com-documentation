#!/usr/bin/env python3  # Tell Unix/Linux shells to run this file with the system Python 3 interpreter
# Agilent PDF downloader.  # Top-level script description
#  # Blank header line
# It runs two jobs, always in this order:  # Execution order summary
#  # Blank header line
# CHEMICAL STANDARDS  # Job 1 section header
#   Downloads the Safety Data Sheet PDFs found on Agilent's chemical-standards search page.  # Job 1 overview
#   Every document can exist in several regional versions; exactly ONE version is downloaded per document:  # Version selection rule
#     1. _NAEnglish.pdf is the default and is always preferred.  # Priority 1 version
#     2. If it is not available, _NACanada.pdf is downloaded instead.  # Priority 2 version
#     3. If neither is available, one of _Australia.pdf, _EUEnglish.pdf, _MalayEnglish.pdf or _UKEnglish.pdf is used.  # Priority 3 regional versions
#     4. If none of these versions exists, the document is skipped. Any other file format is ignored.  # Fallback skip rule
#  # Blank header line
# SAFETY DATA SHEETS  # Job 2 section header
#   Downloads every "English - United States" document from  # Job 2 target source part 1
#   https://www.agilent.com/en/search/safety-data-sheets  # Job 2 target URL
#   (the file names there look like 5190-8406_US.pdf or 18711-60060_NAEnglish.pdf).  # Example SDS filenames
#   * The page is opened once (page 1); then the page numbers 1, 2, 3 ... up to 500 are CLICKED one at a time.  # Sequential pagination behavior
#     Every page's rendered DOM is read, every .pdf link in it is collected, and the PDFs are downloaded.  # Per-page extraction and download behavior
#   * The site only lets you reach 5,000 results (500 pages), so the query asks for US English documents only  # Pagination cap and locale filter
#     and, if that is still more than 5,000, the publication-date range is split in halves until every piece fits.  # Date-range splitting strategy
#   * Progress is saved in the database, so a stopped run continues at the page where it stopped.  # Resume capability
#   * The PDF links are read from the JavaScript-rendered DOM of the real browser (not from the page source).  # Rendered DOM extraction rule
#   * Clicking a PDF link opens the PDF in the browser (https://www.agilent.com/library/msds/<name>.pdf).  # Link opening behavior
#     The script saves that PDF from the page it opened; if that does not work it falls back to  # Primary save + fallback note
#     a direct request that reuses the browser's cookies.  # Cookie-reusing fallback request
#  # Blank header line
# How to start (the job order never changes: Chemical Standards first, then Safety Data Sheets):  # CLI usage header
#   python main.py                                   -> Chemical Standards, then Safety Data Sheets  # Default command usage
#   python main.py --start-from chemical-standards   -> the same as above  # Explicit Chemical Standards start
#   python main.py --start-from safety-data-sheets   -> skips Chemical Standards, runs Safety Data Sheets only  # Safety Data Sheets only mode
#  # Blank header line
# The browser is controlled through invisible_playwright (Playwright on an anti-detect Firefox).  # Browser automation library note
#  # Blank header line
# How the script works:  # Operational overview header
#   * Before anything is requested, the download folder is checked: a document that already has a file there is skipped.  # Pre-run disk check
#   * It starts one browser window at 1920x1080 with a single "results" tab.  # Initial window and tab setup
#   * It visits the home page and a few other pages first, behaving like an ordinary visitor.  # Session warm-up browsing
#   * Chemical Standards searches the keywords a-z and 0-9, one after another. A default (_NAEnglish) version is  # Keyword search and immediate default download
#     downloaded as soon as it is found; fallback versions wait until every keyword has been searched.  # Deferred fallback download rule
#   * After each page of results it downloads the PDFs found there, using a short-lived second tab  # Temporary download tab workflow
#     that is opened only when something needs downloading and closed right afterwards.  # Temporary tab cleanup
#   * Only the results tab is ever kept open; popups, extra tabs and extra windows are closed at once.  # Strict tab hygiene
#   * A SQLite database remembers every URL, file name and Safety Data Sheets page position, so nothing is downloaded twice.  # SQLite deduplication and state tracking
#   * If the browser crashes, or the internet drops, the browser is restarted and the work continues.  # Crash and outage recovery
#   * Random pauses, back-off waits and a "stop after too many blocks" rule keep the traffic gentle.  # Rate limiting and block protection
#   * Keywords that could not be searched are retried once at the end.  # Single retry pass for failed keywords
#   * Everything is logged to the console at DEBUG level.  # Logging verbosity note

import argparse  # Reads the command-line arguments
import asyncio  # Lets us check several internet hosts at the same time
import logging  # Prints log lines to the console
import math  # Distance and curve calculations for human-like mouse movement
import random  # Picks random pause lengths and random actions
import re  # Regular expressions for link matching and text cleaning
import shutil  # Finds the "ping" program on the system PATH
import socket  # Opens a plain TCP connection when ping is unavailable
import sqlite3  # Built-in database that remembers what has been downloaded
import string  # Supplies the lowercase letters and digits used as search keywords
import subprocess  # Runs "ping" as an external process
import sys  # Gives access to standard output and the current platform name
import threading  # Runs the internet check in the background
import time  # Provides sleep() and the current time
from concurrent.futures import (
    ThreadPoolExecutor,
)  # Runs the async internet check in its own clean thread
from contextlib import (
    contextmanager,
)  # Turns a method into something usable in a "with" block
from dataclasses import dataclass  # Makes small classes that only hold data
from datetime import (
    date,
    timedelta,
)  # Calendar dates for splitting the publication-date range
from pathlib import Path  # Object-oriented filesystem paths
from typing import Any, Iterator, Optional  # Type hints
from urllib.parse import (
    unquote,
    urlencode,
    urlparse,
)  # Helpers to read a file name out of a URL and to build addresses

from invisible_playwright import (
    InvisiblePlaywright,
)  # Playwright on an anti-detect Firefox

from invisible_playwright._pw.sync_api import (  # The Playwright copy bundled with invisible_playwright
    Dialog,  # Type of alert/confirm popups
    Download,  # Type of a browser download
    Page,  # Type of a browser tab
    Error as PlaywrightError,  # Base error class raised by Playwright
    TimeoutError as PlaywrightTimeoutError,  # Error raised when Playwright waits too long
)

# ============================================================== SETTINGS  # Configuration constants section

# ---- The two jobs  # Job identifier constants
JOB_CHEMICAL_STANDARDS = "chemical-standards"  # Job one
JOB_SAFETY_DATA_SHEETS = "safety-data-sheets"  # Job two

# ---- Chemical Standards  # Chemical Standards settings group
SEARCH_URL_TEMPLATE = "https://www.agilent.com/en/product/chemical-standards/search-results?tabname=related_part_number&keyword={search_keyword}"  # Search URL template parameterized by keyword
SEARCH_KEYWORDS = list(string.ascii_lowercase + string.digits)  # a-z followed by 0-9
HOME_PAGE_URL = (
    "https://www.agilent.com/"  # The page visited first, like a normal visitor would
)
HOME_PAGE_BROWSING_SECONDS = (
    30  # How long to move the mouse and scroll on the home page
)
OTHER_PAGES_TO_VISIT = 3  # How many other pages are opened from the home page
OTHER_PAGE_BROWSING_SECONDS = 30  # How long to stay on each of those pages
DEFAULT_VERSION_SUFFIX = (
    "_NAEnglish.pdf"  # The preferred version of every document (USA English)
)
FIRST_FALLBACK_VERSION_SUFFIX = (
    "_NACanada.pdf"  # Used only when the default version is not available
)
OTHER_VERSION_SUFFIXES = (  # Tuple of secondary regional PDF suffixes
    "_Australia.pdf",  # Australian English PDF suffix
    "_EUEnglish.pdf",  # European Union English PDF suffix
    "_MalayEnglish.pdf",  # Malaysian English PDF suffix
    "_UKEnglish.pdf",  # UK English PDF suffix
)  # Used only when neither version above is available; tried in this order
ALLOWED_VERSION_SUFFIXES_BEST_FIRST = (
    (  # Ordered tuple of all acceptable version suffixes from best to least preferred
        DEFAULT_VERSION_SUFFIX,  # Highest-priority suffix (_NAEnglish.pdf)
        FIRST_FALLBACK_VERSION_SUFFIX,  # Second-priority suffix (_NACanada.pdf)
    )
    + OTHER_VERSION_SUFFIXES
)  # Every format Chemical Standards may download, best first

# ---- Safety Data Sheets  # Safety Data Sheets settings group
SDS_BASE_URL = "https://www.agilent.com/en/search/safety-data-sheets"  # The page with every safety data sheet
SDS_OBJECT_TYPE_CLAUSE = '@objecttype=="Material Safety Data Sheet"'  # Part of the query that selects safety data sheets
SDS_LOCALE_CLAUSE = '@locale=="English_UNITED STATES"'  # Extra query part that asks for US English documents only
SDS_DATE_FIELD = (
    "publicationdate"  # The search field used to cut the results into date ranges
)
SDS_LANGUAGE_FACET_PARAMETER = (
    "c_ds_f-agilent_doc_language_long"  # The address parameter of the "Language" filter
)
SDS_LANGUAGE_FACET_VALUE = "English"  # The language filter value
SDS_FIRST_RESULT_PARAMETER = (
    "c_ds_firstResult"  # The address parameter that selects the page (0, 10, 20, ...)
)
USE_LOCALE_CLAUSE = True  # Try the US English query first; if it does not work, fall back to the plain English filter
SDS_MAXIMUM_REACHABLE_RESULTS = 5000  # The site shows at most 500 pages
SDS_RESULTS_PER_PAGE = 10  # The site shows 10 cards per page
SDS_RESULTS_SCOPE_SELECTOR = (
    "[data-tab-content='documentsSupport']"  # The results tab that holds the cards
)
SDS_EARLIEST_DATE = date(1990, 1, 1)  # The date range that is split starts here
SDS_LATEST_DATE = date(2027, 12, 31)  # The date range that is split ends here
SDS_DOWNLOAD_ONLY_US_ENGLISH = True  # True: only cards tagged "English - United States"; False: every PDF found on every page
SDS_SEGMENT_KEY_VERSION = "pages-v2"  # Changing this makes old saved Safety Data Sheets progress (for example a wrongly "finished" run) be ignored
SDS_WAIT_FOR_RESULTS_SECONDS = (
    25  # How long to wait for a result page to show its cards or its total
)

BROWSER_WINDOW_WIDTH_PIXELS = 1920  # Browser viewport and screen width in pixels
BROWSER_WINDOW_HEIGHT_PIXELS = 1080  # Browser viewport and screen height in pixels

DOWNLOAD_FOLDER = Path("PDFs").resolve()  # Full path of the folder where PDFs are saved
DATABASE_FILE_PATH = Path(
    "download_history.sqlite3"
).resolve()  # Full path of the SQLite history file
MAXIMUM_FILE_NAME_STEM_LENGTH = 150  # Longest allowed file name, not counting ".pdf"
MAXIMUM_URLS_PER_DATABASE_QUERY = 500  # Most "?" placeholders used in one SQL query

MAXIMUM_RESULT_PAGES_PER_KEYWORD = (
    200  # Safety limit on how many result pages are followed per keyword
)
PAGE_LOAD_TIMEOUT_MILLISECONDS = (
    30_000  # Default timeout for page loads and browser operations in milliseconds
)
DOWNLOAD_START_TIMEOUT_MILLISECONDS = (
    60_000  # How long to wait for the browser to start one download
)
PDF_RESPONSE_DOWNLOAD_WAIT_SECONDS = (
    15  # When the server answered with a file but no download starts, wait this long
)

PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES = (
    2.0,
    5.0,
)  # Min and max delay in seconds between result pages
PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS = (
    1.0,
    3.0,
)  # Min and max delay in seconds between file downloads
PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS = (
    5.0,
    10.0,
)  # Min and max delay in seconds between keyword searches
ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE = (
    3.0,
    6.0,
)  # Min and max human-like idle/browsing duration after page load
MAXIMUM_ACCESS_DENIED_ATTEMPTS = (
    3  # How many times a page showing "Access Denied" is retried
)
ACCESS_DENIED_BACKOFF_SECONDS = (
    30  # First back-off wait in seconds; it doubles on every further attempt
)
MAXIMUM_CONSECUTIVE_BLOCKS = 5  # The whole run stops after this many blocks in a row
MAXIMUM_BROWSER_RESTARTS = (
    5  # How many times the browser may be restarted after a crash
)

INTERNET_CHECK_HOSTS = (
    "1.1.1.1",
    "8.8.8.8",
)  # Public DNS IP addresses used to verify internet connectivity
INTERNET_CHECK_INTERVAL_SECONDS = (
    10  # How often the background thread checks internet connectivity in seconds
)
PING_TIMEOUT_SECONDS = 5  # Timeout in seconds for each ping command
FALLBACK_CONNECTION_TEST_PORT = (
    53  # TCP port (DNS) used when ping is unavailable or blocked
)
FALLBACK_CONNECTION_TEST_TIMEOUT_SECONDS = (
    2  # Timeout in seconds for fallback TCP socket connection checks
)

download_logger = logging.getLogger(
    "agilent_downloader"
)  # Create a named logger instance for the downloader


def validate_settings() -> None:  # Stop immediately if a setting above is obviously wrong
    assert (
        SEARCH_KEYWORDS
    ), "SEARCH_KEYWORDS must contain at least one keyword"  # Ensure at least one keyword is configured
    assert (
        BROWSER_WINDOW_WIDTH_PIXELS > 0 and BROWSER_WINDOW_HEIGHT_PIXELS > 0
    ), "The browser window size must be positive"  # Ensure window dimensions are positive
    assert all(  # Verify every configured suffix ends with ".pdf"
        version_suffix.lower().endswith(".pdf")
        for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST  # Check case-insensitive ".pdf" extension
    ), "Every version suffix must end with .pdf"  # Assertion failure message for invalid suffix
    assert len(
        {
            version_suffix.lower()
            for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST
        }
    ) == len(  # Ensure no duplicate suffixes exist
        ALLOWED_VERSION_SUFFIXES_BEST_FIRST  # Compare unique lowercase count against total count
    ), "The version suffixes must all be different"  # Assertion failure message for duplicate suffixes
    for pause_range in (  # Iterate through every configured pause tuple
        PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,  # Result-page pause range
        PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,  # Download pause range
        PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS,  # Keyword pause range
        ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,  # Post-load activity range
    ):  # End of pause ranges tuple
        assert (
            0 <= pause_range[0] <= pause_range[1]
        ), f"Pause range {pause_range} must be (low, high) with low <= high"  # Ensure bounds are non-negative and ordered
    assert (
        SDS_EARLIEST_DATE < SDS_LATEST_DATE
    ), "SDS_EARLIEST_DATE must be before SDS_LATEST_DATE"  # Ensure SDS date window is valid
    assert (  # Check that max reachable results is an exact multiple of page size
        SDS_MAXIMUM_REACHABLE_RESULTS % SDS_RESULTS_PER_PAGE
        == 0  # Remainder must be zero
    ), "The reachable result count must be a whole number of pages"  # Assertion failure message for non-divisible result cap


class AccessBlockedError(
    Exception
):  # Custom exception raised when consecutive access blocks hit the limit
    """The website kept blocking us; the whole run should stop."""  # Docstring explaining when to stop the run


class InternetOutageError(
    Exception
):  # Custom exception raised when internet connectivity is lost
    """The internet is down; the current step should be retried once it is back."""  # Docstring explaining retry behavior


@dataclass  # Generate __init__, __repr__, and comparison helpers automatically
class RunStatistics:  # Running totals for the whole run
    downloaded_count: int = 0  # PDFs saved to disk
    skipped_count: int = (
        0  # URLs skipped because the file already exists or is not a PDF
    )
    unavailable_count: int = 0  # URLs whose page says it may no longer exist
    failed_count: int = 0  # URLs that could not be downloaded this time
    consecutive_block_count: int = 0  # "Access Denied" pages seen in a row


# ============================================================== LOGGING  # Logging setup and helper functions


def configure_console_logging() -> None:  # Send every log message to the console at DEBUG level
    console_handler = logging.StreamHandler(sys.stdout)  # Write logs to standard output
    console_handler.setLevel(
        logging.DEBUG
    )  # Accept DEBUG and higher-severity log messages
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )  # Format timestamp, level, and message
    download_logger.setLevel(logging.DEBUG)  # Set logger level to DEBUG
    if (
        not download_logger.handlers
    ):  # Only attach a handler once, so lines are never printed twice
        download_logger.addHandler(
            console_handler
        )  # Register the console handler on the logger
    download_logger.propagate = (
        False  # Prevent duplicate logging through the root logger
    )


def attach_browser_event_logging(
    browser_page: Page,
) -> None:  # Attach listeners to log events on a Playwright page
    """Log console output, errors, failed requests, navigation, downloads and popups of one tab."""  # Function docstring
    browser_page.on(  # Listen for browser console messages
        "console",  # Event name for console logs
        lambda console_message: download_logger.debug(
            "BROWSER CONSOLE [%s] %s", console_message.type, console_message.text
        ),  # Log console type and text at DEBUG
    )  # End console event registration
    browser_page.on(
        "pageerror",
        lambda page_error: download_logger.error("BROWSER PAGE ERROR %s", page_error),
    )  # Log uncaught page exceptions as errors
    browser_page.on(  # Listen for failed network requests
        "requestfailed",  # Event name for failed requests
        lambda failed_request: download_logger.warning(  # Log failed request details as a warning
            "REQUEST FAILED %s %s -> %s",
            failed_request.method,
            failed_request.url,
            failed_request.failure,  # Include HTTP method, URL, and failure reason
        ),  # End warning callback
    )  # End requestfailed event registration
    browser_page.on(  # Listen for HTTP responses
        "response",  # Event name for responses
        lambda http_response: download_logger.warning(
            "HTTP %s %s", http_response.status, http_response.url
        )  # Log status and URL when status is 400 or higher
        if http_response.status >= 400  # Only log HTTP error responses
        else None,  # Ignore successful responses
    )  # End response event registration
    browser_page.on(  # Listen for frame navigations
        "framenavigated",  # Event name for frame navigation
        lambda navigated_frame: download_logger.info(
            "NAVIGATED to %s", navigated_frame.url
        )  # Log navigation target URL
        if navigated_frame.parent_frame
        is None  # Only log top-level main frame navigations
        else None,  # Ignore child iframe navigations
    )  # End framenavigated event registration
    browser_page.on(  # Listen for browser-initiated downloads
        "download",  # Event name for downloads
        lambda started_download: download_logger.info(  # Log download URL and suggested filename
            "BROWSER DOWNLOAD STARTED url=%s suggested_name=%s",
            started_download.url,
            started_download.suggested_filename,  # Format download metadata
        ),  # End download callback
    )  # End download event registration

    def handle_dialog_popup(
        dialog_popup: Dialog,
    ) -> None:  # Alert/confirm popups must never block us
        download_logger.warning(
            "BROWSER DIALOG [%s] %s", dialog_popup.type, dialog_popup.message
        )  # Log dialog type and text
        dialog_popup.dismiss()  # Dismiss the dialog so execution continues

    browser_page.on(
        "dialog", handle_dialog_popup
    )  # Register dialog handler on the page


def log_run_statistics(
    run_statistics: RunStatistics, summary_label: str
) -> None:  # Log current counters with a label
    download_logger.info(  # Write summary statistics at INFO level
        "%s: downloaded %d | skipped %d | page unavailable %d | failed %d",  # Summary format string
        summary_label,  # Label describing when this summary was logged
        run_statistics.downloaded_count,  # Total downloaded PDFs
        run_statistics.skipped_count,  # Total skipped URLs/files
        run_statistics.unavailable_count,  # Total unavailable pages
        run_statistics.failed_count,  # Total failed downloads
    )  # End logger call


# ============================================================== SMALL HELPERS  # General-purpose utility functions


def split_into_batches(
    items_to_split: list, maximum_batch_size: int
) -> Iterator[list]:  # Yield slices of a list up to maximum_batch_size
    for batch_start_position in range(
        0, len(items_to_split), maximum_batch_size
    ):  # Step through start indices by batch size
        yield items_to_split[
            batch_start_position : batch_start_position + maximum_batch_size
        ]  # Yield the next slice


def remove_fragment_from_url(
    page_url: str,
) -> str:  # Remove the "#section" part of a web address
    return page_url.split("#")[0]  # Return everything before the first '#' character


# ============================================================== DATABASE  # SQLite persistence layer
#
# Three tables:  # Schema overview comment
#   collected_urls   -- every PDF URL ever seen, and whether it has been handled  # Table 1 description
#   downloaded_files -- every file name actually saved, so duplicate names are caught too  # Table 2 description
#   sds_segments     -- Safety Data Sheets: every query piece (date range), its result count and the page it has reached  # Table 3 description


class DownloadDatabase:  # Wraps the SQLite connection and every query this script needs
    def __init__(
        self, database_path: Path
    ):  # Initialize database connection and create tables if missing
        download_logger.info("Opening database: %s", database_path)  # Log database path
        self.database_connection = sqlite3.connect(
            database_path
        )  # Open or create the SQLite database file
        self.safe_file_names_found_in_folder: set[
            str
        ] = set()  # Clean names of the PDFs seen in the download folder
        with self.database_connection:  # Run DDL statements inside a transaction
            self.database_connection.execute(  # Create table tracking discovered PDF URLs and processing status
                "CREATE TABLE IF NOT EXISTS collected_urls (source_url TEXT PRIMARY KEY, is_processed INTEGER NOT NULL DEFAULT 0)"  # SQL for collected_urls
            )  # Execute collected_urls DDL
            self.database_connection.execute(  # Create table tracking saved filenames and their source URLs
                "CREATE TABLE IF NOT EXISTS downloaded_files (file_name TEXT PRIMARY KEY COLLATE NOCASE, source_url TEXT NOT NULL, downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"  # SQL for downloaded_files
            )  # Execute downloaded_files DDL
            self.database_connection.execute(  # Create table tracking SDS date/locale segments and pagination progress
                "CREATE TABLE IF NOT EXISTS sds_segments (segment_key TEXT PRIMARY KEY, total_results INTEGER NOT NULL, next_first_result INTEGER NOT NULL DEFAULT 0, is_finished INTEGER NOT NULL DEFAULT 0)"  # SQL for sds_segments
            )  # Execute sds_segments DDL

    def close_database(self) -> None:  # Close the SQLite database connection
        self.database_connection.close()  # Close connection cleanly

    def load_sds_segment(
        self, segment_key: str
    ) -> Optional[tuple]:  # (total, next first result, finished) or None
        database_row = self.database_connection.execute(  # Query saved state for a single SDS segment
            "SELECT total_results, next_first_result, is_finished FROM sds_segments WHERE segment_key = ?",  # Parameterized SELECT query
            (segment_key,),  # Bind segment_key parameter
        ).fetchone()  # Fetch one matching row or None
        if database_row is None:  # Return None if segment has never been saved
            return None  # No saved segment found
        return (
            int(database_row[0]),
            int(database_row[1]),
            bool(database_row[2]),
        )  # Convert row values to int, int, bool

    def save_sds_segment(
        self,
        segment_key: str,
        total_results: int,
        next_first_result: int,
        is_finished: bool,
    ) -> None:  # Insert or update SDS segment progress
        with self.database_connection:  # Commit changes atomically
            self.database_connection.execute(  # Upsert segment state
                "INSERT OR REPLACE INTO sds_segments (segment_key, total_results, next_first_result, is_finished) VALUES (?, ?, ?, ?)",  # Parameterized INSERT OR REPLACE
                (
                    segment_key,
                    total_results,
                    next_first_result,
                    int(is_finished),
                ),  # Bind segment fields (bool as 0/1)
            )  # Execute upsert

    def register_pdf_files_already_on_disk(
        self, pdf_folder: Path
    ) -> None:  # Record existing PDFs from disk before downloading
        """Scan the download folder so every PDF that is already there is skipped and never downloaded again."""  # Method docstring
        newly_registered_count = (
            0  # Count how many disk files were newly added to downloaded_files
        )
        with self.database_connection:  # Commit all disk registrations together
            for (
                pdf_path
            ) in pdf_folder.iterdir():  # Iterate over entries in the download directory
                if (
                    not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf"
                ):  # Skip directories and non-PDF files
                    continue  # Move to next directory entry
                self.safe_file_names_found_in_folder.add(
                    build_safe_file_name(pdf_path.name)
                )  # Store normalized safe filename in memory set
                insert_result = self.database_connection.execute(  # Insert filename if not already recorded
                    "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",  # Ignore duplicates
                    (
                        pdf_path.name,
                        "found on disk before first run",
                    ),  # Record original filename and source note
                )  # Execute insert
                newly_registered_count += (
                    insert_result.rowcount
                )  # Add 1 if inserted, 0 if ignored
        download_logger.info(  # Log summary of pre-existing PDFs on disk
            "Download folder check: %d PDF file(s) found in %s (%d newly recorded); documents that already have a file are skipped",  # Summary format
            len(self.safe_file_names_found_in_folder),  # Number of PDFs seen in folder
            pdf_folder,  # Folder path
            newly_registered_count,  # Newly added DB rows
        )  # End log call

    def ignore_urls_in_unwanted_formats(
        self,
    ) -> None:  # Mark unprocessed URLs with non-allowed suffixes as processed
        """Mark pending Chemical Standards URLs that are not in one of the wanted formats as done."""  # Method docstring
        pending_urls = (
            self.load_unprocessed_urls()
        )  # Load all unprocessed URLs from database
        ignored_urls = [
            pending_url
            for pending_url in pending_urls
            if not is_wanted_version_pdf_url(pending_url)
        ]  # Filter to URLs without an allowed suffix
        for ignored_url in ignored_urls:  # Loop over unwanted URLs
            self.mark_url_processed(
                ignored_url
            )  # Mark each unwanted URL as processed so it is skipped
        download_logger.info(  # Log how many URLs were marked ignored
            "Ignored %d pending URL(s) that are not in one of the %d wanted formats",  # Log message format
            len(ignored_urls),  # Count of ignored URLs
            len(ALLOWED_VERSION_SUFFIXES_BEST_FIRST),  # Total count of allowed suffixes
        )  # End log call

    def save_newly_found_urls(
        self, found_urls: set
    ) -> None:  # Insert newly discovered URLs into collected_urls
        with self.database_connection:  # Commit batch insert
            insert_result = self.database_connection.executemany(  # Insert all URLs while ignoring already-present ones
                "INSERT OR IGNORE INTO collected_urls (source_url) VALUES (?)",  # Parameterized insert
                [
                    (found_url,) for found_url in found_urls
                ],  # Wrap each URL in a 1-tuple
            )  # Execute executemany
        download_logger.info(  # Log newly saved vs already-known URL count
            "Saved %d new URL(s) (%d were already known)",
            insert_result.rowcount,
            len(found_urls) - insert_result.rowcount,  # Format counts
        )  # End log call

    def load_unprocessed_urls(
        self,
    ) -> list:  # Return sorted list of unprocessed URLs from database
        database_rows = self.database_connection.execute(  # Query unprocessed URLs ordered alphabetically
            "SELECT source_url FROM collected_urls WHERE is_processed = 0 ORDER BY source_url"  # SELECT query
        )  # Execute query
        return [
            database_row[0] for database_row in database_rows.fetchall()
        ]  # Extract first column from each row

    def keep_only_unprocessed_urls(
        self, candidate_urls: list
    ) -> list:  # Filter candidate_urls to those still unprocessed in DB
        pending_urls: set[str] = set()  # Collect URLs confirmed unprocessed in DB
        for url_batch in split_into_batches(
            candidate_urls, MAXIMUM_URLS_PER_DATABASE_QUERY
        ):  # Process in SQLite-safe batch sizes
            placeholder_text = ",".join(
                "?" for _url_in_batch in url_batch
            )  # Build comma-separated '?' placeholders
            database_rows = self.database_connection.execute(  # Query which URLs in this batch have is_processed == 0
                f"SELECT source_url FROM collected_urls WHERE is_processed = 0 AND source_url IN ({placeholder_text})",  # Dynamic IN clause
                url_batch,  # Bind batch URLs
            )  # Execute batch query
            pending_urls.update(
                database_row[0] for database_row in database_rows.fetchall()
            )  # Add matching URLs to set
        return [
            candidate_url
            for candidate_url in candidate_urls
            if candidate_url in pending_urls
        ]  # Preserve original order while filtering

    def mark_url_processed(
        self, source_url: str
    ) -> None:  # Set is_processed = 1 for source_url
        with self.database_connection:  # Commit update
            self.database_connection.execute(
                "UPDATE collected_urls SET is_processed = 1 WHERE source_url = ?",
                (source_url,),
            )  # Update row

    def is_file_name_already_saved(
        self, file_name: str
    ) -> bool:  # Check memory, disk, and database for an existing file
        """The download folder is checked first because the files on disk are the truth; the database is only a backup record."""  # Method docstring
        if (
            file_name in self.safe_file_names_found_in_folder
            or (DOWNLOAD_FOLDER / file_name).exists()
        ):  # Check in-memory set or filesystem path
            return True  # File already exists locally
        matching_row = self.database_connection.execute(  # Fallback check in downloaded_files table
            "SELECT 1 FROM downloaded_files WHERE file_name = ?",
            (file_name,),  # Query by filename
        ).fetchone()  # Fetch one row if present
        return matching_row is not None  # True if found in DB, False otherwise

    def record_saved_file(
        self, file_name: str, source_url: str
    ) -> None:  # Save filename and source URL to DB and memory set
        with self.database_connection:  # Commit insert
            self.database_connection.execute(  # Insert record if not already present
                "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",
                (file_name, source_url),  # Bind filename and URL
            )  # Execute insert
        self.safe_file_names_found_in_folder.add(
            file_name
        )  # Also track in memory for fast lookup

    def is_file_for_url_already_downloaded(
        self, source_url: str
    ) -> bool:  # Check if the predicted file for a URL already exists
        """If the file name that the URL implies is already known or on disk, there is no need to download it."""  # Method docstring
        guessed_file_name = guess_file_name_from_url(
            source_url
        )  # Extract PDF filename from URL path
        if guessed_file_name is None:  # Cannot predict a PDF filename from this URL
            return False  # Treat as not yet downloaded
        return self.is_file_name_already_saved(
            build_safe_file_name(guessed_file_name)
        )  # Check sanitized filename existence

    def is_any_version_of_document_saved(
        self, document_key: str
    ) -> bool:  # Check if any allowed regional version of a document exists
        """A Chemical Standards document is finished as soon as one of its wanted versions has been saved."""  # Method docstring
        for (
            version_suffix
        ) in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:  # Check each suffix in priority order
            if self.is_file_name_already_saved(
                build_safe_file_name(document_key + version_suffix)
            ):  # Test if candidate filename exists
                return True  # At least one version is already saved
        return False  # No version found on disk or in DB


# ============================================================== FILE NAMES  # URL parsing, version ranking, and filename sanitization


def guess_file_name_from_url(
    source_url: str,
) -> Optional[str]:  # Predict the file name before downloading
    last_path_segment = Path(
        unquote(urlparse(source_url).path)
    ).name  # Decode URL path and take final path segment
    return (
        last_path_segment if last_path_segment.lower().endswith(".pdf") else None
    )  # Return filename if it ends with .pdf


def find_version_suffix_in_url(
    source_url: str,
) -> Optional[str]:  # Which wanted format (if any) is this address in
    lowercase_path = unquote(
        urlparse(source_url).path
    ).lower()  # Decode and lowercase URL path
    for (
        version_suffix
    ) in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:  # Check allowed suffixes in order
        if lowercase_path.endswith(
            version_suffix.lower()
        ):  # Match suffix at end of path
            return version_suffix  # Return canonical suffix string
    return None  # No allowed suffix matched


def is_wanted_version_pdf_url(
    source_url: str,
) -> bool:  # Return True if URL ends with one of the allowed PDF suffixes
    return (
        find_version_suffix_in_url(source_url) is not None
    )  # Non-None means wanted format


def get_document_key_from_url(
    source_url: str,
) -> str:  # The file name without its format ending
    version_suffix = (
        find_version_suffix_in_url(source_url) or ""
    )  # Get matching suffix or empty string
    lowercase_file_name = Path(
        unquote(urlparse(source_url).path)
    ).name.lower()  # Lowercase base filename from URL
    return lowercase_file_name[
        : len(lowercase_file_name) - len(version_suffix)
    ]  # Strip the suffix from the end


def get_version_rank_from_url(
    source_url: str,
) -> int:  # 0 is the default version; higher numbers are less preferred
    version_suffix = find_version_suffix_in_url(source_url)  # Find suffix in URL
    if version_suffix is None:  # If not a wanted version
        return len(
            ALLOWED_VERSION_SUFFIXES_BEST_FIRST
        )  # Assign lowest rank (after all wanted suffixes)
    return ALLOWED_VERSION_SUFFIXES_BEST_FIRST.index(
        version_suffix
    )  # Index in best-first tuple is its rank


def group_urls_by_document(
    source_urls,
) -> dict:  # Every version of the same document together, best version first
    urls_by_document: dict[
        str, list[str]
    ] = {}  # Map document_key -> sorted list of URLs
    for source_url in sorted(
        set(source_urls)
    ):  # Deduplicate and sort URLs deterministically
        if not is_wanted_version_pdf_url(
            source_url
        ):  # Ignore URLs not in wanted formats
            continue  # Skip to next URL
        urls_by_document.setdefault(get_document_key_from_url(source_url), []).append(
            source_url
        )  # Append URL under its document key
    for (
        candidate_urls
    ) in urls_by_document.values():  # Sort each document's URL list by version priority
        candidate_urls.sort(
            key=get_version_rank_from_url
        )  # Best version (lowest rank) comes first
    return urls_by_document  # Return grouped dictionary


def build_safe_file_name(
    raw_file_name: str,
) -> str:  # Clean a file name so it follows the naming rules
    file_name_stem = (
        raw_file_name[:-4] if raw_file_name.lower().endswith(".pdf") else raw_file_name
    )  # Strip trailing ".pdf" if present
    file_name_stem = re.sub(
        r"[^a-z0-9_]+", "_", file_name_stem.lower()
    )  # Lowercase and replace non-alphanumeric/underscore chars with '_'
    file_name_stem = re.sub(
        r"_+", "_", file_name_stem
    )  # Collapse consecutive underscores into a single '_'
    version_ending = ""  # The format ending (such as _naenglish) must never be cut off when a long name is shortened
    for (
        version_suffix
    ) in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:  # Look for a known suffix at end of stem
        if file_name_stem.endswith(
            version_suffix[:-4].lower()
        ):  # Match lowercase suffix without ".pdf"
            version_ending = version_suffix[
                :-4
            ].lower()  # Save suffix ending so it isn't truncated
            break  # Stop at first match
    document_part = file_name_stem[
        : len(file_name_stem) - len(version_ending)
    ]  # Base document portion before version suffix
    document_part = document_part[
        : MAXIMUM_FILE_NAME_STEM_LENGTH - len(version_ending)
    ].strip(
        "_"
    )  # Truncate base part to max length and trim '_'
    return (
        (document_part or "document") + version_ending + ".pdf"
    )  # Reassemble safe filename with ".pdf" extension


# ============================================================== BLOCK TRACKING  # Access-block counter and threshold logic


def register_access_block(
    run_statistics: RunStatistics,
) -> None:  # Count one more "Access Denied" in a row
    run_statistics.consecutive_block_count += 1  # Increment consecutive block counter
    download_logger.warning(  # Log current block count vs maximum allowed
        "Consecutive access blocks: %d/%d",
        run_statistics.consecutive_block_count,
        MAXIMUM_CONSECUTIVE_BLOCKS,  # Format block counters
    )  # End warning log
    if (
        run_statistics.consecutive_block_count >= MAXIMUM_CONSECUTIVE_BLOCKS
    ):  # Stop if threshold reached
        raise AccessBlockedError(  # Raise exception to halt the run safely
            f"The website blocked {MAXIMUM_CONSECUTIVE_BLOCKS} requests in a row. Stopping so the block does not get worse. Try again later or contact Agilent."  # Descriptive error message
        )  # End raise


def reset_access_blocks(
    run_statistics: RunStatistics,
) -> None:  # A page loaded fine, so the count starts again
    run_statistics.consecutive_block_count = (
        0  # Reset consecutive block counter to zero
    )


# ============================================================== PAGE CHECKS & ACTIONS  # Page inspection, waiting, scrolling, link extraction, and pagination

PAGE_UNAVAILABLE_MESSAGE = "the page may be temporarily unavailable or may no longer exist"  # Text on the website's error page


def is_access_denied_error_page(
    browser_page: Page,
) -> bool:  # Check whether the website blocked this visit
    try:  # Safely read page title and HTML content
        page_title, page_source = (
            browser_page.title(),
            browser_page.content(),
        )  # Fetch title and full DOM HTML
    except PlaywrightError:  # Ignore errors if page is mid-navigation or closed
        return False  # Assume not blocked when unreadable
    lowercase_page_source = (
        page_source.lower()
    )  # Lowercase HTML for case-insensitive substring checks
    is_blocked_by_website = (  # Evaluate known block indicators
        "access denied" in page_title.lower()  # Check title for "access denied"
        or "errors.edgesuite.net"
        in lowercase_page_source  # Check for Akamai/Edgesuite error signature
        or "you don't have permission to access"
        in lowercase_page_source  # Check for permission denied message
    )  # End block check expression
    if is_blocked_by_website:  # Log reference ID if blocked
        block_reference_match = re.search(
            r"Reference #([0-9a-fA-F.]+)", page_source
        )  # Extract Akamai reference code if present
        download_logger.warning(  # Log block warning with reference and URL
            "ACCESS DENIED page detected (reference %s) at %s",  # Format string
            block_reference_match.group(1)
            if block_reference_match
            else "unknown",  # Reference code or "unknown"
            browser_page.url,  # Current page URL
        )  # End warning log
    return is_blocked_by_website  # Return boolean block indicator


def is_page_unavailable_error_page(
    browser_page: Page,
) -> bool:  # Check for the "page may no longer exist" error page
    try:  # Safely read visible body text
        visible_text = browser_page.inner_text(
            "body", timeout=3000
        )  # Get visible text of <body>
    except PlaywrightError:  # Return False if reading fails
        return False  # Not confirmed unavailable
    normalized_text = re.sub(
        r"\s+", " ", visible_text.lower()
    )  # Collapse whitespace and lowercase text
    error_message_found = (
        PAGE_UNAVAILABLE_MESSAGE in normalized_text
    )  # Check for unavailable message
    if error_message_found:  # Log if unavailable message was found
        download_logger.warning(
            "PAGE UNAVAILABLE message found at %s", browser_page.url
        )  # Log URL
    return error_message_found  # Return boolean result


def is_results_page_usable(
    browser_page: Page, run_statistics: RunStatistics
) -> bool:  # Verify page is neither unavailable nor blocked
    if is_page_unavailable_error_page(
        browser_page
    ):  # Check for unavailable error page first
        return False  # Page is not usable
    if is_access_denied_error_page(browser_page):  # Check for Access Denied error page
        register_access_block(
            run_statistics
        )  # Increment block counter (may raise AccessBlockedError)
        return False  # Page is not usable
    reset_access_blocks(run_statistics)  # Page loaded cleanly, so reset block streak
    return True  # Page is usable


def wait_for_network_to_settle(
    browser_page: Page,
) -> None:  # Wait until the page stops loading data
    try:  # Wait for networkidle state with timeout
        browser_page.wait_for_load_state(
            "networkidle", timeout=20_000
        )  # Wait up to 20s for idle network
    except PlaywrightTimeoutError:  # Continue even if background requests keep trickling
        pass  # Ignore timeout
    time.sleep(2)  # Give the page's scripts a little extra time to draw the results


def dismiss_cookie_banner(
    browser_page: Page,
) -> None:  # Close the cookie popup if it shows up
    for accept_selector in (
        "#onetrust-accept-btn-handler",
        "button[id*='accept' i]",
    ):  # Candidate cookie accept button selectors
        try:  # Attempt to find and click accept button
            accept_button = browser_page.locator(
                accept_selector
            ).first  # First matching element
            if accept_button.is_visible():  # Click only if visible on screen
                accept_button.click(timeout=3000)  # Click accept button
                time.sleep(0.5)  # Brief pause for banner dismissal animation
                return  # Exit after dismissing banner
        except PlaywrightError:  # Try next selector if this one fails
            continue  # Continue loop


def scroll_to_bottom_of_page(
    browser_page: Page, pause_seconds: float = 0.8
) -> None:  # Make lazily loaded results appear
    previous_page_height = 0  # Track previous document height
    for _scroll_round in range(15):  # Scroll down at most 15 times
        browser_page.evaluate(
            "window.scrollTo(0, document.documentElement.scrollHeight)"
        )  # Scroll to bottom of document
        time.sleep(pause_seconds)  # Wait for lazy content to render
        current_page_height = browser_page.evaluate(
            "document.documentElement.scrollHeight"
        )  # Measure updated document height
        if (
            current_page_height == previous_page_height
        ):  # Stop if height did not increase
            break  # Reached bottom or no new content loaded
        previous_page_height = current_page_height  # Update height for next iteration


PDF_OR_SAFETY_DATA_SHEET_LINK_PATTERN = (
    re.compile(  # Regex matching PDF links or SDS/MSDS paths
        r"\.pdf(\?|#|$)|msds|/sds|sds[_/\-.]|safety[-_ ]?data",
        re.IGNORECASE,  # Case-insensitive pattern
    )
)  # Matches links that look like PDFs or safety data sheets
PDF_URL_INSIDE_PAGE_SOURCE_PATTERN = (
    re.compile(  # Regex matching full .pdf URLs embedded in HTML/JS source
        r"https?://[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?",
        re.IGNORECASE,  # Case-insensitive URL pattern
    )
)  # Matches .pdf addresses hidden inside the page source


def extract_pdf_urls_from_page(
    browser_page: Page,
) -> set:  # Every PDF address on the page that is in a wanted format
    found_urls = set()  # Collect candidate URLs from DOM links and page source
    for (
        link_url
    ) in browser_page.evaluate(  # Evaluate JS in page to extract all resolved anchor hrefs
        "() => Array.from(document.querySelectorAll('a[href]')).map(linkElement => linkElement.href)"  # Map <a> elements to href strings
    ):  # End evaluate call
        if link_url.startswith("http") and PDF_OR_SAFETY_DATA_SHEET_LINK_PATTERN.search(
            link_url
        ):  # Keep HTTP(S) links matching PDF/SDS pattern
            found_urls.add(link_url)  # Add to candidate set
    page_source_text = browser_page.content().replace(
        "\\/", "/"
    )  # Unescape forward slashes in raw HTML
    for url_found_in_source in PDF_URL_INSIDE_PAGE_SOURCE_PATTERN.findall(
        page_source_text
    ):  # Find embedded .pdf URLs in source
        found_urls.add(
            url_found_in_source.replace("\\u0026", "&").replace("&amp;", "&")
        )  # Decode escaped ampersands and add URL
    wanted_urls = {
        found_url for found_url in found_urls if is_wanted_version_pdf_url(found_url)
    }  # Filter to allowed version suffixes
    download_logger.debug(
        "Found %d PDF link(s), %d in a wanted format", len(found_urls), len(wanted_urls)
    )  # Log extraction counts
    return wanted_urls  # Return set of wanted PDF URLs


NEXT_PAGE_BUTTON_SELECTORS = [  # Selectors used to locate "Next" pagination controls on keyword search pages
    "button[aria-label*='next' i]",  # Button with 'next' in aria-label
    "a[aria-label*='next' i]",  # Link with 'next' in aria-label
    "li.next:not(.disabled) a",  # Active list-item next link
    "a.next",  # Anchor with class 'next'
    "button.next",  # Button with class 'next'
    "[class*='pagination'] [class*='next']",  # Element with 'next' class inside pagination container
    "xpath=//button[normalize-space()='Next' or normalize-space()='>']",  # Button with text 'Next' or '>'
    "xpath=//a[normalize-space()='Next' or normalize-space()='>']",  # Link with text 'Next' or '>'
]  # End next-page selectors list


def click_next_page_button(
    browser_page: Page,
) -> bool:  # Go to the next page of results if there is one
    for (
        next_button_selector
    ) in NEXT_PAGE_BUTTON_SELECTORS:  # Try each candidate selector
        matching_locator = browser_page.locator(
            next_button_selector
        )  # Create Playwright locator
        try:  # Safely count matching elements
            match_count = matching_locator.count()  # Number of matched elements
        except PlaywrightError:  # Ignore locator errors and try next selector
            continue  # Next selector
        for match_position in range(match_count):  # Inspect each matching element
            candidate_button = matching_locator.nth(
                match_position
            )  # Element at current index
            try:  # Check visibility, enabled state, and attributes
                if (
                    not candidate_button.is_visible()
                    or not candidate_button.is_enabled()
                ):  # Skip hidden or disabled controls
                    continue  # Next match
                if candidate_button.get_attribute(
                    "aria-disabled"
                ) == "true" or "disabled" in (  # Check aria-disabled and class names
                    candidate_button.get_attribute("class")
                    or ""  # Class attribute string or empty
                ):  # End disabled attribute check
                    continue  # Skip disabled button
                click_element_like_a_human(
                    browser_page, candidate_button
                )  # Click button using human-like pointer movement
                download_logger.info(
                    "Clicked next page button (%s)", next_button_selector
                )  # Log successful click
                return True  # Indicate navigation button was clicked
            except PlaywrightError:  # Continue if element cannot be interacted with
                continue  # Next match
    download_logger.info(
        "No usable next page button found"
    )  # Log when no active next button exists
    return False  # Indicate no next page click occurred


# ============================================================== HUMAN-LIKE BEHAVIOUR  # Simulated mouse, scroll, keyboard, and browsing actions
#
# Links, human-like input (curved mouse paths, wheel scrolling, random key presses) and the browsing routine.  # Section overview
# Random letters are only pressed, never submitted: Enter and other special keys are never used,  # Safe key-press rule
# and any focused text box is un-focused first, so no new search is ever started by accident.  # Focus-blur protection

LINKS_TO_AVOID_WHILE_BROWSING_PATTERN = re.compile(  # Pattern of URLs/schemes to skip during casual browsing
    r"log-?in|log-?out|sign-?in|sign-?out|register|cart|checkout|account"  # Auth, account, and shopping URLs
    r"|\.pdf|msds|/sds|mailto:|tel:|javascript:",  # PDFs, SDS links, and non-HTTP schemes
    re.IGNORECASE,  # Case-insensitive matching
)  # End links-to-avoid pattern
DESCRIBE_ALL_LINKS_SCRIPT = (  # JS snippet returning bounding box, visibility, target, and download info for all <a> links
    "() => Array.from(document.querySelectorAll('a[href]')).map((linkElement, linkIndex) => {"  # Iterate over all links with href
    "const linkBox = linkElement.getBoundingClientRect();"  # Get element bounding rectangle
    "const linkStyle = getComputedStyle(linkElement);"  # Get computed CSS styles
    "return {"  # Return object describing this link
    "index: linkIndex,"  # Original index in querySelectorAll result
    "href: linkElement.href,"  # Resolved href URL
    "visible: linkBox.width > 20 && linkBox.height > 10 && linkStyle.visibility !== 'hidden' && linkStyle.display !== 'none',"  # Check size and visibility
    "new_tab: linkElement.target === '_blank',"  # Flag if link opens in new tab
    "download: linkElement.hasAttribute('download'),"  # Flag if link has download attribute
    "};"  # End returned object
    "})"  # End map and arrow function
)  # End DESCRIBE_ALL_LINKS_SCRIPT
REMOVE_KEYBOARD_FOCUS_FROM_TEXT_BOXES_SCRIPT = (  # JS snippet that blurs active input/textarea/select/editable elements
    "() => { const focusedElement = document.activeElement;"  # Get currently focused element
    " if (focusedElement && focusedElement !== document.body && (['INPUT', 'TEXTAREA', 'SELECT'].includes(focusedElement.tagName) || focusedElement.isContentEditable)) focusedElement.blur(); }"  # Blur if form field or editable
)  # End REMOVE_KEYBOARD_FOCUS_FROM_TEXT_BOXES_SCRIPT


def pick_random_internal_link(
    browser_page: Page, visited_urls: set
) -> Optional[tuple]:  # Pick a random unvisited Agilent link on the page
    candidate_links = []  # List of eligible (link_index, link_url) tuples
    for link_description in browser_page.evaluate(
        DESCRIBE_ALL_LINKS_SCRIPT
    ):  # Inspect all links on current page
        link_url = link_description["href"]  # Extract URL string
        parsed_url = urlparse(link_url)  # Parse URL components
        website_host_name = parsed_url.hostname or ""  # Hostname or empty string
        if (
            not link_description["visible"]
            or link_description["new_tab"]
            or link_description["download"]
        ):  # Skip hidden, new-tab, or download links
            continue  # Next link
        if parsed_url.scheme not in ("http", "https"):  # Allow only HTTP and HTTPS URLs
            continue  # Next link
        if website_host_name != "agilent.com" and not website_host_name.endswith(
            ".agilent.com"
        ):  # Restrict to agilent.com domain
            continue  # Next link
        if LINKS_TO_AVOID_WHILE_BROWSING_PATTERN.search(
            link_url
        ):  # Skip sensitive or PDF/SDS URLs
            continue  # Next link
        if (
            remove_fragment_from_url(link_url) in visited_urls
        ):  # Skip already-visited URLs
            continue  # Next link
        candidate_links.append(
            (link_description["index"], link_url)
        )  # Add valid link candidate
    return (
        random.choice(candidate_links) if candidate_links else None
    )  # Return random choice or None if none available


current_mouse_x = (
    200.0  # Playwright cannot tell us where the pointer is, so we remember it
)
current_mouse_y = 200.0  # Track last known vertical pointer coordinate


def reset_mouse_position() -> None:  # Reset tracked pointer coordinates to default starting point
    global current_mouse_x, current_mouse_y  # Modify module-level pointer coordinates
    current_mouse_x, current_mouse_y = 200.0, 200.0  # Set default (x, y)


def get_visible_page_size(
    browser_page: Page,
) -> tuple:  # Return (width, height) of viewport or fallback window dimensions
    visible_page_size = (
        browser_page.viewport_size
        or {  # Get viewport dict or fallback dict
            "width": BROWSER_WINDOW_WIDTH_PIXELS,  # Fallback width
            "height": BROWSER_WINDOW_HEIGHT_PIXELS,  # Fallback height
        }
    )  # End fallback dict
    return (
        visible_page_size["width"],
        visible_page_size["height"],
    )  # Return width and height tuple


def move_mouse_like_a_human(
    browser_page: Page, pointer_target_x: float, pointer_target_y: float
) -> None:  # Move cursor along a curved eased path
    global current_mouse_x, current_mouse_y  # Update tracked pointer position
    visible_page_width, visible_page_height = get_visible_page_size(
        browser_page
    )  # Get viewport bounds
    pointer_target_x = min(
        max(pointer_target_x, 1), visible_page_width - 1
    )  # Clamp target X inside viewport
    pointer_target_y = min(
        max(pointer_target_y, 1), visible_page_height - 1
    )  # Clamp target Y inside viewport
    pointer_start_x, pointer_start_y = (
        current_mouse_x,
        current_mouse_y,
    )  # Record starting coordinates
    travel_distance = math.hypot(
        pointer_target_x - pointer_start_x, pointer_target_y - pointer_start_y
    )  # Euclidean distance to target
    if travel_distance < 2:  # Skip movement if already practically at target
        return  # Nothing to move
    movement_step_count = max(
        12, min(60, int(travel_distance / 10))
    )  # Choose step count proportional to distance
    curve_bend_amount = travel_distance * random.uniform(
        0.1, 0.3
    )  # Randomize Bezier control point offset
    control_point_x = (pointer_start_x + pointer_target_x) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()  # Control point X
    control_point_y = (pointer_start_y + pointer_target_y) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()  # Control point Y
    for movement_step_number in range(
        1, movement_step_count + 1
    ):  # Step through quadratic Bezier curve
        progress_fraction = (
            movement_step_number / movement_step_count
        )  # Linear progress in (0, 1]
        eased_progress = (
            progress_fraction * progress_fraction * (3 - 2 * progress_fraction)
        )  # Smoothstep ease-in-out curve
        curve_point_x = (  # Quadratic Bezier X coordinate
            (1 - eased_progress) ** 2 * pointer_start_x  # Start weight
            + 2
            * (1 - eased_progress)
            * eased_progress
            * control_point_x  # Control weight
            + eased_progress**2 * pointer_target_x  # End weight
        )  # End curve_point_x
        curve_point_y = (  # Quadratic Bezier Y coordinate
            (1 - eased_progress) ** 2 * pointer_start_y  # Start weight
            + 2
            * (1 - eased_progress)
            * eased_progress
            * control_point_y  # Control weight
            + eased_progress**2 * pointer_target_y  # End weight
        )  # End curve_point_y
        if (
            movement_step_number < movement_step_count
        ):  # Add slight human hand jitter except on final step
            curve_point_x += random.uniform(-1.0, 1.0)  # Small horizontal jitter
            curve_point_y += random.uniform(-1.0, 1.0)  # Small vertical jitter
        browser_page.mouse.move(
            curve_point_x, curve_point_y
        )  # Dispatch mouse move event
        time.sleep(random.uniform(0.004, 0.014))  # Short delay between steps
    current_mouse_x, current_mouse_y = (
        pointer_target_x,
        pointer_target_y,
    )  # Save final pointer position


def click_element_like_a_human(
    browser_page: Page, target_element
) -> None:  # Scroll element into view, move pointer to it, and click
    target_element.scroll_into_view_if_needed(
        timeout=5000
    )  # Ensure element is within viewport
    element_box = target_element.bounding_box()  # Get bounding box of element
    if (
        element_box is None
    ):  # Fallback to direct Playwright click if bounding box is unavailable
        target_element.click(timeout=5000)  # Click element directly
        return  # Done
    click_position_x = element_box["x"] + element_box["width"] * random.uniform(
        0.3, 0.7
    )  # Pick random X inside middle 40% of box
    click_position_y = element_box["y"] + element_box["height"] * random.uniform(
        0.3, 0.7
    )  # Pick random Y inside middle 40% of box
    move_mouse_like_a_human(
        browser_page, click_position_x, click_position_y
    )  # Move cursor smoothly to target point
    time.sleep(random.uniform(0.2, 0.6))  # Brief pause before clicking
    browser_page.mouse.click(
        current_mouse_x, current_mouse_y, delay=random.randint(50, 150)
    )  # Click with realistic press duration


def scroll_like_a_human(
    browser_page: Page, scroll_pixels: int
) -> None:  # Scroll vertically in small wheel notches with pauses
    scroll_direction = 1 if scroll_pixels > 0 else -1  # Down (+1) or up (-1)
    remaining_pixels = abs(scroll_pixels)  # Absolute pixel distance left to scroll
    while remaining_pixels > 0:  # Scroll in increments until distance is covered
        wheel_notch_pixels = min(
            remaining_pixels, random.randint(40, 120)
        )  # Random wheel step size
        browser_page.mouse.wheel(
            0, scroll_direction * wheel_notch_pixels
        )  # Emit mouse wheel event
        remaining_pixels -= wheel_notch_pixels  # Decrement remaining distance
        time.sleep(random.uniform(0.02, 0.09))  # Short delay between notches
        if random.random() < 0.08:  # Occasional reading pause while scrolling
            time.sleep(random.uniform(0.3, 0.9))  # Pause briefly


def press_random_letter_keys_like_a_human(
    browser_page: Page,
) -> None:  # Press a few harmless lowercase letters after blurring inputs
    browser_page.evaluate(
        REMOVE_KEYBOARD_FOCUS_FROM_TEXT_BOXES_SCRIPT
    )  # Ensure no input box is focused
    for _key_press_number in range(random.randint(2, 6)):  # Press between 2 and 6 keys
        random_letter = random.choice(
            string.ascii_lowercase
        )  # Pick a random lowercase letter
        browser_page.keyboard.press(
            random_letter, delay=random.randint(40, 140)
        )  # Press and release key
        time.sleep(random.uniform(0.08, 0.35))  # Pause between keystrokes
        if random.random() < 0.15:  # Occasional longer pause
            time.sleep(random.uniform(0.4, 1.0))  # Sleep briefly


def act_like_a_human_on_page(
    browser_page: Page, duration_seconds: float
) -> None:  # Perform random mouse, scroll, and key actions for duration_seconds
    activity_end_time = time.time() + duration_seconds  # Compute deadline timestamp
    visible_page_width, visible_page_height = get_visible_page_size(
        browser_page
    )  # Get viewport dimensions
    while time.time() < activity_end_time:  # Loop until activity window expires
        try:  # Perform one random human-like action
            random_action_choice = random.random()  # Roll random float in [0.0, 1.0)
            if random_action_choice < 0.2:  # 20% chance: press random letters
                press_random_letter_keys_like_a_human(browser_page)  # Press random keys
            elif random_action_choice < 0.55:  # 35% chance: scroll down
                scroll_like_a_human(
                    browser_page, random.randint(200, 650)
                )  # Scroll down by random pixels
            elif random_action_choice < 0.9:  # 35% chance: move mouse to random spot
                move_mouse_like_a_human(  # Move mouse smoothly
                    browser_page,  # Target page
                    random.uniform(
                        40, visible_page_width - 40
                    ),  # Random X inside margins
                    random.uniform(
                        40, visible_page_height - 40
                    ),  # Random Y inside margins
                )  # End move_mouse_like_a_human call
            else:  # 10% chance: scroll up slightly
                scroll_like_a_human(
                    browser_page, -random.randint(100, 300)
                )  # Scroll up by random pixels
        except PlaywrightError as browser_error:  # Handle Playwright errors during activity
            if is_browser_closed_error(
                browser_error
            ):  # Propagate browser-closed errors for restart handling
                raise  # Re-raise closed browser error
        remaining_seconds = (
            activity_end_time - time.time()
        )  # Time remaining in activity window
        if remaining_seconds > 0:  # Sleep if time remains
            time.sleep(
                min(remaining_seconds, random.uniform(0.5, 2.0))
            )  # Sleep up to remaining time


def pause_while_acting_like_a_human(  # Sleep for a random duration within [minimum_seconds, maximum_seconds] with human activity
    browser_page: Page,
    minimum_seconds: float,
    maximum_seconds: float,
    pause_reason: str,  # Page, range bounds, and log label
) -> None:  # Wait a random time while still looking active
    pause_seconds = random.uniform(
        minimum_seconds, maximum_seconds
    )  # Pick random pause length
    download_logger.debug(
        "Pausing %.1f seconds with mouse and keyboard activity (%s)",
        pause_seconds,
        pause_reason,
    )  # Log pause details
    try:  # Run human activity during pause
        act_like_a_human_on_page(
            browser_page, pause_seconds
        )  # Simulate user activity for pause_seconds
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # Fall back to plain sleep if page interaction fails
        download_logger.debug(
            "Activity during the pause failed: %s", str(browser_error).splitlines()[0]
        )  # Log short error summary
        time.sleep(pause_seconds)  # Plain sleep fallback


def open_random_link(
    browser_page: Page, visited_urls: set
) -> Optional[str]:  # Click a random unvisited internal link and wait for navigation
    for _attempt_number in range(5):  # Try up to 5 candidate links
        chosen_candidate = pick_random_internal_link(
            browser_page, visited_urls
        )  # Pick one link
        if chosen_candidate is None:  # Stop if no unvisited links remain
            return None  # No link opened
        link_position, link_url = chosen_candidate  # Unpack link index and URL
        visited_urls.add(
            remove_fragment_from_url(link_url)
        )  # Mark URL (without fragment) as visited
        page_url_before_click = remove_fragment_from_url(
            browser_page.url
        )  # Record current URL before click
        try:  # Click link and wait for URL change
            chosen_link = browser_page.locator("a[href]").nth(
                link_position
            )  # Locate link element by index
            click_element_like_a_human(
                browser_page, chosen_link
            )  # Click link like a human
            browser_page.wait_for_url(  # Wait until URL changes from previous URL
                lambda new_page_url, previous_page_url=page_url_before_click: remove_fragment_from_url(
                    str(new_page_url)
                )  # Compare URLs without fragment
                != previous_page_url,  # True once navigated to a different page
                wait_until="domcontentloaded",  # Wait for DOM content loaded
                timeout=8000,  # 8-second navigation timeout
            )  # type: ignore[misc]  # Ignore static type checker note on lambda default arg
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as browser_error:  # Handle navigation failure or timeout
            if is_browser_closed_error(
                browser_error
            ):  # Propagate if browser itself closed
                raise  # Re-raise closed error
            download_logger.debug(
                "Browsing: link %s did not open a page", link_url
            )  # Log failed link navigation
            continue  # Try another link
        return browser_page.url  # Return newly opened page URL
    return None  # Return None if all 5 attempts failed


def return_to_home_page(
    browser_page: Page, home_page_url: str
) -> None:  # Navigate back or directly to home page
    try:  # Try browser back navigation first
        browser_page.go_back(
            wait_until="domcontentloaded", timeout=15_000
        )  # Go back one history entry
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # Handle back failure
        if is_browser_closed_error(browser_error):  # Propagate if browser closed
            raise  # Re-raise closed error
    if remove_fragment_from_url(browser_page.url) != remove_fragment_from_url(
        home_page_url
    ):  # If not back on home page
        browser_page.goto(
            home_page_url, wait_until="domcontentloaded"
        )  # Navigate directly to home page URL
    time.sleep(random.uniform(1.0, 2.0))  # Short pause after returning


def browse_like_a_normal_visitor(
    browser_page: Page,
) -> None:  # Move around the home page, then open a few other pages
    try:  # Wrap warm-up browsing so transient errors don't crash the script
        home_page_url = browser_page.url  # Remember starting home URL
        visited_urls = {
            remove_fragment_from_url(home_page_url),
            remove_fragment_from_url(HOME_PAGE_URL),
        }  # Track visited URLs to avoid repeats
        download_logger.info(  # Log home page browsing duration
            "Browsing: mouse, scrolling and random keys on the home page for about %d seconds",
            HOME_PAGE_BROWSING_SECONDS,  # Format log message
        )  # End log call
        act_like_a_human_on_page(
            browser_page, HOME_PAGE_BROWSING_SECONDS
        )  # Spend configured seconds on home page
        for visit_number in range(
            1, OTHER_PAGES_TO_VISIT + 1
        ):  # Visit a few internal pages
            visit_start_time = time.time()  # Record start time of this page visit
            opened_page_url = open_random_link(
                browser_page, visited_urls
            )  # Open a random internal link
            if opened_page_url is None:  # Stop browsing if no link could be opened
                download_logger.warning(
                    "Browsing: could not open another page, stopping early"
                )  # Log early stop
                break  # Exit loop
            download_logger.info(  # Log opened subpage and planned dwell time
                "Browsing: page %d/%d opened (about %d seconds on it): %s",  # Format message
                visit_number,  # Current visit index
                OTHER_PAGES_TO_VISIT,  # Total planned subpages
                OTHER_PAGE_BROWSING_SECONDS,  # Target seconds per subpage
                opened_page_url,  # Opened URL
            )  # End log call
            seconds_left_on_page = max(
                2.0, OTHER_PAGE_BROWSING_SECONDS - (time.time() - visit_start_time)
            )  # Compute remaining dwell time (at least 2s)
            act_like_a_human_on_page(
                browser_page, seconds_left_on_page
            )  # Browse current subpage
            if (
                visit_number < OTHER_PAGES_TO_VISIT
            ):  # Return to home page between subpage visits
                return_to_home_page(browser_page, home_page_url)  # Go back to home page
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # Log if browsing ends early due to browser/timeout error
        download_logger.warning(
            "Browsing like a normal visitor ended early: %s",
            str(browser_error).splitlines()[0],
        )  # Log short error
        return  # Return cleanly
    download_logger.info(
        "Finished browsing like a normal visitor"
    )  # Log completion of normal browsing


def warm_up_session_on_home_page(
    browser_page: Page,
) -> bool:  # Open the home page and browse around a little first
    download_logger.info(
        "Warm-up: opening the home page %s", HOME_PAGE_URL
    )  # Log warm-up start
    try:  # Navigate to Agilent home page
        browser_page.goto(
            HOME_PAGE_URL, wait_until="domcontentloaded"
        )  # Load home page
    except PlaywrightError as browser_error:  # Log error if home page fails to open
        download_logger.error(
            "Warm-up: could not open the home page: %s", browser_error
        )  # Log failure
        return False  # Warm-up failed
    wait_for_network_to_settle(
        browser_page
    )  # Wait for initial network requests to finish
    dismiss_cookie_banner(browser_page)  # Dismiss cookie consent popup if present
    if is_access_denied_error_page(
        browser_page
    ):  # Check if home page itself returned Access Denied
        download_logger.error(
            "Warm-up: the home page itself shows Access Denied"
        )  # Log block on home page
        return False  # Warm-up failed
    browse_like_a_normal_visitor(
        browser_page
    )  # Perform normal visitor browsing behavior
    return True  # Warm-up succeeded


PAGE_STATUS_OPENED = (
    "opened"  # Status constant: page opened and not blocked/unavailable
)
PAGE_STATUS_UNAVAILABLE = (
    "unavailable"  # Status constant: page shows unavailable message
)
PAGE_STATUS_BLOCKED = "blocked"  # Status constant: page blocked by Access Denied
PAGE_STATUS_FAILED = "failed"  # Status constant: navigation failed with an error


def open_page_politely(
    browser_page: Page, page_url: str, run_statistics: RunStatistics
) -> str:  # Open page with retry/backoff on blocks
    """Open a page; if blocked, wait, visit the home page again, and retry a limited number of times."""  # Function docstring
    for attempt_number in range(
        1, MAXIMUM_ACCESS_DENIED_ATTEMPTS + 1
    ):  # Try up to MAXIMUM_ACCESS_DENIED_ATTEMPTS times
        download_logger.info(
            "Opening %s (attempt %d/%d)",
            page_url,
            attempt_number,
            MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        )  # Log attempt
        try:  # Navigate to target URL
            browser_page.goto(page_url, wait_until="domcontentloaded")  # Load page
        except PlaywrightError as browser_error:  # Handle Playwright navigation errors
            if is_browser_closed_error(
                browser_error
            ):  # Re-raise if browser closed so restart logic runs
                raise  # Propagate closed browser error
            download_logger.error(
                "Could not open %s: %s", page_url, browser_error
            )  # Log navigation error
            return PAGE_STATUS_FAILED  # Return failed status
        wait_for_network_to_settle(browser_page)  # Wait for network activity to settle
        if is_page_unavailable_error_page(browser_page):  # Check if page is unavailable
            return PAGE_STATUS_UNAVAILABLE  # Return unavailable status
        if not is_access_denied_error_page(
            browser_page
        ):  # Check if page is not blocked
            reset_access_blocks(run_statistics)  # Reset consecutive block counter
            return PAGE_STATUS_OPENED  # Return opened status
        register_access_block(
            run_statistics
        )  # Increment block counter (may raise AccessBlockedError)
        if (
            attempt_number < MAXIMUM_ACCESS_DENIED_ATTEMPTS
        ):  # Back off and re-warm session if attempts remain
            backoff_seconds = ACCESS_DENIED_BACKOFF_SECONDS * (
                2 ** (attempt_number - 1)
            ) + random.uniform(
                0, 5
            )  # Exponential backoff plus jitter
            download_logger.info(
                "Backing off for %.0f seconds, then revisiting the home page",
                backoff_seconds,
            )  # Log backoff wait
            time.sleep(backoff_seconds)  # Sleep for backoff duration
            warm_up_session_on_home_page(
                browser_page
            )  # Re-visit home page to refresh session
    download_logger.error(
        "Still blocked after %d attempt(s): %s",
        MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        page_url,
    )  # Log final block failure
    return PAGE_STATUS_BLOCKED  # Return blocked status


# ============================================================== DOWNLOADING  # PDF download and fallback mechanisms
#
# Order of attempts for one PDF address (this is what makes the Safety Data Sheets downloads work):  # Download strategy header
#   1. Open the address in the download tab. The browser either starts a download, or shows the PDF  # Step 1 explanation
#      (https://www.agilent.com/library/msds/<name>.pdf). When it shows the PDF, the bytes of that very page are saved.  # Inline PDF handling
#   2. If that gave nothing, request the PDF directly, reusing the browser's cookies.  # Step 2 fallback explanation
#   3. If that gave nothing, look for a "download" button on the opened page and click it.  # Step 3 fallback explanation

DOWNLOAD_OUTCOME_STARTED = "download started"  # The browser started downloading a file
DOWNLOAD_OUTCOME_PDF_PAGE = (
    "pdf page"  # The browser showed the PDF; its bytes were read from that page
)
DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE = (
    "page unavailable"  # Outcome constant: page unavailable message shown
)
DOWNLOAD_OUTCOME_ACCESS_DENIED = (
    "access denied"  # Outcome constant: Access Denied shown
)
DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD = (
    "not a download"  # The link led to an ordinary web page
)
DOWNLOAD_OUTCOME_TIMED_OUT = (
    "timeout"  # Outcome constant: timed out waiting for download
)

PDF_CONTENT_TYPE_HINTS = (
    "pdf",
    "octet-stream",
    "force-download",
)  # Content types that mean "this is a file"


def start_browser_download(
    browser_page: Page, source_url: str
) -> tuple:  # Navigate to source_url and capture Download, PDF bytes, or outcome
    """Open a URL; returns (download or pdf bytes or None, outcome)."""  # Function docstring
    first_started_download = None  # Store first Download object emitted by page

    def remember_first_download(
        started_download: Download,
    ) -> None:  # Event callback capturing first Download
        nonlocal first_started_download  # Reference outer variable
        if first_started_download is None:  # Record only the first download event
            first_started_download = started_download  # Save Download reference

    browser_page.on(
        "download", remember_first_download
    )  # Attach download listener before navigating
    try:  # Ensure listener is removed in finally block
        try:  # Start navigation to source_url
            navigation_response = browser_page.goto(
                source_url, wait_until="commit", timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS
            )  # Wait for initial response commit
        except PlaywrightError as navigation_error:  # Handle errors thrown when navigation triggers a download
            if "Download is starting" in str(
                navigation_error
            ) or "NS_BINDING_ABORTED" in str(
                navigation_error
            ):  # Check known download-trigger messages
                navigation_response = (
                    None  # The navigation became a download straight away
                )
            else:  # Any other Playwright error should propagate
                raise  # Re-raise unexpected navigation error

        if navigation_response is not None:  # Inspect HTTP response if one was returned
            response_content_type = navigation_response.headers.get(
                "content-type", ""
            ).lower()  # Lowercase Content-Type header
            download_logger.debug(  # Log HTTP status and content-type
                "  Server answered HTTP %s, content-type '%s' for %s",  # Format string
                navigation_response.status,  # HTTP status code
                response_content_type,  # Content-Type value
                source_url,  # Requested URL
            )  # End debug log
            if (
                "text/html" in response_content_type
            ):  # The server answered with a web page
                try:  # Wait for HTML DOM to load
                    browser_page.wait_for_load_state(
                        "domcontentloaded", timeout=15_000
                    )  # Wait up to 15s for DOM
                except PlaywrightTimeoutError:  # Ignore DOM load timeout
                    pass  # Continue checking page content
                if is_page_unavailable_error_page(
                    browser_page
                ):  # Check if page says unavailable
                    return (
                        None,
                        DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE,
                    )  # Return unavailable outcome
                if is_access_denied_error_page(
                    browser_page
                ):  # Check if page says Access Denied
                    return (
                        None,
                        DOWNLOAD_OUTCOME_ACCESS_DENIED,
                    )  # Return access denied outcome
                return (
                    None,
                    DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD,
                )  # Otherwise it's an HTML page, not a download
            if any(
                content_type_hint in response_content_type
                for content_type_hint in PDF_CONTENT_TYPE_HINTS
            ):  # Check if response looks like binary/PDF
                try:  # The browser shows the PDF: read the bytes of this very page
                    pdf_bytes = (
                        navigation_response.body()
                    )  # Read raw response body bytes
                except PlaywrightError as body_error:  # Handle failure reading response body
                    pdf_bytes = None  # Clear pdf_bytes on error
                    download_logger.debug(
                        "  Could not read the PDF bytes of the page: %s",
                        str(body_error).splitlines()[0],
                    )  # Log short error
                if pdf_bytes and pdf_bytes[:4] == b"%PDF":  # Verify PDF magic header
                    return (
                        pdf_bytes,
                        DOWNLOAD_OUTCOME_PDF_PAGE,
                    )  # Return raw bytes and PDF page outcome

        wait_limit_seconds = (  # Choose how long to wait for a download event
            PDF_RESPONSE_DOWNLOAD_WAIT_SECONDS
            if navigation_response is not None
            else DOWNLOAD_START_TIMEOUT_MILLISECONDS
            / 1000  # Shorter wait if server already responded
        )  # A real answer without a download event means the PDF is shown inline, so do not wait long
        download_wait_deadline = (
            time.time() + wait_limit_seconds
        )  # Compute deadline timestamp
        while (
            first_started_download is None and time.time() < download_wait_deadline
        ):  # Poll until download starts or timeout expires
            browser_page.wait_for_timeout(
                200
            )  # Yield to Playwright event loop for 200ms
        if first_started_download is not None:  # Return Download object if one started
            return (
                first_started_download,
                DOWNLOAD_OUTCOME_STARTED,
            )  # Return download and started outcome
        return (
            None,
            DOWNLOAD_OUTCOME_TIMED_OUT,
        )  # Return None and timeout outcome if nothing started
    finally:  # Always remove temporary download listener
        browser_page.remove_listener(
            "download", remember_first_download
        )  # Detach listener


URL_RESULT_SAVED = "saved"  # Result constant: PDF saved to disk
URL_RESULT_FILE_ALREADY_EXISTS = (
    "file already exists"  # Result constant: target file already exists
)
URL_RESULT_VERSION_NOT_AVAILABLE = "version not available"  # This version does not exist, so the next best version may be tried
URL_RESULT_TRY_AGAIN_LATER = "try again later"  # A temporary problem; no lower-priority version may be used instead


def mark_versions_as_skipped(  # Mark a list of version URLs as processed and increment skipped counter
    download_database: DownloadDatabase,
    version_urls: list,
    run_statistics: RunStatistics,
    reason_text: str,  # DB, URLs, stats, and skip reason
) -> None:  # No return value
    for version_url in version_urls:  # Iterate over each version URL
        download_logger.info(
            "  SKIP: %s: %s", reason_text, version_url
        )  # Log skip reason and URL
        download_database.mark_url_processed(version_url)  # Mark URL as processed in DB
        run_statistics.skipped_count += 1  # Increment skipped counter


def skip_documents_already_in_download_folder(  # Filter out document URLs where any allowed version is already saved
    download_database: DownloadDatabase,
    found_urls: set,
    run_statistics: RunStatistics,  # DB, discovered URLs, and stats
) -> set:  # Return set of URLs still needing download
    """Documents that already have a file are marked as finished and never requested."""  # Function docstring
    urls_still_needed: set[
        str
    ] = set()  # Accumulate URLs for documents without a saved file
    for document_key, version_urls in group_urls_by_document(
        found_urls
    ).items():  # Group candidate URLs by document key
        if download_database.is_any_version_of_document_saved(
            document_key
        ):  # Check if document already has a saved version
            mark_versions_as_skipped(  # Skip all versions of this already-saved document
                download_database,
                version_urls,
                run_statistics,
                "this document already has a file in the download folder",  # Pass skip reason
            )  # End mark_versions_as_skipped call
        else:  # Document not yet saved
            urls_still_needed.update(version_urls)  # Keep its candidate URLs
    return urls_still_needed  # Return remaining URLs


def save_pdf_bytes(  # Validate/write PDF bytes to disk and record in DB
    pdf_bytes: bytes,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,  # Bytes, source URL, DB, and stats
) -> str:  # Return URL result status
    """Save PDF bytes that were read from the browser page or from a direct request."""  # Function docstring
    safe_file_name = build_safe_file_name(
        guess_file_name_from_url(source_url) or "document.pdf"
    )  # Build sanitized target filename
    if download_database.is_file_name_already_saved(
        safe_file_name
    ):  # Avoid overwriting existing file
        download_logger.info(
            "  DUPLICATE file name, not saved: %s", safe_file_name
        )  # Log duplicate skip
        download_database.mark_url_processed(source_url)  # Mark URL as processed
        run_statistics.skipped_count += 1  # Increment skipped count
        return URL_RESULT_FILE_ALREADY_EXISTS  # Return already-exists result
    safe_file_path = DOWNLOAD_FOLDER / safe_file_name  # Full destination file path
    safe_file_path.write_bytes(pdf_bytes)  # Write PDF bytes to disk
    download_database.record_saved_file(
        safe_file_name, source_url
    )  # Record saved file in DB and memory
    download_database.mark_url_processed(source_url)  # Mark source URL as processed
    run_statistics.downloaded_count += 1  # Increment downloaded count
    download_logger.info(
        "  SAVED: %s (%d bytes)", safe_file_path, len(pdf_bytes)
    )  # Log saved file and byte size
    return URL_RESULT_SAVED  # Return saved result


def fetch_pdf_bytes_with_browser_cookies(
    download_page: Page, source_url: str
) -> Optional[bytes]:  # Fetch PDF via browser context request API
    """Fallback: request the PDF directly, reusing the browser's cookies."""  # Function docstring
    try:  # Perform HTTP GET using context request client
        api_response = download_page.context.request.get(  # Send GET request sharing session cookies
            source_url,  # Target PDF URL
            timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS,  # Request timeout
            headers={
                "Accept": "application/pdf,*/*",
                "Referer": SDS_BASE_URL,
            },  # Set Accept and Referer headers
        )  # End GET request
        if not api_response.ok:  # Check for non-2xx HTTP status
            download_logger.warning(
                "  Direct request answered HTTP %s for %s",
                api_response.status,
                source_url,
            )  # Log HTTP status warning
            return None  # Return None on HTTP failure
        response_body = api_response.body()  # Read response body bytes
    except PlaywrightError as browser_error:  # Handle Playwright request errors
        if is_browser_closed_error(browser_error):  # Re-raise if browser closed
            raise  # Propagate closed error
        download_logger.warning(
            "  Direct request failed for %s: %s",
            source_url,
            str(browser_error).splitlines()[0],
        )  # Log request failure
        return None  # Return None on error
    if response_body[:4] != b"%PDF":  # Verify response starts with PDF header
        download_logger.warning(
            "  Direct request did not return a PDF for %s", source_url
        )  # Log non-PDF response
        return None  # Return None if not a PDF
    return response_body  # Return valid PDF bytes


VIEWER_DOWNLOAD_BUTTON_SELECTORS = (  # Candidate selectors for download buttons inside a PDF viewer or page
    "#downloadButton",  # PDF.js download button ID
    "#download",  # Generic download element ID
    "button[title*='Download' i]",  # Button with 'Download' in title
    "button[aria-label*='Download' i]",  # Button with 'Download' in aria-label
    "a[download]",  # Anchor tag with download attribute
)  # End viewer download button selectors


def click_viewer_download_button(
    download_page: Page,
) -> Optional[Download]:  # Click a download button on page and wait for Download event
    """Last resort: click a download button on the opened page and catch the download."""  # Function docstring
    for (
        button_selector
    ) in VIEWER_DOWNLOAD_BUTTON_SELECTORS:  # Try each selector in order
        try:  # Locate and click button while awaiting download
            download_button = download_page.locator(
                button_selector
            ).first  # First matching button
            if download_button.count() == 0:  # Skip if element does not exist
                continue  # Try next selector
            with download_page.expect_download(
                timeout=15_000
            ) as download_event_info:  # Wait up to 15s for download after click
                download_button.click(timeout=5000)  # Click the download button
            return download_event_info.value  # Return captured Download object
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as browser_error:  # Handle click or timeout failure
            if is_browser_closed_error(browser_error):  # Propagate if browser closed
                raise  # Re-raise closed error
            continue  # Try next selector
    return None  # Return None if no download triggered


def try_fallback_downloads(  # Try direct cookie-authenticated fetch, then clicking a page download button
    download_page: Page,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,  # Page, URL, DB, and stats
) -> Optional[str]:  # Return URL result string or None
    """Used when the browser did not hand over a file; returns a URL result, or None when nothing worked."""  # Function docstring
    download_logger.info(
        "  The browser did not hand over a file, trying a direct request"
    )  # Log fallback step 1
    fetched_pdf_bytes = fetch_pdf_bytes_with_browser_cookies(
        download_page, source_url
    )  # Attempt direct request with cookies
    if fetched_pdf_bytes is not None:  # Save if valid PDF bytes returned
        return save_pdf_bytes(
            fetched_pdf_bytes, source_url, download_database, run_statistics
        )  # Save and return result
    download_logger.info(
        "  Direct request gave nothing, looking for a download button on the page"
    )  # Log fallback step 2
    button_download = click_viewer_download_button(
        download_page
    )  # Attempt clicking a download button
    if button_download is not None:  # Save if a Download object was produced
        return check_and_save_finished_download(
            button_download, source_url, download_database, run_statistics
        )  # Validate, save, and return result
    return None  # Return None when all fallbacks fail


def download_single_url(  # Download one PDF URL using browser navigation and fallbacks, updating stats/DB
    download_page: Page,  # Tab used for downloading
    download_database: DownloadDatabase,  # Database instance
    source_url: str,  # PDF URL to download
    run_statistics: RunStatistics,  # Run statistics instance
    internet_monitor: "InternetMonitor",  # Internet monitor instance
) -> str:  # Download one version and say what happened
    try:  # Start browser download for source_url
        download_result, download_outcome = start_browser_download(
            download_page, source_url
        )  # Attempt browser load/download
    except PlaywrightError as browser_error:  # Handle Playwright errors during download start
        if is_browser_closed_error(browser_error):  # Re-raise if browser closed
            raise  # The restart logic deals with it; this URL stays pending
        if internet_monitor.confirm_internet_is_down():  # Check if internet went down
            raise InternetOutageError(
                "The internet went down while downloading"
            ) from browser_error  # Raise outage error
        download_logger.error(
            "  FAILED to open %s: %s", source_url, str(browser_error).splitlines()[0]
        )  # Log failure
        run_statistics.failed_count += 1  # Increment failed counter
        return URL_RESULT_TRY_AGAIN_LATER  # Retry later

    if (
        download_outcome == DOWNLOAD_OUTCOME_PDF_PAGE
    ):  # The PDF was shown by the browser and its bytes were read
        reset_access_blocks(run_statistics)  # Reset block counter on success
        return save_pdf_bytes(
            download_result, source_url, download_database, run_statistics
        )  # Save PDF bytes

    if download_outcome in (
        DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD,
        DOWNLOAD_OUTCOME_TIMED_OUT,
    ):  # Try fallbacks if no file handed over yet
        if (
            download_outcome == DOWNLOAD_OUTCOME_TIMED_OUT
            and internet_monitor.confirm_internet_is_down()
        ):  # Check for outage on timeout
            raise InternetOutageError(
                "The internet went down while downloading"
            )  # Raise outage error
        fallback_result = try_fallback_downloads(
            download_page, source_url, download_database, run_statistics
        )  # Run fallback methods
        if fallback_result is not None:  # Return fallback result if it succeeded
            reset_access_blocks(run_statistics)  # Reset block counter
            return fallback_result  # Return saved/duplicate result

    if (
        download_outcome == DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE
    ):  # Page explicitly unavailable
        download_logger.warning(
            "  SKIP: page unavailable, no PDF at %s", source_url
        )  # Log unavailable warning
        download_database.mark_url_processed(
            source_url
        )  # Mark URL processed so it won't repeat
        run_statistics.unavailable_count += 1  # Increment unavailable counter
        reset_access_blocks(run_statistics)  # Reset block counter
        return URL_RESULT_VERSION_NOT_AVAILABLE  # Allow trying next version if any
    if download_outcome == DOWNLOAD_OUTCOME_ACCESS_DENIED:  # Access denied by server
        download_logger.error(
            "  FAILED: access denied for %s", source_url
        )  # Log access denied error
        run_statistics.failed_count += 1  # Increment failed counter
        register_access_block(run_statistics)  # Register block (may stop run)
        return URL_RESULT_TRY_AGAIN_LATER  # Retry later
    if (
        download_outcome == DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD
    ):  # Regular HTML page rather than PDF
        download_logger.warning(
            "  SKIP: the link is a web page, not a PDF, and no fallback worked: %s",
            source_url,
        )  # Log skip warning
        download_database.mark_url_processed(source_url)  # Mark URL processed
        run_statistics.skipped_count += 1  # Increment skipped counter
        reset_access_blocks(run_statistics)  # Reset block counter
        return URL_RESULT_VERSION_NOT_AVAILABLE  # Try next version if any
    if download_result is None:  # Timed out and no fallback worked
        download_logger.error(
            "  FAILED: no download (%s) for %s", download_outcome, source_url
        )  # Log timeout failure
        run_statistics.failed_count += 1  # Increment failed counter
        return URL_RESULT_TRY_AGAIN_LATER  # Retry later
    reset_access_blocks(run_statistics)  # Reset block counter for active Download
    return check_and_save_finished_download(
        download_result, source_url, download_database, run_statistics
    )  # Complete and save Download object


def check_and_save_finished_download(  # Wait for Download completion, verify %PDF header, deduplicate, and save
    started_download: Download,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,  # Download, URL, DB, and stats
) -> str:  # Return URL result status
    """Check that the file is a real PDF, give it a clean name, skip duplicates, and save it."""  # Function docstring
    suggested_file_name = (
        started_download.suggested_filename
    )  # Browser-suggested filename
    download_failure = started_download.failure()  # Waits for the download to end
    if download_failure:  # Check if download reported an error
        download_logger.error(
            "  FAILED: browser download error '%s' for %s", download_failure, source_url
        )  # Log download failure
        run_statistics.failed_count += 1  # Increment failed counter
        return URL_RESULT_TRY_AGAIN_LATER  # Retry later

    with open(
        started_download.path(), "rb"
    ) as downloaded_file_handle:  # Open downloaded temp file in binary read mode
        file_start_bytes = downloaded_file_handle.read(
            4
        )  # Read first 4 bytes to check PDF signature
    if file_start_bytes != b"%PDF":  # Reject files that do not begin with %PDF
        download_logger.error(
            "  FAILED: downloaded file is not a PDF, treating this version as unavailable: %s",
            source_url,
        )  # Log non-PDF error
        started_download.delete()  # Remove temporary file
        download_database.mark_url_processed(source_url)  # Mark URL processed
        run_statistics.failed_count += 1  # Increment failed counter
        return URL_RESULT_VERSION_NOT_AVAILABLE  # Treat version as unavailable

    safe_file_name = build_safe_file_name(
        suggested_file_name
    )  # Sanitize suggested filename
    safe_file_path = DOWNLOAD_FOLDER / safe_file_name  # Target path in download folder
    if download_database.is_file_name_already_saved(
        safe_file_name
    ):  # Skip if file already exists
        download_logger.info(
            "  DUPLICATE file name, not saved: %s", safe_file_name
        )  # Log duplicate skip
        started_download.delete()  # Delete temporary download file
        download_database.mark_url_processed(source_url)  # Mark URL processed
        run_statistics.skipped_count += 1  # Increment skipped counter
        return URL_RESULT_FILE_ALREADY_EXISTS  # Return already-exists result

    started_download.save_as(
        safe_file_path
    )  # Move/save completed download to target path
    download_database.record_saved_file(
        safe_file_name, source_url
    )  # Record file in DB and memory
    download_database.mark_url_processed(source_url)  # Mark source URL processed
    run_statistics.downloaded_count += 1  # Increment downloaded counter
    download_logger.info(
        "  SAVED: %s (%d bytes)", safe_file_path, safe_file_path.stat().st_size
    )  # Log saved file path and size
    return URL_RESULT_SAVED  # Return saved result


def download_best_version_of_each_document(  # Iterate through documents and try candidate versions in priority order
    download_page: Page,  # Download tab
    download_database: DownloadDatabase,  # Database instance
    documents_to_download: dict,  # Mapping of document_key -> sorted candidate URLs
    run_statistics: RunStatistics,  # Run statistics instance
    internet_monitor: "InternetMonitor",  # Internet monitor instance
    page_for_mouse_activity: Page,  # Page used to perform human-like pauses between downloads
) -> None:  # No return value
    """Try the versions of each document best first, stop at the first one that is saved, skip all the others."""  # Function docstring
    already_contacted_website = (
        False  # Track whether at least one download request has been made
    )
    for document_position, (document_key, candidate_urls) in enumerate(
        documents_to_download.items(), start=1
    ):  # Iterate over each document
        download_logger.info(  # Log document progress
            "(%d/%d) Document '%s' has %d version(s) to try, best first",  # Format string
            document_position,  # 1-based document index
            len(documents_to_download),  # Total documents in batch
            document_key,  # Document identifier
            len(candidate_urls),  # Number of candidate URLs
        )  # End info log
        for version_position, source_url in enumerate(
            candidate_urls
        ):  # Try versions from highest to lowest priority
            internet_monitor.raise_error_if_internet_is_down()  # Abort step if internet is down
            lower_priority_urls = candidate_urls[
                version_position + 1 :
            ]  # Remaining fallback versions for this document
            download_logger.info(
                "  Trying version %d of %d: %s",
                version_position + 1,
                len(candidate_urls),
                source_url,
            )  # Log version attempt

            if download_database.is_file_for_url_already_downloaded(
                source_url
            ):  # Skip if this exact URL's file already exists
                download_logger.info(
                    "  SKIP: the file for this URL already exists"
                )  # Log existing file skip
                download_database.mark_url_processed(source_url)  # Mark URL processed
                run_statistics.skipped_count += 1  # Increment skipped counter
                url_result = URL_RESULT_FILE_ALREADY_EXISTS  # Set result status
            else:  # Need to download this URL
                if (
                    already_contacted_website
                ):  # Pause between consecutive network downloads
                    page_for_mouse_activity.bring_to_front()  # Focus results page during pause
                    pause_while_acting_like_a_human(  # Pause with mouse/scroll/key activity
                        page_for_mouse_activity,
                        *PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,
                        "between downloads",  # Pass range and reason
                    )  # End pause
                already_contacted_website = (
                    True  # Mark that website has now been contacted
                )
                url_result = download_single_url(  # Attempt downloading this version
                    download_page,
                    download_database,
                    source_url,
                    run_statistics,
                    internet_monitor,  # Pass required arguments
                )  # End download_single_url call

            if url_result in (
                URL_RESULT_SAVED,
                URL_RESULT_FILE_ALREADY_EXISTS,
            ):  # Stop trying lower versions once one is saved/exists
                mark_versions_as_skipped(  # Mark remaining lower-priority versions as skipped
                    download_database,
                    lower_priority_urls,
                    run_statistics,
                    "a better version of this document is already saved",  # Pass reason
                )  # End mark_versions_as_skipped
                break  # Move to next document
            if (
                url_result == URL_RESULT_TRY_AGAIN_LATER
            ):  # Do not fall back to lower version on temporary error
                download_logger.warning(  # Log that fallback is deferred
                    "  This version will be retried later, so no lower-priority version is downloaded now"  # Warning message
                )  # End warning log
                break  # Move to next document
            if lower_priority_urls:  # Current version unavailable and fallbacks remain
                download_logger.info(
                    "  This version is not available, trying the next best version"
                )  # Log fallback attempt
            else:  # No more versions left for this document
                download_logger.warning(
                    "  No wanted version of this document could be downloaded, skipping it"
                )  # Log exhausted versions

    log_run_statistics(
        run_statistics, "Totals so far"
    )  # Log running totals after batch finishes


def keep_only_documents_with_default_version(
    urls_grouped_by_document: dict,
) -> dict:  # Filter to documents whose best URL is rank 0 (_NAEnglish.pdf)
    return {  # Return filtered dictionary
        document_key: [candidate_urls[0]]  # Keep only the top-ranked URL
        for document_key, candidate_urls in urls_grouped_by_document.items()  # Iterate over grouped documents
        if candidate_urls
        and get_version_rank_from_url(candidate_urls[0])
        == 0  # Require rank 0 (default version)
    }  # End dict comprehension


def select_documents_with_default_version(
    download_database: DownloadDatabase, found_urls: set
) -> dict:  # Pick unprocessed URLs with default version
    pending_urls = download_database.keep_only_unprocessed_urls(
        sorted(found_urls)
    )  # Keep only unprocessed URLs
    return keep_only_documents_with_default_version(
        group_urls_by_document(pending_urls)
    )  # Group and keep rank-0 documents


def select_documents_for_final_download(
    download_database: DownloadDatabase, some_keywords_were_not_searched: bool
) -> dict:  # Prepare final download batch after keyword loop
    urls_grouped_by_document = group_urls_by_document(
        download_database.load_unprocessed_urls()
    )  # Group all remaining unprocessed URLs
    if (
        some_keywords_were_not_searched
    ):  # Only download default versions if some keywords failed
        download_logger.warning(  # Warn that fallback versions are deferred
            "Some keywords were not searched, so only default versions are downloaded now; fallback versions wait for the next run"  # Warning message
        )  # End warning log
        urls_grouped_by_document = keep_only_documents_with_default_version(
            urls_grouped_by_document
        )  # Restrict to default versions
    return urls_grouped_by_document  # Return final document map


def download_documents_using_temporary_tab(  # Open a temporary tab only when needed to download remaining documents
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    documents_to_download: dict,  # Documents to download
    run_statistics: RunStatistics,  # Run statistics instance
) -> None:  # No return value
    """Skip documents that already have a file, then open the download tab only if something is left to do."""  # Function docstring
    documents_still_needed = {}  # Dictionary of documents that still need downloading
    for (
        document_key,
        candidate_urls,
    ) in documents_to_download.items():  # Check each document
        if download_database.is_any_version_of_document_saved(
            document_key
        ):  # Skip if already saved
            mark_versions_as_skipped(  # Mark versions as skipped
                download_database,
                candidate_urls,
                run_statistics,
                "this document already has a saved version",  # Skip reason
            )  # End mark_versions_as_skipped
        else:  # Add document to needed map
            documents_still_needed[
                document_key
            ] = candidate_urls  # Store candidate URLs
    if not documents_still_needed:  # Avoid opening a tab if nothing needs downloading
        download_logger.info(
            "Nothing to download in this batch, so no second tab is opened"
        )  # Log no-op
        return  # Exit early
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Verify internet is up before opening tab
    with browser_session.temporary_download_tab() as download_page:  # Open temporary download tab and close on exit
        download_best_version_of_each_document(  # Download best versions for all needed documents
            download_page,  # Temporary tab
            download_database,  # Database
            documents_still_needed,  # Needed documents
            run_statistics,  # Statistics
            browser_session.internet_monitor,  # Internet monitor
            browser_session.active_results_page,  # Results tab for pauses
        )  # End download_best_version_of_each_document


# ============================================================== CHEMICAL STANDARDS (KEYWORD SEARCH)  # Job 1 keyword search and pagination


def search_one_keyword_and_download_pdfs(  # Search one keyword across pages and download default PDFs
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    search_keyword: str,  # Single search keyword (letter or digit)
    run_statistics: RunStatistics,  # Run statistics instance
) -> bool:  # Search one keyword; returns True when it was searched
    results_page = browser_session.active_results_page  # Primary results page
    browser_session.close_unneeded_tabs()  # Ensure only needed tabs are open
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before navigating
    search_url = SEARCH_URL_TEMPLATE.format(
        search_keyword=search_keyword
    )  # Format search URL with keyword
    download_logger.info(
        "Opening search page for keyword '%s': %s", search_keyword, search_url
    )  # Log search page URL

    page_status = open_page_politely(
        results_page, search_url, run_statistics
    )  # Open search page with block handling
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):  # Detect outage on failure
        raise InternetOutageError(
            "The internet went down while opening the search page"
        )  # Raise outage error
    if (
        page_status != PAGE_STATUS_OPENED
    ):  # Skip keyword if page could not be opened cleanly
        download_logger.warning(
            "[%s] skipping this keyword for now (page status: %s)",
            search_keyword,
            page_status,
        )  # Log skip status
        return False  # Keyword not completed
    dismiss_cookie_banner(results_page)  # Dismiss cookie banner if present
    pause_while_acting_like_a_human(  # Brief human activity after opening search page
        results_page,
        *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
        "after opening the search page",  # Range and context
    )  # End pause

    all_keyword_urls: set[
        str
    ] = set()  # Track all URLs seen across pages for this keyword
    for page_number in range(
        1, MAXIMUM_RESULT_PAGES_PER_KEYWORD + 1
    ):  # Paginate up to safety cap
        browser_session.close_unneeded_tabs()  # Close any stray tabs
        browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before each page
        results_page.bring_to_front()  # Bring results tab to front
        if page_number > 1:  # Pause after loading subsequent pages
            pause_while_acting_like_a_human(  # Human-like activity on result page
                results_page,
                *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
                "after opening a result page",  # Range and context
            )  # End pause
        scroll_to_bottom_of_page(results_page)  # Scroll down to trigger lazy rendering

        urls_on_this_page = extract_pdf_urls_from_page(
            results_page
        )  # Extract wanted PDF URLs from current page
        new_urls = (
            urls_on_this_page - all_keyword_urls
        )  # URLs not yet seen for this keyword
        all_keyword_urls |= urls_on_this_page  # Add current page URLs to keyword set
        download_logger.info(  # Log per-page link counts
            "[%s] page %d: %d link(s), %d new, %d total",  # Format string
            search_keyword,  # Current keyword
            page_number,  # 1-based page index
            len(urls_on_this_page),  # Links on this page
            len(new_urls),  # New links on this page
            len(all_keyword_urls),  # Cumulative links for keyword
        )  # End info log

        if new_urls:  # Save new URLs and immediately download default versions
            download_database.save_newly_found_urls(new_urls)  # Persist new URLs to DB
            urls_without_files = skip_documents_already_in_download_folder(
                download_database, new_urls, run_statistics
            )  # Skip docs already on disk
            default_version_documents = select_documents_with_default_version(
                download_database, urls_without_files
            )  # Pick default-version docs
            download_logger.info(  # Log immediate default downloads count
                "[%s] page %d: downloading %d default version(s) right away; fallback versions wait until every keyword is searched",  # Format string
                search_keyword,  # Current keyword
                page_number,  # Current page
                len(
                    default_version_documents
                ),  # Number of default docs to download now
            )  # End info log
            download_documents_using_temporary_tab(  # Download default versions using temporary tab
                browser_session,
                download_database,
                default_version_documents,
                run_statistics,  # Pass session, DB, docs, and stats
            )  # End download call

        if page_number > 1 and not new_urls:  # Stop if a later page adds no new links
            download_logger.info(
                "[%s] no new links on this page, stopping", search_keyword
            )  # Log stop reason
            break  # End pagination loop

        pause_while_acting_like_a_human(  # Pause before clicking next page
            results_page,
            *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
            "before the next result page",  # Range and context
        )  # End pause
        results_page.bring_to_front()  # Focus results page before clicking next
        if not click_next_page_button(
            results_page
        ):  # Stop if no next page button is available
            break  # End pagination loop
        wait_for_network_to_settle(results_page)  # Wait for next page results to load
        if not is_results_page_usable(
            results_page, run_statistics
        ):  # Verify next page isn't blocked or unavailable
            download_logger.warning(
                "[%s] the next result page is not usable, moving on", search_keyword
            )  # Log unusable next page
            break  # End pagination loop

    download_logger.info(
        "[%s] finished with %d PDF link(s)", search_keyword, len(all_keyword_urls)
    )  # Log keyword completion
    del all_keyword_urls  # Free set memory explicitly
    browser_session.close_unneeded_tabs()  # Clean up any extra tabs
    return True  # Keyword searched successfully


# ============================================================== SAFETY DATA SHEETS (US ENGLISH)  # Job 2 SDS scraping, segmentation, and pagination
#
# The page https://www.agilent.com/en/search/safety-data-sheets shows 10 cards per page. The cards and their PDF links  # SDS page layout note
# are NOT in the page source: JavaScript builds them after the page opens, so they are read from the rendered DOM  # Why live DOM evaluation is required
# (what Inspect Element shows) with page.evaluate(), never from view-source or the raw HTML response. Every card has  # DOM inspection details
#   <a class="pub-ext" href="/library/msds/5190-8406_US.pdf">  and tags such as  <span class="meta-text">English - United States</span>  # Example card markup
# Clicking the link opens the PDF at https://www.agilent.com/library/msds/<name>.pdf.  # Resulting PDF URL format
#
# Result pages are opened directly by address:  # Direct URL pagination format header
#   ...?cq=<query>&searchAttempt=modified&c_ds_f-agilent_doc_language_long=English&c_ds_firstResult=10   (page 2)  # Example query string for page 2
#
# The site never shows more than 5,000 results (500 pages) for one query. So the query is cut into "segments":  # Segmentation explanation
#   * The first segment is every US English document.  # Initial segment definition
#   * If a segment has more than 5,000 results, its publication-date range is split in two halves,  # Binary date splitting rule
#     and each half is a new segment, until every segment fits.  # Recursive subdivision until <= 5,000 results
# The state of every segment (total, page reached, finished) is stored in the database, so a stopped run  # Persistence and resume note
# continues exactly where it stopped and finished segments are never requested again.  # Skipping completed segments

US_ENGLISH_LABEL = (
    "English - United States"  # The language tag on the cards that is wanted
)

READ_SDS_CARDS_SCRIPT = (  # Runs inside the page, on the live DOM after JavaScript has rendered it (not on the page source)
    "(scopeSelector) => {"  # Accept container selector argument
    "const scope = document.querySelector(scopeSelector);"  # Find SDS results container
    "if (!scope) return [];"  # Return empty list if container missing
    "return Array.from(scope.querySelectorAll('.compendiumcard')).map(card => {"  # Map each result card element
    "const link = card.querySelector('a.pub-ext') || card.querySelector('a[href*=\\'.pdf\\' i]') || card.querySelector('a[href]');"  # Find best link inside card
    "const labels = Array.from(card.querySelectorAll('.meta-text')).map(tag => tag.innerText.trim());"  # Extract metadata tag texts
    "let locale = null; let dataUri = null;"  # Initialize optional fields from data-result JSON
    "const holder = card.closest('[data-result]');"  # Find enclosing element carrying serialized result JSON
    "if (holder) { try { const result = JSON.parse(holder.getAttribute('data-result')); const raw = result.raw || {};"  # Parse data-result JSON safely
    " locale = raw.locale || null; dataUri = raw.clickableuri || result.printableUri || result.PrintableUri || null; } catch (e) {} }"  # Extract locale and fallback URI
    "let href = link ? link.href : null;"  # link.href is the full address that the browser resolved from the rendered link
    "if (!href && dataUri) { try { href = new URL(dataUri, location.origin).href; } catch (e) {} }"  # Resolve relative dataUri against origin if href absent
    "return {href: href, labels: labels, locale: locale};"  # Return structured card info
    "});"  # End map
    "}"  # End function
)  # End READ_SDS_CARDS_SCRIPT
READ_SDS_TOTAL_SCRIPT = (  # JS snippet to read total result count from pagination or scope attributes
    "(scopeSelector) => {"  # Accept container selector argument
    "const scope = document.querySelector(scopeSelector);"  # Locate SDS container
    "if (!scope) return null;"  # Return null if container missing
    "const pagination = scope.querySelector('agt-pagination');"  # Locate pagination element
    "const rawTotal = (pagination && pagination.getAttribute('data-total-results')) || scope.getAttribute('data-no-of-results');"  # Read raw total string
    "const total = parseInt(rawTotal, 10);"  # Parse base-10 integer
    "return Number.isNaN(total) ? null : total;"  # Return integer or null if invalid
    "}"  # End function
)  # End READ_SDS_TOTAL_SCRIPT
READ_SDS_NO_RESULTS_SCRIPT = (  # JS snippet to detect empty search result text
    "(scopeSelector) => {"  # Accept container selector argument
    "const scope = document.querySelector(scopeSelector);"  # Locate SDS container
    "if (!scope) return false;"  # Return false if container missing
    "return /no results|showing 0 results/i.test(scope.innerText || '');"  # Test inner text for zero-result phrases
    "}"  # End function
)  # End READ_SDS_NO_RESULTS_SCRIPT


def normalize_label(
    label_text: str,
) -> str:  # Lowercase, and turn every dash/space/odd character into one space
    return re.sub(
        r"[^a-z]+", " ", label_text.lower()
    ).strip()  # Replace non-letter runs with single space and strip ends


def card_is_us_english(
    card_description: dict,
) -> bool:  # True when the card says English + United States
    texts_to_check = list(card_description.get("labels", []))  # Copy card label strings
    if card_description.get(
        "locale"
    ):  # For example "English_UNITED STATES", read from the rendered card
        texts_to_check.append(
            card_description["locale"]
        )  # Include locale string in checked texts
    for label_text in texts_to_check:  # Check each text candidate
        normalized_label = normalize_label(label_text)  # Normalize label text
        if (
            "english" in normalized_label and "united states" in normalized_label
        ):  # Match both "english" and "united states"
            return True  # Confirmed US English card
    return False  # Not US English


def read_sds_cards(
    browser_page: Page,
) -> list:  # Every card of the current result page that has a link
    card_descriptions = browser_page.evaluate(
        READ_SDS_CARDS_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
    )  # Evaluate card extraction script in DOM
    return [
        card_description
        for card_description in card_descriptions
        if card_description.get("href")
    ]  # Keep only cards with a non-empty href


def get_us_english_urls_from_cards(
    card_descriptions: list,
) -> set:  # Keep only the cards tagged "English - United States"
    if (
        not SDS_DOWNLOAD_ONLY_US_ENGLISH
    ):  # Every PDF link found in the rendered DOM of the page
        return {  # Return all PDF hrefs across cards
            card_description["href"]  # Card URL
            for card_description in card_descriptions  # Iterate cards
            if ".pdf"
            in urlparse(
                card_description["href"]
            ).path.lower()  # Check path contains ".pdf"
        }  # End set comprehension
    us_english_urls = {  # Filter to US English cards with PDF URLs
        card_description["href"]  # Card URL
        for card_description in card_descriptions  # Iterate cards
        if card_is_us_english(card_description)
        and ".pdf"
        in urlparse(
            card_description["href"]
        ).path.lower()  # Require US English and ".pdf" path
    }  # End set comprehension
    if (
        card_descriptions and not us_english_urls
    ):  # Warn if cards existed on page but none matched US English
        download_logger.warning(  # Log warning with sample labels
            "[SDS page] %d card(s) but none tagged '%s'. First tags seen: %s",  # Format string
            len(card_descriptions),  # Total cards read
            US_ENGLISH_LABEL,  # Expected tag label
            [
                card_description["labels"] for card_description in card_descriptions[:3]
            ],  # First 3 cards' labels
        )  # End warning log
    return us_english_urls  # Return set of US English PDF URLs


def wait_for_sds_cards_to_appear(
    browser_page: Page, timeout_seconds: float
) -> bool:  # Wait until JS-rendered SDS cards stabilize in DOM
    """Wait until JavaScript has rendered the result cards and their PDF links in the live DOM.

    The PDF links are not in the page source: the site builds the cards with JavaScript after the page opens.
    So this waits for the cards to exist in the rendered DOM, waits until the number of cards stops changing,
    and scrolls the page so that anything loaded lazily is rendered too. Only then are the cards read.
    """  # Function docstring
    wait_deadline = time.time() + timeout_seconds  # Overall wait deadline
    try:  # Step 1: wait for the first rendered card to appear in the DOM
        browser_page.wait_for_selector(  # Wait for card element inside results scope
            f"{SDS_RESULTS_SCOPE_SELECTOR} .compendiumcard",
            state="attached",
            timeout=int(timeout_seconds * 1000),  # Selector, state, and ms timeout
        )  # End wait_for_selector
    except PlaywrightTimeoutError:  # Fall through to polling loop if selector times out
        pass  # The polling loop below makes the final decision
    except PlaywrightError as browser_error:  # Propagate if browser closed
        if is_browser_closed_error(browser_error):  # Check closed browser error
            raise  # Re-raise
    previous_card_count = -1  # Track previous count of cards with links
    stable_rounds = 0  # Consecutive checks with unchanged card count
    scrolled_once = False  # Track whether lazy-load scroll pass has run
    while (
        time.time() < wait_deadline
    ):  # Step 2: wait until the rendered cards have links and stop changing
        try:  # Read cards and check stability
            rendered_cards = read_sds_cards(
                browser_page
            )  # Get cards that already have hrefs
            if rendered_cards:  # Once at least one card with a link exists
                if (
                    not scrolled_once
                ):  # Step 3: scroll once so lazily rendered content appears
                    scroll_to_bottom_of_page(
                        browser_page, pause_seconds=0.4
                    )  # Scroll down to trigger lazy elements
                    browser_page.evaluate(
                        "window.scrollTo(0, 0)"
                    )  # Scroll back to top of page
                    scrolled_once = True  # Mark scroll pass completed
                    previous_card_count = -1  # Reset stability counter after scrolling
                    stable_rounds = 0  # Reset stable round count
                    continue  # Re-check rendered cards after scroll
                if (
                    len(rendered_cards) == previous_card_count
                ):  # Compare card count to previous poll
                    stable_rounds += 1  # Increment unchanged poll count
                    if (
                        stable_rounds >= 2
                    ):  # The number of rendered cards did not change for two checks
                        download_logger.debug(
                            "[SDS page] rendered DOM ready: %d card(s) with a PDF link",
                            len(rendered_cards),
                        )  # Log ready state
                        return True  # DOM is ready
                else:  # Card count changed since last check
                    previous_card_count = len(rendered_cards)  # Update previous count
                    stable_rounds = 0  # Reset stability counter
        except PlaywrightError as browser_error:  # Handle Playwright errors during polling
            if is_browser_closed_error(browser_error):  # Re-raise if browser closed
                raise  # Propagate
        time.sleep(0.7)  # Wait 700ms between polls
    return bool(
        scrolled_once
    )  # Cards were seen at some point, even if the count kept changing


def wait_for_sds_total_results(
    browser_page: Page, timeout_seconds: float
) -> Optional[int]:  # Poll DOM for total SDS result count
    wait_deadline = time.time() + timeout_seconds  # Compute timeout deadline
    while time.time() < wait_deadline:  # Poll until deadline
        try:  # Evaluate total count script
            total_results = browser_page.evaluate(
                READ_SDS_TOTAL_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
            )  # Read total from DOM
            if total_results is not None:  # Return integer once available
                return int(total_results)  # Cast to int
        except PlaywrightError as browser_error:  # Handle Playwright errors
            if is_browser_closed_error(browser_error):  # Propagate if browser closed
                raise  # Re-raise
        time.sleep(0.5)  # Wait 500ms between polls
    try:  # The total never appeared; look at what the page shows instead
        if read_sds_cards(
            browser_page
        ):  # If cards are visible without a readable total
            download_logger.warning(
                "[SDS page] the total is not readable, assuming the maximum reachable number of results"
            )  # Log fallback assumption
            return SDS_MAXIMUM_REACHABLE_RESULTS  # Assume max reachable count
        if browser_page.evaluate(
            READ_SDS_NO_RESULTS_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
        ):  # Check if page says no results
            return 0  # Zero results confirmed
    except PlaywrightError as browser_error:  # Handle Playwright errors on fallback check
        if is_browser_closed_error(browser_error):  # Propagate if browser closed
            raise  # Re-raise
    return None  # Total could not be determined


def format_date_clause(
    range_start: Optional[date], range_end: Optional[date]
) -> list:  # Build Coveo date filter clauses for range
    if (
        range_start is None or range_end is None
    ):  # Return empty list if no date bounds given
        return []  # No date filter clauses
    return [  # Lower and upper inclusive date clauses
        f"@{SDS_DATE_FIELD}>={range_start.strftime('%Y/%m/%d')}",  # Start date filter (YYYY/MM/DD)
        f"@{SDS_DATE_FIELD}<={range_end.strftime('%Y/%m/%d')}",  # End date filter (YYYY/MM/%d)
    ]  # End list


def build_sds_page_url(  # Construct full SDS search URL for locale, date range, and starting result offset
    use_locale_clause: bool,
    range_start: Optional[date],
    range_end: Optional[date],
    first_result: int,  # Query options and result offset
) -> str:  # The address of one result page, in the same style as the addresses of the site
    query_parts = [SDS_OBJECT_TYPE_CLAUSE]  # Start query with MSDS objecttype filter
    if use_locale_clause:  # Include US English locale filter if enabled
        query_parts.append(SDS_LOCALE_CLAUSE)  # Append locale clause
    query_parts.extend(
        format_date_clause(range_start, range_end)
    )  # Append date range clauses if present
    address_parameters = [  # Query string parameters
        ("cq", " AND ".join(query_parts)),  # Combined search query expression
        ("searchAttempt", "modified"),  # Search attempt flag used by site
        (
            SDS_LANGUAGE_FACET_PARAMETER,
            SDS_LANGUAGE_FACET_VALUE,
        ),  # Language facet parameter
    ]  # End base parameters
    if first_result > 0:  # Add offset parameter for pages after page 1
        address_parameters.append(
            (SDS_FIRST_RESULT_PARAMETER, str(first_result))
        )  # Set starting result index
    return (
        SDS_BASE_URL + "?" + urlencode(address_parameters)
    )  # Encode and join base URL with query string


def build_sds_segment_key(
    use_locale_clause: bool, range_start: Optional[date], range_end: Optional[date]
) -> str:  # Unique DB key for an SDS segment
    mode_name = "us" if use_locale_clause else "en"  # Encode query mode
    start_text = (
        range_start.isoformat() if range_start else "all"
    )  # Encode start date or "all"
    end_text = range_end.isoformat() if range_end else "all"  # Encode end date or "all"
    return f"{SDS_SEGMENT_KEY_VERSION}|{mode_name}|{start_text}|{end_text}"  # Combine version, mode, and date bounds


def split_date_range(
    range_start: Optional[date], range_end: Optional[date]
) -> Optional[tuple]:  # Split a date range into two halves
    if (
        range_start is None or range_end is None
    ):  # Default to full configured date range when None
        range_start, range_end = (
            SDS_EARLIEST_DATE,
            SDS_LATEST_DATE,
        )  # Use earliest and latest configured dates
    if range_start >= range_end:  # A single day cannot be cut
        return None  # Cannot split further
    middle_date = range_start + (range_end - range_start) // 2  # Compute midpoint date
    return (range_start, middle_date), (
        middle_date + timedelta(days=1),
        range_end,
    )  # Return first and second half ranges


def probe_sds_segment_total(  # Determine total results for a segment, using saved DB state or loading page 1
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    run_statistics: RunStatistics,  # Run statistics instance
    use_locale_clause: bool,  # Whether locale filter is used
    range_start: Optional[date],  # Segment start date
    range_end: Optional[date],  # Segment end date
) -> Optional[
    int
]:  # How many results a segment has; None when the page could not be read
    segment_key = build_sds_segment_key(
        use_locale_clause, range_start, range_end
    )  # Build segment key
    saved_state = download_database.load_sds_segment(
        segment_key
    )  # Check DB for previously probed total
    if saved_state is not None:  # Reuse saved total if present
        return saved_state[0]  # Return cached total_results
    results_page = browser_session.active_results_page  # Active results tab
    browser_session.close_unneeded_tabs()  # Close extra tabs
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet connectivity
    page_url = build_sds_page_url(
        use_locale_clause, range_start, range_end, 0
    )  # Build URL for first result of segment
    page_status = open_page_politely(
        results_page, page_url, run_statistics
    )  # Load segment URL
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):  # Detect outage on failure
        raise InternetOutageError(
            "The internet went down while counting the results"
        )  # Raise outage error
    if page_status != PAGE_STATUS_OPENED:  # Abort if page did not open cleanly
        download_logger.warning(
            "[SDS page] could not count the results of segment %s (status: %s)",
            segment_key,
            page_status,
        )  # Log failure
        return None  # Return None
    dismiss_cookie_banner(results_page)  # Dismiss cookie banner if shown
    total_results = wait_for_sds_total_results(
        results_page, SDS_WAIT_FOR_RESULTS_SECONDS
    )  # Read total result count from DOM
    if total_results is None:  # Warn if total could not be read
        download_logger.warning(
            "[SDS page] the result count of segment %s could not be read", segment_key
        )  # Log unreadable total
        return None  # Return None
    download_database.save_sds_segment(
        segment_key, total_results, 0, False
    )  # Persist initial segment state in DB
    download_logger.info(
        "[SDS page] segment %s has %d result(s)", segment_key, total_results
    )  # Log segment result count
    pause_while_acting_like_a_human(
        results_page,
        *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
        "after counting a segment",
    )  # Pause with human activity
    return total_results  # Return total count


def choose_sds_query_mode(
    browser_session: "BrowserSession", run_statistics: RunStatistics
) -> Optional[bool]:  # Select US locale query or plain English filter
    """Returns True (US English query works), False (use the plain English filter) or None (nothing works)."""  # Function docstring
    results_page = browser_session.active_results_page  # Active results tab
    modes_to_try = (
        [True, False] if USE_LOCALE_CLAUSE else [False]
    )  # Order of query modes to test
    for use_locale_clause in modes_to_try:  # Test each mode
        browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before opening page
        page_url = build_sds_page_url(
            use_locale_clause, None, None, 0
        )  # Build test URL
        page_status = open_page_politely(
            results_page, page_url, run_statistics
        )  # Open test URL
        if (
            page_status == PAGE_STATUS_FAILED
            and browser_session.internet_monitor.confirm_internet_is_down()
        ):  # Check for internet outage
            raise InternetOutageError(
                "The internet went down while opening the SDS page"
            )  # Raise outage error
        if page_status != PAGE_STATUS_OPENED:  # Try next mode if page failed to open
            download_logger.warning(
                "[SDS page] could not open %s (status: %s)", page_url, page_status
            )  # Log failure
            continue  # Next mode
        dismiss_cookie_banner(results_page)  # Dismiss cookie popup if visible
        if not wait_for_sds_cards_to_appear(
            results_page, SDS_WAIT_FOR_RESULTS_SECONDS
        ):  # Try next mode if no cards render
            download_logger.warning(
                "[SDS page] no result cards at %s, trying the next query style",
                page_url,
            )  # Log missing cards
            continue  # Next mode
        card_descriptions = read_sds_cards(results_page)  # Read rendered cards
        if use_locale_clause and not all(
            card_is_us_english(card_description)
            for card_description in card_descriptions
        ):  # Verify locale clause actually filtered to US English
            download_logger.warning(  # Log that site ignored US English clause
                "[SDS page] the US English query is not honoured by the site, using the plain English filter instead"  # Warning message
            )  # End warning log
            continue  # Fall back to plain English mode
        download_logger.info(  # Log chosen query mode
            "[SDS page] query style chosen: %s",  # Format string
            "US English query"
            if use_locale_clause
            else "plain English filter (cards are filtered by their tag)",  # Mode description
        )  # End info log
        return use_locale_clause  # Return selected boolean mode
    return None  # Return None if no mode succeeded


def download_us_english_urls(  # Download pending SDS PDF URLs from one result page using a temporary tab
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    us_english_urls: set,  # Set of PDF URLs from current SDS page
    run_statistics: RunStatistics,  # Run statistics instance
) -> bool:  # Download every address that has no file yet; returns True when a request was made
    pending_urls = sorted(  # Sort URLs that don't already have a saved file
        us_english_url  # Candidate URL
        for us_english_url in us_english_urls  # Iterate page URLs
        if not download_database.is_file_for_url_already_downloaded(
            us_english_url
        )  # Skip if file already exists
    )  # The download folder is checked first, so existing files are never requested again
    skipped_count = len(us_english_urls) - len(
        pending_urls
    )  # Count already-saved URLs on this page
    run_statistics.skipped_count += skipped_count  # Add skipped URLs to run statistics
    if not pending_urls:  # Exit early if all URLs on page are already downloaded
        download_logger.info(
            "[SDS page] nothing new to download (%d already saved)", skipped_count
        )  # Log nothing new
        return False  # No network download performed
    if skipped_count:  # Log how many files on this page were already present
        download_logger.info(
            "[SDS page] %d of %d file(s) on this page already exist and are skipped",
            skipped_count,
            len(us_english_urls),
        )  # Log skip count
    download_database.save_newly_found_urls(
        set(pending_urls)
    )  # Record pending URLs in collected_urls
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Verify internet before opening download tab
    with browser_session.temporary_download_tab() as download_page:  # Open temporary tab for downloads
        for url_position, pending_url in enumerate(
            pending_urls
        ):  # Download each pending URL
            browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before each download
            if url_position > 0:  # Pause between consecutive downloads
                browser_session.active_results_page.bring_to_front()  # Bring results page to front during pause
                pause_while_acting_like_a_human(  # Pause with human-like actions
                    browser_session.active_results_page,
                    *PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,
                    "between downloads",  # Range and reason
                )  # End pause
            download_logger.info(
                "  (%d/%d) US English: %s",
                url_position + 1,
                len(pending_urls),
                pending_url,
            )  # Log download progress
            download_single_url(
                download_page,
                download_database,
                pending_url,
                run_statistics,
                browser_session.internet_monitor,
            )  # Download single PDF
    log_run_statistics(run_statistics, "Totals so far")  # Log updated totals
    return True  # Indicate download requests were made


# ---- Page navigation by clicking the page numbers (1, 2, 3 ... 500) in the rendered pagination bar  # Sequential SDS pagination section

SDS_PAGINATION_SELECTOR = f"{SDS_RESULTS_SCOPE_SELECTOR} agt-pagination"  # The pagination bar of the safety data sheet tab
READ_SDS_PAGINATION_SCRIPT = r"""(scopeSelector) => {  # JS snippet reading current page number from pagination UI
  const pagination = document.querySelector(scopeSelector + ' agt-pagination');  # Locate agt-pagination element
  if (!pagination) return null;  # Return null if pagination bar is absent
  const activeButton = pagination.querySelector('.agt-pagination__item--active button[data-page]');  # Find active page button
  const info = pagination.querySelector('.agt-pagination__info');  # Find range text element (e.g. "11 - 20 of ...")
  let fromInfo = null;  # Page number derived from displayed result range
  if (info) {  # Parse range text if available
    const match = (info.innerText || '').match(/^\s*([\d,]+)\s*-/);  # Match first result number in range
    if (match) fromInfo = Math.floor((parseInt(match[1].replace(/,/g, ''), 10) - 1) / 10) + 1;  # Convert 1-based start index to 1-based page number
  }  # End info parsing
  const fromActive = activeButton ? parseInt(activeButton.getAttribute('data-page'), 10) : null;  # Page number from active button's data-page
  const fromAttribute = parseInt(pagination.getAttribute('data-current-page'), 10);  # Page number from data-current-page attribute
  return {info: fromInfo, active: fromActive, attribute: Number.isNaN(fromAttribute) ? null : fromAttribute};  # Return all three page indicators
}"""  # Runs in the live DOM: tells which page number the pagination bar shows right now


def read_current_sds_page_number(
    browser_page: Page,
) -> Optional[int]:  # Read active SDS page number from DOM
    """The page number the rendered pagination bar shows now (from the '11 - 20 of ...' text, the active button, or its attribute)."""  # Function docstring
    try:  # Evaluate pagination script in browser
        pagination_state = browser_page.evaluate(
            READ_SDS_PAGINATION_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
        )  # Get pagination state dict
    except PlaywrightError as browser_error:  # Handle Playwright errors
        if is_browser_closed_error(browser_error):  # Re-raise if browser closed
            raise  # Propagate
        return None  # Return None on non-fatal error
    if not pagination_state:  # Return None if pagination bar not found
        return None  # No pagination state
    for state_key in ("info", "active", "attribute"):  # Check sources in priority order
        if pagination_state.get(state_key):  # Use first truthy/non-zero page number
            return int(pagination_state[state_key])  # Return as int
    return None  # Return None if none available


def click_first_visible_match(
    browser_page: Page, matching_locator
) -> bool:  # Click the first visible element of a locator like a person
    try:  # Count matching elements
        match_count = matching_locator.count()  # Total locator matches
    except PlaywrightError as browser_error:  # Handle Playwright errors
        if is_browser_closed_error(browser_error):  # Re-raise if browser closed
            raise  # Propagate
        return False  # Return False on error
    for match_position in range(match_count):  # Iterate through matches
        candidate_element = matching_locator.nth(
            match_position
        )  # Element at match_position
        try:  # Check visibility and click like a human
            if not candidate_element.is_visible():  # Skip invisible elements
                continue  # Next match
            click_element_like_a_human(
                browser_page, candidate_element
            )  # Click visible element
            return True  # Click succeeded
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as browser_error:  # Handle click errors
            if is_browser_closed_error(browser_error):  # Re-raise if browser closed
                raise  # Propagate
            continue  # Try next match
    return False  # No visible element could be clicked


def click_sds_page_number_button(
    browser_page: Page, target_page: int
) -> bool:  # Click only the exact numbered button for "target_page"
    exact_number_locator = browser_page.locator(  # Locate candidate page buttons matching data-page
        f"{SDS_PAGINATION_SELECTOR} button.agt-pagination__btn[data-page='{target_page}']"  # Selector scoped to the target page number
    ).filter(
        has_text=re.compile(rf"^\s*{target_page}\s*$")
    )  # Ensure the button text is strictly the target page number and not a multi-page control
    return click_first_visible_match(
        browser_page, exact_number_locator
    )  # Click the first visible matching page button


def click_sds_next_button(
    browser_page: Page,
) -> bool:  # Click the single-step ">" next-page button without skipping multiple pages
    single_next_locator = browser_page.locator(  # Locate enabled next-page controls
        f"{SDS_PAGINATION_SELECTOR} button[data-action='next']:not([disabled]), "  # Match pagination buttons with action="next"
        f"{SDS_PAGINATION_SELECTOR} button[aria-label*='next page' i]:not([disabled])"  # Also match accessible "next page" buttons
    ).filter(
        has_not_text=re.compile(r">>|»")
    )  # Exclude double-arrow controls that skip ahead by multiple pages
    return click_first_visible_match(
        browser_page, single_next_locator
    )  # Click the first visible single-step next button


def click_sds_page_through_ellipsis_menu(
    browser_page: Page, target_page: int
) -> bool:  # Open '...' menu and click target_page item
    """Page numbers that the bar hides behind '...' are reached by opening the '...' menu and clicking the number in it."""  # Function docstring
    ellipsis_buttons = browser_page.locator(
        f"{SDS_PAGINATION_SELECTOR} button.agt-pagination__ellipsis-btn"
    )  # Locate ellipsis buttons
    try:  # Count ellipsis buttons
        ellipsis_count = ellipsis_buttons.count()  # Number of ellipsis buttons
    except PlaywrightError as browser_error:  # Handle Playwright errors
        if is_browser_closed_error(browser_error):  # Re-raise if browser closed
            raise  # Propagate
        return False  # Return False on error
    for ellipsis_position in range(ellipsis_count):  # Try each ellipsis button
        ellipsis_button = ellipsis_buttons.nth(
            ellipsis_position
        )  # Ellipsis button at index
        try:  # Open menu and look for target_page
            if not ellipsis_button.is_visible():  # Skip hidden ellipsis buttons
                continue  # Next ellipsis button
            click_element_like_a_human(
                browser_page, ellipsis_button
            )  # Click ellipsis button to open menu
            time.sleep(0.6)  # Wait briefly for menu to render
            menu_item_locator = browser_page.locator(
                f"button.agt-pagination__menu-item[data-page='{target_page}']"
            )  # Locate target page item in menu
            if click_first_visible_match(
                browser_page, menu_item_locator
            ):  # Click target page item if visible
                return True  # Navigation item clicked
            browser_page.keyboard.press(
                "Escape"
            )  # This menu does not hold the number; close it and try the next '...'
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as browser_error:  # Handle menu interaction errors
            if is_browser_closed_error(browser_error):  # Re-raise if browser closed
                raise  # Propagate
            continue  # Try next ellipsis button
    return False  # Target page not found in any ellipsis menu


def wait_until_sds_page_is(  # Wait until pagination bar and rendered card links reflect target_page
    browser_page: Page,
    target_page: int,
    previous_page_links: Optional[tuple],
    timeout_seconds: float,  # Page, target, prior links, and timeout
) -> bool:  # Return True once target page updates and cards finish rendering
    """After a click: wait until the bar shows the target page AND the rendered cards are different from the previous page."""  # Function docstring
    wait_deadline = time.time() + timeout_seconds  # Compute timeout deadline
    while time.time() < wait_deadline:  # Poll until deadline
        try:  # Read current page number and card links
            current_page = read_current_sds_page_number(
                browser_page
            )  # Current page number from pagination bar
            current_links = tuple(
                card_description["href"]
                for card_description in read_sds_cards(browser_page)
            )  # Tuple of card hrefs on page
        except PlaywrightError as browser_error:  # Handle transient Playwright errors during update
            if is_browser_closed_error(browser_error):  # Re-raise if browser closed
                raise  # Propagate
            time.sleep(0.5)  # Brief sleep before retrying poll
            continue  # Next poll
        page_number_matches = (
            current_page == target_page if current_page is not None else True
        )  # Check page indicator matches target_page
        if (
            page_number_matches
            and current_links
            and current_links != previous_page_links
        ):  # Ensure cards loaded and changed from previous page
            return wait_for_sds_cards_to_appear(
                browser_page, SDS_WAIT_FOR_RESULTS_SECONDS
            )  # Let the new cards finish rendering
        time.sleep(0.5)  # Wait 500ms before next poll
    return False  # Timed out waiting for target page to render


def go_to_sds_page_number(  # Navigate to target_page sequentially without skipping pages
    browser_session: "BrowserSession",
    target_page: int,
    previous_page_links: Optional[
        tuple
    ],  # Session, destination page, and prior card links
) -> bool:  # Return True once target_page finishes rendering
    """Go to one specific page by clicking its number: the number itself, else '>' for the next page, else the '...' menu."""  # Docstring describing fallback order
    results_page = (
        browser_session.active_results_page
    )  # Active tab displaying Safety Data Sheets results
    for attempt_number in range(
        1, 4
    ):  # Retry up to 3 times if navigation does not settle
        browser_session.internet_monitor.raise_error_if_internet_is_down()  # Abort immediately if internet connectivity is lost
        current_page = read_current_sds_page_number(
            results_page
        )  # Read the page number currently shown in the pagination bar
        if current_page == target_page:  # Already on the requested page
            return True  # No click needed
        if click_sds_page_number_button(
            results_page, target_page
        ):  # Try clicking the exact page number button first
            click_method = f"page number {target_page}"  # Record that an exact page number button was clicked
        elif (
            current_page is None or target_page == current_page + 1
        ) and click_sds_next_button(
            results_page
        ):  # Step forward by 1 page when moving from current_page to current_page + 1
            click_method = (
                "next button (>)"  # Record that the single-step next button was clicked
            )
        elif click_sds_page_through_ellipsis_menu(
            results_page, target_page
        ):  # Fall back to opening '...' when target_page is hidden
            click_method = "'...' menu"  # Record that the ellipsis menu was used
        else:  # No usable pagination control was found on this attempt
            download_logger.warning(
                "[SDS page] could not find a way to click page %d (attempt %d/3)",
                target_page,
                attempt_number,
            )  # Log the missing control
            time.sleep(2)  # Wait briefly before retrying
            continue  # Try the next attempt
        download_logger.info(
            "[SDS page] clicked page %d (%s), waiting for it to render",
            target_page,
            click_method,
        )  # Log the click action taken
        if wait_until_sds_page_is(
            results_page, target_page, previous_page_links, 20
        ):  # Wait until both page number and rendered cards update
            return True  # Target page rendered successfully
        download_logger.warning(
            "[SDS page] page %d did not show after the click (attempt %d/3)",
            target_page,
            attempt_number,
        )  # Log that the page did not change in time
    return False  # Return False after exhausting all retry attempts


def open_sds_result_page_by_address(  # Open an SDS result page directly by constructed URL and return card links
    browser_session: "BrowserSession",  # Active browser session
    run_statistics: RunStatistics,  # Run statistics instance
    use_locale_clause: bool,  # Locale query flag
    range_start: Optional[date],  # Date range start
    range_end: Optional[date],  # Date range end
    first_result: int,  # Starting result offset
) -> Optional[tuple]:  # Return tuple of card hrefs or None if failed
    """Open a result page by its address in the real browser; returns the links of its rendered cards, or None."""  # Function docstring
    results_page = browser_session.active_results_page  # Active results tab
    page_url = build_sds_page_url(
        use_locale_clause, range_start, range_end, first_result
    )  # Build target URL
    page_status = open_page_politely(
        results_page, page_url, run_statistics
    )  # Navigate to URL with block retry logic
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):  # Detect outage on failure
        raise InternetOutageError(
            "The internet went down while opening a result page"
        )  # Raise outage error
    if page_status != PAGE_STATUS_OPENED:  # Return None if page not opened cleanly
        download_logger.warning(
            "[SDS page] could not open the page at result %d (status: %s)",
            first_result,
            page_status,
        )  # Log status
        return None  # Failed to open
    dismiss_cookie_banner(results_page)  # Dismiss cookie banner if visible
    if not wait_for_sds_cards_to_appear(
        results_page, SDS_WAIT_FOR_RESULTS_SECONDS
    ):  # Wait for cards to render
        download_logger.warning(
            "[SDS page] no rendered cards at result %d", first_result
        )  # Log missing cards
        return None  # No cards rendered
    return tuple(
        card_description["href"] for card_description in read_sds_cards(results_page)
    )  # Return tuple of rendered card URLs


def walk_sds_segment(  # Step through all pages of an SDS segment one page at a time, collecting and downloading PDFs
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    run_statistics: RunStatistics,  # Run statistics instance
    use_locale_clause: bool,  # Locale query flag
    range_start: Optional[date],  # Segment start date
    range_end: Optional[date],  # Segment end date
    total_results: int,  # Total results reported for segment
    first_result: int,  # Saved starting result offset to resume from
) -> bool:  # Return True when segment finishes
    """Open the page once, then go through its pages ONE AT A TIME by clicking the page numbers (page 1, 2, 3 ... up to 500).

    On every page the rendered DOM is read (what Inspect Element shows), every PDF link found there is collected,
    and the PDFs are downloaded before the next page number is clicked. Returns True when the segment is finished.
    """  # Function docstring
    segment_key = build_sds_segment_key(
        use_locale_clause, range_start, range_end
    )  # Compute segment key
    results_page = browser_session.active_results_page  # Active results tab
    reachable_results = min(
        total_results, SDS_MAXIMUM_REACHABLE_RESULTS
    )  # Cap reachable results at 5,000
    total_pages = (
        reachable_results + SDS_RESULTS_PER_PAGE - 1
    ) // SDS_RESULTS_PER_PAGE  # Compute total pages (10 per page)
    page_number = (
        first_result // SDS_RESULTS_PER_PAGE + 1
    )  # A stopped run continues at the page where it stopped

    browser_session.close_unneeded_tabs()  # Close any extra tabs
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before loading page 1
    current_page_links = open_sds_result_page_by_address(  # Load page 1 by URL
        browser_session,
        run_statistics,
        use_locale_clause,
        range_start,
        range_end,
        0,  # Offset 0 for page 1
    )  # Page 1 is opened once by its address; every other page is reached by clicking
    if current_page_links is None:  # Return False if page 1 could not be opened
        download_logger.warning(
            "[SDS page] segment %s: could not open the first page; it will be continued next run",
            segment_key,
        )  # Log warning
        return False  # Segment incomplete
    if page_number > 1:  # Continue where the last run stopped
        download_logger.info(
            "[SDS page] segment %s: continuing at page %d", segment_key, page_number
        )  # Log resume page
        if not go_to_sds_page_number(
            browser_session, page_number, current_page_links
        ):  # Navigate to saved page number
            current_page_links = open_sds_result_page_by_address(  # Fallback: open target offset directly by URL if click navigation failed
                browser_session,  # Session
                run_statistics,  # Statistics
                use_locale_clause,  # Locale flag
                range_start,  # Start date
                range_end,  # End date
                (page_number - 1)
                * SDS_RESULTS_PER_PAGE,  # Result offset for page_number
            )  # End fallback open_sds_result_page_by_address
            if current_page_links is None:  # Return False if fallback open also failed
                return False  # Segment incomplete

    while page_number <= total_pages:  # Process pages sequentially up to total_pages
        browser_session.close_unneeded_tabs()  # Close stray tabs
        browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before processing page
        results_page.bring_to_front()  # Focus results tab

        card_descriptions = read_sds_cards(
            results_page
        )  # The fully rendered DOM of this page
        current_page_links = tuple(
            card_description["href"] for card_description in card_descriptions
        )  # Snapshot card URLs on this page
        pdf_urls = get_us_english_urls_from_cards(
            card_descriptions
        )  # Filter to US English PDF URLs
        download_logger.info(  # Log current page status and PDF count
            "[SDS page] segment %s: page %d of %d: %d card(s), %d PDF(s) to download",  # Format string
            segment_key,  # Segment key
            page_number,  # Current page number
            total_pages,  # Total pages in segment
            len(card_descriptions),  # Card count on page
            len(pdf_urls),  # PDF count on page
        )  # End info log
        if pdf_urls:  # Download PDFs found on this page if any
            download_us_english_urls(
                browser_session, download_database, pdf_urls, run_statistics
            )  # Download pending PDFs

        page_number += 1  # Advance target page counter
        download_database.save_sds_segment(  # Persist next page offset to database
            segment_key,
            total_results,
            (page_number - 1) * SDS_RESULTS_PER_PAGE,
            False,  # Mark unfinished while walking
        )  # Save the position, so a stopped run continues at the next page
        if page_number > total_pages:  # Stop loop after last page
            break  # Exit while loop
        pause_while_acting_like_a_human(  # Pause with human-like activity before advancing page
            results_page,
            *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
            "before clicking the next page number",  # Range and reason
        )  # End pause
        results_page.bring_to_front()  # Bring results tab to front before clicking page control
        if go_to_sds_page_number(
            browser_session, page_number, current_page_links
        ):  # Click to next page
            continue  # Proceed to next iteration on success
        download_logger.warning(
            "[SDS page] clicking page %d failed, opening it by its address instead",
            page_number,
        )  # Log click fallback
        new_page_links = (
            open_sds_result_page_by_address(  # Open next page by direct URL as fallback
                browser_session,  # Session
                run_statistics,  # Statistics
                use_locale_clause,  # Locale flag
                range_start,  # Start date
                range_end,  # End date
                (page_number - 1) * SDS_RESULTS_PER_PAGE,  # Result offset for next page
            )
        )  # End open_sds_result_page_by_address
        if new_page_links is None:  # Return False if direct page load failed
            download_logger.warning(
                "[SDS page] segment %s: page %d could not be opened; it will be continued next run",
                segment_key,
                page_number,
            )  # Log warning
            return False  # Segment incomplete
        if (
            new_page_links == current_page_links
        ):  # Stop if page repeated previous cards (end of actual results)
            download_logger.warning(
                "[SDS page] segment %s: page %d shows the same cards as the page before, stopping this segment",
                segment_key,
                page_number,
            )  # Log duplicate page stop
            break  # Exit while loop

    download_database.save_sds_segment(
        segment_key, total_results, (page_number - 1) * SDS_RESULTS_PER_PAGE, True
    )  # Mark segment finished in DB
    download_logger.info(
        "[SDS page] segment %s finished", segment_key
    )  # Log segment completion
    return True  # Segment finished


def process_sds_segment(  # Recursively split large SDS segments by date or walk pages when within reachable limit
    browser_session: "BrowserSession",  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    run_statistics: RunStatistics,  # Run statistics instance
    use_locale_clause: bool,  # Locale query flag
    range_start: Optional[date],  # Segment start date
    range_end: Optional[date],  # Segment end date
    known_total: Optional[int] = None,  # Optional pre-counted total
) -> bool:  # Split the segment if it is too big, otherwise walk its pages; True when finished
    segment_key = build_sds_segment_key(
        use_locale_clause, range_start, range_end
    )  # Compute segment key
    saved_state = download_database.load_sds_segment(
        segment_key
    )  # Load saved segment state from DB
    if (
        saved_state is not None and saved_state[2]
    ):  # Skip segment if already marked finished
        download_logger.info(
            "[SDS page] segment %s was finished earlier, skipping", segment_key
        )  # Log skip
        return True  # Already finished
    total_results = (  # Determine total result count for this segment
        known_total  # Use provided total if available
        if known_total is not None  # Check if known_total passed
        else probe_sds_segment_total(  # Otherwise probe total from page/DB
            browser_session,
            download_database,
            run_statistics,
            use_locale_clause,
            range_start,
            range_end,  # Pass segment args
        )  # End probe_sds_segment_total
    )  # End total_results assignment
    if total_results is None:  # Return False if total could not be read
        return False  # Incomplete
    if total_results == 0:  # Immediately mark empty segments as finished
        download_database.save_sds_segment(
            segment_key, 0, 0, True
        )  # Save zero-result finished state
        return True  # Finished with 0 results

    if (
        total_results > SDS_MAXIMUM_REACHABLE_RESULTS
    ):  # Too many results to reach with page numbers alone
        halves = split_date_range(
            range_start, range_end
        )  # Split date range into two halves
        if halves is not None:  # Recurse on both halves if date range could be split
            (first_start, first_end), (
                second_start,
                second_end,
            ) = halves  # Unpack first and second date ranges
            first_total = probe_sds_segment_total(  # Probe result count of first half
                browser_session,
                download_database,
                run_statistics,
                use_locale_clause,
                first_start,
                first_end,  # Pass first half
            )  # End first_total probe
            second_total = probe_sds_segment_total(  # Probe result count of second half
                browser_session,
                download_database,
                run_statistics,
                use_locale_clause,
                second_start,
                second_end,  # Pass second half
            )  # End second_total probe
            if (
                first_total is None or second_total is None
            ):  # Return False if either half could not be counted
                return False  # Incomplete
            combined_total = first_total + second_total  # Sum counts of both halves
            if (
                combined_total > total_results or combined_total == 0
            ):  # The site ignores the date part of the query
                download_logger.warning(  # Warn that date filter appears ineffective
                    "[SDS page] segment %s: the date filter does not seem to work (%d + %d vs %d); only the first %d results of it can be reached",  # Format string
                    segment_key,  # Segment key
                    first_total,  # First half total
                    second_total,  # Second half total
                    total_results,  # Parent total
                    SDS_MAXIMUM_REACHABLE_RESULTS,  # Reachable cap
                )  # End warning log
            else:  # Date split looks valid; process both halves
                download_logger.info(  # Log split details
                    "[SDS page] segment %s has %d results, splitting it into %s..%s (%d) and %s..%s (%d)",  # Format string
                    segment_key,  # Segment key
                    total_results,  # Parent total
                    first_start,  # First half start date
                    first_end,  # First half end date
                    first_total,  # First half total
                    second_start,  # Second half start date
                    second_end,  # Second half end date
                    second_total,  # Second half total
                )  # End info log
                first_finished = process_sds_segment(  # Recursively process first half
                    browser_session,
                    download_database,
                    run_statistics,
                    use_locale_clause,
                    first_start,
                    first_end,
                    first_total,  # Pass first half args
                )  # End first_finished
                second_finished = (
                    process_sds_segment(  # Recursively process second half
                        browser_session,
                        download_database,
                        run_statistics,
                        use_locale_clause,
                        second_start,
                        second_end,
                        second_total,  # Pass second half args
                    )
                )  # End second_finished
                if (
                    first_finished and second_finished
                ):  # Mark parent segment finished if both halves finished
                    download_database.save_sds_segment(
                        segment_key, total_results, 0, True
                    )  # Save finished state
                return (
                    first_finished and second_finished
                )  # True only if both halves finished
        else:  # Single-day range cannot be split any further
            download_logger.warning(  # Warn that segment cannot be subdivided further
                "[SDS page] segment %s cannot be cut any more; only the first %d results of it can be reached",  # Format string
                segment_key,  # Segment key
                SDS_MAXIMUM_REACHABLE_RESULTS,  # Reachable cap
            )  # End warning log

    saved_state = download_database.load_sds_segment(
        segment_key
    )  # Reload latest saved offset before walking pages
    first_result = (
        saved_state[1] if saved_state is not None else 0
    )  # Continue where an earlier run stopped
    return walk_sds_segment(  # Walk pages in this segment
        browser_session,  # Session
        download_database,  # Database
        run_statistics,  # Statistics
        use_locale_clause,  # Locale flag
        range_start,  # Start date
        range_end,  # End date
        total_results,  # Total results
        first_result,  # Starting offset
    )  # End walk_sds_segment call


def download_all_us_english_sds(  # Entry point for Job 2: download US English Safety Data Sheets
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,  # Session, DB, and stats
) -> None:  # Safety Data Sheets: download the "English - United States" documents of the whole page
    browser_session.close_unneeded_tabs()  # Ensure clean tab state
    browser_session.internet_monitor.raise_error_if_internet_is_down()  # Check internet before starting SDS job
    download_logger.info(
        "Safety Data Sheets: US English documents from %s", SDS_BASE_URL
    )  # Log SDS job start
    use_locale_clause = choose_sds_query_mode(
        browser_session, run_statistics
    )  # Determine query mode
    if use_locale_clause is None:  # Give up on SDS job if page could not be opened
        download_logger.error(
            "[SDS page] could not open the page with results, giving up on Safety Data Sheets"
        )  # Log error
        return  # Exit SDS job
    pause_while_acting_like_a_human(  # Brief human activity after opening SDS results page
        browser_session.active_results_page,  # Results tab
        *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,  # Activity duration range
        "after opening the SDS page",  # Context label
    )  # End pause
    all_segments_finished = (
        process_sds_segment(  # Process root SDS segment (and any date subdivisions)
            browser_session,
            download_database,
            run_statistics,
            use_locale_clause,
            None,
            None,  # Unbounded date range initially
        )
    )  # End process_sds_segment
    if all_segments_finished:  # Log completion status
        download_logger.info(
            "[SDS page] Safety Data Sheets finished: every segment is complete"
        )  # All segments finished
    else:  # Log partial completion warning
        download_logger.warning(  # Warn that some segments remain incomplete
            "[SDS page] Safety Data Sheets is not complete: some pages could not be read; run the script again to continue"  # Resume instructions
        )  # End warning log
    browser_session.close_unneeded_tabs()  # Clean up extra tabs


# ============================================================== INTERNET MONITOR  # Background connectivity checker and outage signaling


def is_host_responding(
    host_address: str,
) -> bool:  # Check whether one address answers us
    if shutil.which("ping"):  # Use system ping utility if available
        if sys.platform.startswith(
            "win"
        ):  # Windows ping flags (-n count, -w timeout_ms)
            ping_command = [
                "ping",
                "-n",
                "1",
                "-w",
                str(PING_TIMEOUT_SECONDS * 1000),
                host_address,
            ]  # Build Windows ping command
        elif sys.platform.startswith(
            "linux"
        ):  # Linux ping flags (-c count, -W timeout_sec)
            ping_command = [
                "ping",
                "-c",
                "1",
                "-W",
                str(PING_TIMEOUT_SECONDS),
                host_address,
            ]  # Build Linux ping command
        else:  # macOS/BSD ping flags (-c count)
            ping_command = [
                "ping",
                "-c",
                "1",
                host_address,
            ]  # Build macOS/BSD ping command
        try:  # Execute ping subprocess
            ping_result = subprocess.run(  # Run ping command
                ping_command,  # Command list
                stdout=subprocess.DEVNULL,  # Discard stdout
                stderr=subprocess.DEVNULL,  # Discard stderr
                timeout=PING_TIMEOUT_SECONDS
                + 2,  # Subprocess timeout slightly above ping timeout
                check=False,  # Do not raise CalledProcessError on non-zero exit
            )  # End subprocess.run
            if ping_result.returncode == 0:  # Return True if ping succeeded
                return True  # Host responded to ping
        except (
            subprocess.TimeoutExpired,
            OSError,
        ):  # Ignore ping timeout or OS errors and try TCP fallback
            pass  # Fall through to socket check
    try:  # Some networks block ping, so also try a plain connection
        with socket.create_connection(  # Open TCP socket to host and port
            (host_address, FALLBACK_CONNECTION_TEST_PORT),
            timeout=FALLBACK_CONNECTION_TEST_TIMEOUT_SECONDS,  # Target tuple and timeout
        ):  # Connection succeeded if context manager entered
            return True  # Host reachable via TCP
    except OSError:  # Connection refused, timed out, or unreachable
        return False  # Host not responding


async def check_one_host_in_background(
    host_address: str,
) -> bool:  # Run blocking is_host_responding in a worker thread via asyncio
    return await asyncio.to_thread(
        is_host_responding, host_address
    )  # Offload blocking check to thread pool


async def check_all_hosts_at_once() -> bool:  # Check all configured hosts concurrently and return True if any responds
    host_responded_flags = await asyncio.gather(  # Run all host checks concurrently
        *(
            check_one_host_in_background(host_address)
            for host_address in INTERNET_CHECK_HOSTS
        )  # Create coroutine per host
    )  # End asyncio.gather
    return any(host_responded_flags)  # True if at least one host responded


def check_internet_connection() -> bool:  # The internet works when at least one address answers
    # Playwright's sync API may already own an event loop in the main thread, so the async check  # Event-loop safety note part 1
    # always runs in its own short-lived thread, where asyncio.run() is safe.  # Event-loop safety note part 2
    with ThreadPoolExecutor(
        max_workers=1
    ) as single_worker_pool:  # Create single-thread executor
        return single_worker_pool.submit(
            asyncio.run, check_all_hosts_at_once()
        ).result()  # Run async check and wait for result


class InternetMonitor:  # Background thread monitor that tracks internet availability and flags outages
    """Watches the internet in a background thread and flags the browser for a restart after an outage."""  # Class docstring

    def __init__(self):  # Initialize monitor events, locks, and state flags
        self.internet_available_event = (
            threading.Event()
        )  # Set while the internet works
        self.stop_requested_event = threading.Event()  # Set when monitoring should stop
        self.internet_state_lock = (
            threading.Lock()
        )  # Synchronize state transitions and outage flag
        self.outage_callback = (
            None  # Optional callback invoked when internet transitions from up to down
        )
        self.internet_outage_occurred = (
            False  # True when the browser must be restarted because of an outage
        )
        self.monitor_thread = None  # Background monitoring thread reference

    def start_monitoring(
        self, outage_callback
    ) -> None:  # Perform initial connectivity check and start background monitor thread
        self.outage_callback = outage_callback  # Store outage callback
        if check_internet_connection():  # Check initial internet status
            self.internet_available_event.set()  # Mark internet available
            download_logger.info(
                "Internet check: the internet is working"
            )  # Log available status
        else:  # Initial check found no connectivity
            download_logger.warning(
                "Internet check: no answer yet, waiting for the internet"
            )  # Log waiting status
        self.monitor_thread = threading.Thread(
            target=self._keep_checking_internet, name="internet-monitor", daemon=True
        )  # Create daemon monitor thread
        self.monitor_thread.start()  # Start background thread

    def stop_monitoring(
        self,
    ) -> None:  # Signal monitor thread to stop and wait for it to exit
        self.stop_requested_event.set()  # Request stop
        if self.monitor_thread:  # Join thread if started
            self.monitor_thread.join(timeout=15)  # Wait up to 15 seconds

    def _keep_checking_internet(
        self,
    ) -> None:  # Periodic loop running in background thread
        while not self.stop_requested_event.is_set():  # Loop until stop requested
            self.update_internet_status(
                check_internet_connection()
            )  # Check and update status
            self.stop_requested_event.wait(
                INTERNET_CHECK_INTERVAL_SECONDS
            )  # Sleep until next check or stop signal

    def update_internet_status(
        self, internet_is_working: bool
    ) -> None:  # Update availability event and detect up->down transitions
        internet_just_went_down = (
            False  # Flag whether internet just transitioned from available to down
        )
        with self.internet_state_lock:  # Acquire state lock
            internet_was_available = (
                self.internet_available_event.is_set()
            )  # Previous availability state
            if (
                internet_is_working and not internet_was_available
            ):  # Transition from down to up
                self.internet_available_event.set()  # Set available event
                download_logger.warning("INTERNET IS BACK")  # Log recovery
            elif (
                not internet_is_working and internet_was_available
            ):  # Transition from up to down
                self.internet_available_event.clear()  # Clear available event
                self.internet_outage_occurred = (
                    True  # Flag that browser restart is needed due to outage
                )
                internet_just_went_down = True  # Trigger callback after releasing lock
                download_logger.error("INTERNET IS DOWN")  # Log outage
        if (
            internet_just_went_down and self.outage_callback
        ):  # Invoke callback if outage just started
            try:  # Guard callback execution
                self.outage_callback()  # Notify session of outage
            except Exception:  # Log unexpected callback errors without crashing monitor thread
                download_logger.exception(
                    "Could not handle the internet outage"
                )  # Log exception traceback

    def confirm_internet_is_down(
        self,
    ) -> bool:  # Check right now whether the internet is down
        internet_is_working = (
            check_internet_connection()
        )  # Run immediate connectivity check
        self.update_internet_status(internet_is_working)  # Sync event and outage state
        return not internet_is_working  # Return True if down

    def raise_error_if_internet_is_down(
        self,
    ) -> None:  # Cheap check used before every request
        if (
            not self.internet_available_event.is_set()
        ):  # Check event flag without network I/O
            raise InternetOutageError("The internet is down")  # Raise outage exception

    def read_and_clear_outage_flag(
        self,
    ) -> bool:  # Atomically read and reset internet_outage_occurred
        with self.internet_state_lock:  # Acquire state lock
            outage_had_occurred, self.internet_outage_occurred = (
                self.internet_outage_occurred,
                False,
            )  # Read old value and clear flag
            return outage_had_occurred  # Return whether an outage had occurred

    def wait_until_internet_available(
        self,
    ) -> None:  # Block until internet_available_event is set, logging progress
        waited_seconds = 0  # Track total wait time in seconds
        while not self.internet_available_event.wait(
            timeout=INTERNET_CHECK_INTERVAL_SECONDS
        ):  # Wait for event with periodic wakeup
            waited_seconds += (
                INTERNET_CHECK_INTERVAL_SECONDS  # Increment elapsed wait time
            )
            download_logger.info(
                "Still waiting for the internet (%.0f seconds so far)", waited_seconds
            )  # Log wait progress
        if waited_seconds:  # Log recovery duration if we had to wait
            download_logger.info(
                "The internet is back after about %.0f seconds", waited_seconds
            )  # Log recovery time


# ============================================================== BROWSER SESSION  # Browser lifecycle, tab management, and crash/outage restart

ERROR_PHRASES_MEANING_BROWSER_IS_CLOSED = (  # Error substrings indicating browser/context/page closed or disconnected
    "has been closed",  # Target/page/context has been closed
    "target closed",  # CDP/Playwright target closed
    "browser closed",  # Browser process closed
    "connection closed",  # Underlying transport connection closed
    "browser has disconnected",  # Browser disconnected
)  # End closed error phrases tuple


def is_browser_closed_error(
    browser_error: Exception,
) -> bool:  # Return True if exception message indicates browser closed
    error_text = str(browser_error).lower()  # Lowercase error string
    return any(
        closed_phrase in error_text
        for closed_phrase in ERROR_PHRASES_MEANING_BROWSER_IS_CLOSED
    )  # Check for any closed phrase


class BrowserSession:  # Manages InvisiblePlaywright browser instance, context, tabs, and restarts
    """Owns the browser and both tabs; can restart everything if the browser closes or crashes."""  # Class docstring

    def __init__(
        self, internet_monitor: InternetMonitor
    ):  # Initialize session attributes
        self.internet_monitor = internet_monitor  # Internet monitor reference
        self.download_page: Optional[
            Page
        ] = None  # The temporary download tab, only set while it is open
        self.is_download_tab_being_opened = (
            False  # Guard flag while creating temporary download tab
        )
        self.invisible_playwright_launcher: Optional[
            InvisiblePlaywright
        ] = None  # Context manager launcher for anti-detect Firefox
        self.connected_browser: Any = None  # Connected Playwright browser object
        self.browser_context: Any = None  # Browser context holding pages and cookies
        self.results_page: Optional[
            Page
        ] = None  # Main tab used for browsing and search results
        self.browser_restart_count = 0  # Count of non-outage browser restarts

    @property  # Expose active_results_page as a property
    def active_results_page(
        self,
    ) -> Page:  # Return active results tab or raise if browser not running
        if self.results_page is None:  # Ensure results tab exists
            raise RuntimeError(
                "The browser is not running, so there is no results tab"
            )  # Raise runtime error if not started
        return self.results_page  # Return results Page

    def start_browser(
        self,
    ) -> None:  # Launch browser, create context and results tab, and initialize mouse position
        download_logger.info(  # Log browser startup dimensions
            "Starting the browser through invisible_playwright at %dx%d",
            BROWSER_WINDOW_WIDTH_PIXELS,
            BROWSER_WINDOW_HEIGHT_PIXELS,  # Format width and height
        )  # End info log
        self.invisible_playwright_launcher = InvisiblePlaywright(  # Configure InvisiblePlaywright with PDF viewer disabled
            extra_prefs={
                "pdfjs.disabled": True
            }  # Disable built-in PDF viewer so PDFs trigger downloads
        )  # Without this Firefox shows PDFs in its viewer instead of downloading them
        self.connected_browser = (
            self.invisible_playwright_launcher.__enter__()
        )  # Enter launcher context and obtain browser
        self.browser_context = self.connected_browser.new_context(  # Create new browser context with configured viewport/screen size
            viewport={
                "width": BROWSER_WINDOW_WIDTH_PIXELS,
                "height": BROWSER_WINDOW_HEIGHT_PIXELS,
            },  # Viewport dimensions
            screen={
                "width": BROWSER_WINDOW_WIDTH_PIXELS,
                "height": BROWSER_WINDOW_HEIGHT_PIXELS,
            },  # Screen dimensions
        )  # End new_context
        self.browser_context.set_default_timeout(
            PAGE_LOAD_TIMEOUT_MILLISECONDS
        )  # Set default timeout on context
        self.results_page = self.browser_context.new_page()  # Open initial results tab
        attach_browser_event_logging(
            self.results_page
        )  # Attach event listeners to results tab
        self.browser_context.on(
            "page", self._close_unexpected_new_tab
        )  # Auto-close unexpected popup/new tabs
        self.results_page.bring_to_front()  # Focus results tab
        reset_mouse_position()  # Reset tracked mouse position
        self.results_page.mouse.move(
            current_mouse_x, current_mouse_y
        )  # Move cursor to initial tracked position

    @staticmethod  # Helper does not access instance state
    def _close_tab_ignoring_errors(
        tab_to_close: Page,
    ) -> None:  # Close a tab safely if still open
        try:  # Attempt closing tab
            tab_to_close.close()  # Close tab
        except PlaywrightError:  # Ignore errors if already closed
            pass  # No-op

    def _close_unexpected_new_tab(
        self, new_tab: Page
    ) -> None:  # Close tabs that nobody asked for
        if self.is_download_tab_being_opened or new_tab in (
            self.results_page,
            self.download_page,
        ):  # Allow expected tabs
            return  # Keep expected tab
        download_logger.info(
            "Closing an unexpected new tab: %s", new_tab.url
        )  # Log unexpected tab closure
        self._close_tab_ignoring_errors(new_tab)  # Close unexpected tab

    def _list_all_browser_contexts(
        self,
    ) -> list:  # Every browser context, so tabs in stray windows are found too
        try:  # Read contexts from browser instance
            all_browser_contexts = list(
                self.connected_browser.contexts
            )  # Copy list of active contexts
        except (
            PlaywrightError,
            AttributeError,
        ):  # Fallback if browser disconnected or not initialized
            all_browser_contexts = []  # Empty list
        if (
            self.browser_context is not None
            and self.browser_context not in all_browser_contexts
        ):  # Ensure primary context is included
            all_browser_contexts.append(self.browser_context)  # Append primary context
        return all_browser_contexts  # Return list of contexts

    def _close_tabs_except(
        self, tabs_to_keep: tuple
    ) -> None:  # Close all open tabs across contexts except those in tabs_to_keep
        for (
            browser_context
        ) in self._list_all_browser_contexts():  # Iterate over contexts
            try:  # Snapshot open pages in context
                open_tabs = list(browser_context.pages)  # Copy open pages list
            except PlaywrightError:  # Skip context if inaccessible
                continue  # Next context
            for open_tab in open_tabs:  # Check each tab
                if open_tab not in tabs_to_keep:  # Close tab if not kept
                    download_logger.info(
                        "Closing an unneeded tab: %s", open_tab.url
                    )  # Log closing tab
                    self._close_tab_ignoring_errors(open_tab)  # Close tab safely

    def close_unneeded_tabs(
        self,
    ) -> None:  # Keep only the results tab (and the download tab while it is in use)
        self._close_tabs_except(
            (self.results_page, self.download_page)
        )  # Keep results and active download tab

    def close_all_tabs(self) -> None:  # Close all tabs across all contexts
        self._close_tabs_except(())  # Keep no tabs

    def handle_internet_outage(
        self,
    ) -> None:  # Called by the monitor thread when the internet goes down
        # Playwright's sync API must only be used from the thread that started it, so this thread  # Thread-affinity note part 1
        # cannot close the browser. The main thread notices the outage at its next step  # Thread-affinity note part 2
        # (raise_error_if_internet_is_down) and restart_browser() closes the old browser.  # Thread-affinity note part 3
        download_logger.error(
            "The internet is down: the browser will be closed and restarted by the main thread"
        )  # Log outage notification

    @contextmanager  # Context manager yielding a temporary tab for downloads and closing it afterward
    def temporary_download_tab(
        self,
    ) -> Iterator[Page]:  # Open a second tab only while downloads need it
        download_logger.info(
            "Opening a temporary second tab for downloading"
        )  # Log tab creation
        self.is_download_tab_being_opened = (
            True  # Allow new tab event during intentional creation
        )
        try:  # Open new tab via window.open or fallback new_page
            try:  # Prefer opening via window.open so it behaves like a normal tab
                with self.browser_context.expect_page(
                    timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS
                ) as new_tab_event_info:  # Await new Page from context
                    self.active_results_page.evaluate(
                        "() => { window.open('about:blank', '_blank'); }"
                    )  # Trigger window.open in results page
                new_download_page = new_tab_event_info.value  # Capture opened Page
            except PlaywrightTimeoutError:  # Probably blocked as a popup
                download_logger.warning(
                    "The tab did not open from the page, opening it directly instead"
                )  # Log fallback
                new_download_page = (
                    self.browser_context.new_page()
                )  # Create page directly on context
            self.active_results_page.bring_to_front()  # Downloads work from a background tab
        finally:  # Always clear opening guard flag
            self.is_download_tab_being_opened = False  # Reset flag

        self.download_page = new_download_page  # Store reference to active download tab
        attach_browser_event_logging(
            new_download_page
        )  # Attach event logging to download tab
        try:  # Yield download tab to caller
            yield new_download_page  # Provide download tab to with-block
        finally:  # Clean up download tab when exiting with-block
            self.download_page = None  # Clear active download tab reference
            download_logger.info(
                "Closing the temporary download tab"
            )  # Log tab closure
            self._close_tab_ignoring_errors(new_download_page)  # Close temporary tab
            try:  # Restore tab hygiene and focus on results tab
                self.close_unneeded_tabs()  # Close any stray tabs
                self.active_results_page.bring_to_front()  # Refocus results tab
            except PlaywrightError:  # Ignore errors if browser is shutting down
                pass  # No-op

    def stop_browser(self) -> None:  # Close tabs, context, and launcher cleanly
        if (
            self.invisible_playwright_launcher is not None
        ):  # Only stop if launcher was started
            try:  # Close tabs and browser context
                self.close_all_tabs()  # Close all open tabs
                if self.browser_context is not None:  # Close browser context if present
                    self.browser_context.close()  # Close context
            except PlaywrightError:  # Ignore Playwright errors during teardown
                pass  # No-op
            try:  # Exit InvisiblePlaywright launcher context
                self.invisible_playwright_launcher.__exit__(
                    None, None, None
                )  # Clean up browser process/resources
            except Exception as closing_error:  # Log any cleanup error at DEBUG level
                download_logger.debug(
                    "Ignoring error while closing the browser: %s", closing_error
                )  # Log cleanup error
        self.invisible_playwright_launcher = None  # Clear launcher reference
        self.connected_browser = None  # Clear browser reference
        self.browser_context = None  # Clear context reference
        self.results_page = None  # Clear results page reference
        self.download_page = None  # Clear download page reference

    def restart_browser(
        self,
    ) -> None:  # Start a fresh browser after the old one closed or crashed
        if (
            self.internet_monitor.read_and_clear_outage_flag()
        ):  # Check if restart was caused by internet outage
            download_logger.info(
                "This restart is due to an internet outage, so it does not count towards the limit"
            )  # Outage restarts don't count toward crash limit
        else:  # Count non-outage restarts toward MAXIMUM_BROWSER_RESTARTS
            self.browser_restart_count += 1  # Increment restart counter
            if (
                self.browser_restart_count > MAXIMUM_BROWSER_RESTARTS
            ):  # Give up if restart limit exceeded
                raise RuntimeError(
                    f"The browser closed more than {MAXIMUM_BROWSER_RESTARTS} times, giving up"
                )  # Raise runtime error
        download_logger.error(  # Log browser restart
            "Browser problem detected. Restarting (%d/%d)",
            self.browser_restart_count,
            MAXIMUM_BROWSER_RESTARTS,  # Format restart count
        )  # End error log
        self.stop_browser()  # Stop existing browser instance
        self.internet_monitor.wait_until_internet_available()  # Wait until internet connection is available
        time.sleep(3)  # Brief settle delay before relaunching
        self.start_browser()  # Launch fresh browser session
        warm_up_session_on_home_page(
            self.active_results_page
        )  # Re-warm session on home page
        self.close_unneeded_tabs()  # Ensure only results tab remains


def run_step_and_restart_browser_if_needed(
    browser_session: BrowserSession, step_description: str, step_to_run
):  # Retry step across browser restarts/outages
    """Run a step, transparently restarting the browser and retrying after a crash or internet outage."""  # Function docstring
    while True:  # Loop until step succeeds or non-recoverable error occurs
        try:  # Execute step callable
            return step_to_run()  # Return step result on success
        except InternetOutageError as outage_error:  # Handle detected internet outage
            download_logger.warning(
                "Internet outage during %s: %s", step_description, outage_error
            )  # Log outage
            browser_session.restart_browser()  # Restart browser once internet returns and retry step
        except PlaywrightError as browser_error:  # Handle Playwright errors
            if is_browser_closed_error(
                browser_error
            ):  # Restart if browser closed/crashed
                download_logger.error(  # Log closed browser error
                    "Browser closed during %s: %s",
                    step_description,
                    str(browser_error).splitlines()[0],  # Format step and error line
                )  # End error log
                browser_session.restart_browser()  # Restart browser and retry step
            elif (
                browser_session.internet_monitor.confirm_internet_is_down()
            ):  # Restart if error coincided with internet outage
                download_logger.warning(
                    "The internet is down, so the error during %s is expected",
                    step_description,
                )  # Log expected outage error
                browser_session.restart_browser()  # Restart browser and retry step
            else:  # Log unexpected non-closed Playwright error and skip this step
                download_logger.exception(
                    "Browser error during %s, skipping this step", step_description
                )  # Log exception with traceback
                return None  # Return None to indicate step skipped


# ============================================================== MAIN PROGRAM  # CLI entry point, keyword orchestration, and top-level run loop


def search_keywords_and_return_failed_ones(  # Run keyword searches sequentially and collect keywords that did not succeed
    browser_session: BrowserSession,  # Active browser session
    download_database: DownloadDatabase,  # Database instance
    keywords_to_search: list,  # List of keywords to search
    run_statistics: RunStatistics,  # Run statistics instance
) -> list:  # Search a list of keywords and return the ones that failed
    failed_keywords = []  # Collect keywords that failed or were skipped
    for keyword_position, search_keyword in enumerate(
        keywords_to_search, start=1
    ):  # Iterate over keywords with 1-based index
        if keyword_position > 1:  # Pause between keywords after the first
            pause_while_acting_like_a_human(  # Pause with human-like activity before next keyword
                browser_session.active_results_page,
                *PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS,
                "before the next keyword",  # Range and reason
            )  # End pause
        keyword_was_searched = run_step_and_restart_browser_if_needed(  # Search one keyword with crash/outage recovery
            browser_session,  # Browser session
            f"keyword '{search_keyword}'",  # Step description for logs
            lambda keyword_to_search=search_keyword: search_one_keyword_and_download_pdfs(  # Bind current keyword in closure
                browser_session,
                download_database,
                keyword_to_search,
                run_statistics,  # Pass arguments to search function
            ),  # End lambda
        )  # End run_step_and_restart_browser_if_needed
        if (
            keyword_was_searched is not True
        ):  # Record keyword as failed if result wasn't True
            failed_keywords.append(search_keyword)  # Append failed keyword
    return failed_keywords  # Return list of failed keywords


def parse_command_line_arguments() -> argparse.Namespace:  # Parse CLI arguments (--start-from)
    argument_parser = argparse.ArgumentParser(  # Create argument parser with program description
        description="Agilent PDF downloader. Jobs run in this order: Chemical Standards, then Safety Data Sheets."  # Program description
    )  # End ArgumentParser
    argument_parser.add_argument(  # Define --start-from option
        "--start-from",  # Option flag name
        choices=(
            JOB_CHEMICAL_STANDARDS,
            JOB_SAFETY_DATA_SHEETS,
        ),  # Allowed starting job choices
        default=JOB_CHEMICAL_STANDARDS,  # Default to starting with Chemical Standards
        help=(  # Detailed help text for --start-from
            "Which job to start from. 'chemical-standards' (the default) runs Chemical Standards and then "  # Default behavior description
            "Safety Data Sheets. 'safety-data-sheets' skips Chemical Standards and runs Safety Data Sheets only."  # SDS-only behavior description
        ),  # End help string
    )  # End add_argument
    return argument_parser.parse_args()  # Parse sys.argv and return Namespace


def run_agilent_downloader(
    start_from: str = JOB_CHEMICAL_STANDARDS,
) -> None:  # Run the whole job from start to finish
    configure_console_logging()  # Configure DEBUG console logging
    validate_settings()  # Validate configuration constants before starting
    run_chemical_standards = (
        start_from == JOB_CHEMICAL_STANDARDS
    )  # Safety Data Sheets always runs; it is the last job
    download_logger.info(  # Log starting job configuration
        "Starting from: %s | jobs to run: %s",  # Format string
        start_from,  # Chosen start job
        "Chemical Standards, then Safety Data Sheets"
        if run_chemical_standards
        else "Safety Data Sheets",  # Job sequence description
    )  # End info log
    download_logger.info(
        "Download folder: %s", DOWNLOAD_FOLDER
    )  # Log resolved download folder path

    DOWNLOAD_FOLDER.mkdir(exist_ok=True)  # Create download folder if it does not exist
    download_database = DownloadDatabase(DATABASE_FILE_PATH)  # Open SQLite database
    download_database.register_pdf_files_already_on_disk(
        DOWNLOAD_FOLDER
    )  # Scan disk for already-downloaded PDFs
    download_database.ignore_urls_in_unwanted_formats()  # Mark pending unwanted-format URLs as processed

    run_statistics = RunStatistics()  # Initialize run counters

    try:  # Top-level error handling for blocks, user interrupt, and unexpected errors
        internet_monitor = InternetMonitor()  # Create internet monitor
        browser_session = BrowserSession(internet_monitor)  # Create browser session
        try:  # Browser and monitor lifecycle block
            internet_monitor.start_monitoring(
                browser_session.handle_internet_outage
            )  # Start background connectivity monitoring
            internet_monitor.wait_until_internet_available()  # Wait until internet is available before launching browser
            browser_session.start_browser()  # Start browser session
            if not warm_up_session_on_home_page(
                browser_session.active_results_page
            ):  # Perform initial home page warm-up
                download_logger.warning(
                    "Warm-up did not finish cleanly, continuing anyway"
                )  # Log warm-up warning
            browser_session.close_unneeded_tabs()  # Ensure only results tab is open

            # ---------------- CHEMICAL STANDARDS: keyword search on the chemical standards pages  # Job 1 block header
            if run_chemical_standards:  # Run Chemical Standards job if selected
                download_logger.info(
                    "Keywords: %s", " ".join(SEARCH_KEYWORDS)
                )  # Log keyword list
                still_failed_keywords: list = (
                    []
                )  # Track keywords still failing after retry
                failed_keywords = search_keywords_and_return_failed_ones(  # Search all configured keywords
                    browser_session,
                    download_database,
                    SEARCH_KEYWORDS,
                    run_statistics,  # Pass session, DB, keywords, and stats
                )  # End initial keyword pass
                if failed_keywords:  # Retry any failed keywords once at the end
                    download_logger.info(  # Log retry pass
                        "Retrying %d keyword(s) that failed: %s",
                        len(failed_keywords),
                        " ".join(failed_keywords),  # Format retry count and list
                    )  # End info log
                    still_failed_keywords = search_keywords_and_return_failed_ones(  # Run second pass on failed keywords
                        browser_session,
                        download_database,
                        failed_keywords,
                        run_statistics,  # Pass failed keywords
                    )  # End retry pass
                    if (
                        still_failed_keywords
                    ):  # Log any keywords that still failed after retry
                        download_logger.warning(
                            "Keywords that still failed: %s",
                            " ".join(still_failed_keywords),
                        )  # Log remaining failed keywords

                download_logger.info(  # Log start of final version download pass
                    "All keywords searched: downloading the best available version of every remaining document"  # Info message
                )  # End info log
                run_step_and_restart_browser_if_needed(  # Download remaining best versions (including fallbacks if all keywords were searched)
                    browser_session,  # Browser session
                    "the final download of the best remaining versions",  # Step description
                    lambda: download_documents_using_temporary_tab(  # Run final download using temporary tab
                        browser_session,  # Session
                        download_database,  # Database
                        select_documents_for_final_download(
                            download_database, bool(still_failed_keywords)
                        ),  # Select documents for final pass
                        run_statistics,  # Statistics
                    ),  # End lambda
                )  # End run_step_and_restart_browser_if_needed
                log_run_statistics(
                    run_statistics, "CHEMICAL STANDARDS FINISHED"
                )  # Log Chemical Standards completion stats

            # ---------------- SAFETY DATA SHEETS: every "English - United States" document  # Job 2 block header
            run_step_and_restart_browser_if_needed(  # Run Safety Data Sheets job with restart support
                browser_session,  # Browser session
                "the US English Safety Data Sheets pages",  # Step description
                lambda: download_all_us_english_sds(
                    browser_session, download_database, run_statistics
                ),  # Run SDS downloader
            )  # End run_step_and_restart_browser_if_needed
            log_run_statistics(
                run_statistics, "SAFETY DATA SHEETS FINISHED"
            )  # Log Safety Data Sheets completion stats
        finally:  # Always stop monitor, close tabs, and stop browser when session exits
            internet_monitor.stop_monitoring()  # Stop background internet check thread
            browser_session.close_all_tabs()  # Close any remaining tabs
            browser_session.stop_browser()  # Shut down browser and Playwright launcher
    except AccessBlockedError as blocked_error:  # Handle repeated access blocks gracefully
        download_logger.error(
            "STOPPED EARLY: %s", blocked_error
        )  # Log block stop reason
    except KeyboardInterrupt:  # Handle Ctrl+C interruption gracefully
        download_logger.warning(
            "Stopped by the user; progress is saved and the next run continues from here"
        )  # Log user interruption
    except Exception:  # Log unexpected exceptions and re-raise
        download_logger.exception("Unexpected error, stopping")  # Log full traceback
        raise  # Re-raise unexpected exception
    finally:  # Always log final statistics and close SQLite database
        log_run_statistics(run_statistics, "FINAL")  # Log final run counters
        download_database.close_database()  # Close SQLite database connection
        download_logger.info("Database closed, finished")  # Log final shutdown message


if __name__ == "__main__":  # Run CLI only when executed directly as a script
    command_line_arguments = parse_command_line_arguments()  # Parse CLI arguments
    run_agilent_downloader(
        command_line_arguments.start_from
    )  # Start downloader with requested starting job
