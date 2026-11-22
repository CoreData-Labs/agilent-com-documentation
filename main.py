#!/usr/bin/env python3
# Agilent PDF downloader.
#
# It runs two jobs, always in this order:
#
# CHEMICAL STANDARDS
#   Downloads the Safety Data Sheet PDFs found on Agilent's chemical-standards search page.
#   Every document can exist in several regional versions; exactly ONE version is downloaded per document:
#     1. _NAEnglish.pdf is the default and is always preferred.
#     2. If it is not available, _NACanada.pdf is downloaded instead.
#     3. If neither is available, one of _Australia.pdf, _EUEnglish.pdf, _MalayEnglish.pdf or _UKEnglish.pdf is used.
#     4. If none of these versions exists, the document is skipped. Any other file format is ignored.
#
# SAFETY DATA SHEETS
#   Downloads every "English - United States" document from
#   https://www.agilent.com/en/search/safety-data-sheets
#   (the file names there look like 5190-8406_US.pdf or 18711-60060_NAEnglish.pdf).
#   * Every result page is opened directly by its address (c_ds_firstResult=0, 10, 20, ...).
#   * The site only lets you reach 5,000 results (500 pages), so the query asks for US English documents only
#     and, if that is still more than 5,000, the publication-date range is split in halves until every piece fits.
#   * Progress is saved in the database, so a stopped run continues at the page where it stopped.
#   * Clicking a PDF link opens the PDF in the browser (https://www.agilent.com/library/msds/<name>.pdf).
#     The script saves that PDF from the page it opened; if that does not work it falls back to
#     a direct request that reuses the browser's cookies.
#
# How to start (the job order never changes: Chemical Standards first, then Safety Data Sheets):
#   python agilent_downloader.py                                   -> Chemical Standards, then Safety Data Sheets
#   python agilent_downloader.py --start-from chemical-standards   -> the same as above
#   python agilent_downloader.py --start-from safety-data-sheets   -> skips Chemical Standards, runs Safety Data Sheets only
#
# The browser is controlled through invisible_playwright (Playwright on an anti-detect Firefox).
#
# How the script works:
#   * Before anything is requested, the download folder is checked: a document that already has a file there is skipped.
#   * It starts one browser window at 1920x1080 with a single "results" tab.
#   * It visits the home page and a few other pages first, behaving like an ordinary visitor.
#   * Chemical Standards searches the keywords a-z and 0-9, one after another. A default (_NAEnglish) version is
#     downloaded as soon as it is found; fallback versions wait until every keyword has been searched.
#   * After each page of results it downloads the PDFs found there, using a short-lived second tab
#     that is opened only when something needs downloading and closed right afterwards.
#   * Only the results tab is ever kept open; popups, extra tabs and extra windows are closed at once.
#   * A SQLite database remembers every URL, file name and Safety Data Sheets page position, so nothing is downloaded twice.
#   * If the browser crashes, or the internet drops, the browser is restarted and the work continues.
#   * Random pauses, back-off waits and a "stop after too many blocks" rule keep the traffic gentle.
#   * Keywords that could not be searched are retried once at the end.
#   * Everything is logged to the console at DEBUG level.

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

# ============================================================== SETTINGS

# ---- The two jobs
JOB_CHEMICAL_STANDARDS = "chemical-standards"  # Job one
JOB_SAFETY_DATA_SHEETS = "safety-data-sheets"  # Job two

# ---- Chemical Standards
SEARCH_URL_TEMPLATE = "https://www.agilent.com/en/product/chemical-standards/search-results?tabname=related_part_number&keyword={search_keyword}"
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
OTHER_VERSION_SUFFIXES = (
    "_Australia.pdf",
    "_EUEnglish.pdf",
    "_MalayEnglish.pdf",
    "_UKEnglish.pdf",
)  # Used only when neither version above is available; tried in this order
ALLOWED_VERSION_SUFFIXES_BEST_FIRST = (
    DEFAULT_VERSION_SUFFIX,
    FIRST_FALLBACK_VERSION_SUFFIX,
) + OTHER_VERSION_SUFFIXES  # Every format Chemical Standards may download, best first

# ---- Safety Data Sheets
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
SDS_WAIT_FOR_RESULTS_SECONDS = (
    25  # How long to wait for a result page to show its cards or its total
)

BROWSER_WINDOW_WIDTH_PIXELS = 1920
BROWSER_WINDOW_HEIGHT_PIXELS = 1080

DOWNLOAD_FOLDER = Path("PDFs").resolve()  # Full path of the folder where PDFs are saved
DATABASE_FILE_PATH = Path(
    "download_history.sqlite3"
).resolve()  # Full path of the SQLite history file
MAXIMUM_FILE_NAME_STEM_LENGTH = 150  # Longest allowed file name, not counting ".pdf"
MAXIMUM_URLS_PER_DATABASE_QUERY = 500  # Most "?" placeholders used in one SQL query

MAXIMUM_RESULT_PAGES_PER_KEYWORD = (
    200  # Safety limit on how many result pages are followed per keyword
)
PAGE_LOAD_TIMEOUT_MILLISECONDS = 30_000
DOWNLOAD_START_TIMEOUT_MILLISECONDS = (
    60_000  # How long to wait for the browser to start one download
)
PDF_RESPONSE_DOWNLOAD_WAIT_SECONDS = (
    15  # When the server answered with a file but no download starts, wait this long
)

PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES = (2.0, 5.0)
PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS = (1.0, 3.0)
PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS = (5.0, 10.0)
ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE = (3.0, 6.0)
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

INTERNET_CHECK_HOSTS = ("1.1.1.1", "8.8.8.8")
INTERNET_CHECK_INTERVAL_SECONDS = 10
PING_TIMEOUT_SECONDS = 5
FALLBACK_CONNECTION_TEST_PORT = 53
FALLBACK_CONNECTION_TEST_TIMEOUT_SECONDS = 2

download_logger = logging.getLogger("agilent_downloader")


def validate_settings() -> None:  # Stop immediately if a setting above is obviously wrong
    assert SEARCH_KEYWORDS, "SEARCH_KEYWORDS must contain at least one keyword"
    assert (
        BROWSER_WINDOW_WIDTH_PIXELS > 0 and BROWSER_WINDOW_HEIGHT_PIXELS > 0
    ), "The browser window size must be positive"
    assert all(
        version_suffix.lower().endswith(".pdf")
        for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST
    ), "Every version suffix must end with .pdf"
    assert len(
        {
            version_suffix.lower()
            for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST
        }
    ) == len(
        ALLOWED_VERSION_SUFFIXES_BEST_FIRST
    ), "The version suffixes must all be different"
    for pause_range in (
        PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
        PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,
        PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS,
        ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
    ):
        assert (
            0 <= pause_range[0] <= pause_range[1]
        ), f"Pause range {pause_range} must be (low, high) with low <= high"
    assert (
        SDS_EARLIEST_DATE < SDS_LATEST_DATE
    ), "SDS_EARLIEST_DATE must be before SDS_LATEST_DATE"
    assert (
        SDS_MAXIMUM_REACHABLE_RESULTS % SDS_RESULTS_PER_PAGE == 0
    ), "The reachable result count must be a whole number of pages"


class AccessBlockedError(Exception):
    """The website kept blocking us; the whole run should stop."""


class InternetOutageError(Exception):
    """The internet is down; the current step should be retried once it is back."""


@dataclass
class RunStatistics:  # Running totals for the whole run
    downloaded_count: int = 0  # PDFs saved to disk
    skipped_count: int = (
        0  # URLs skipped because the file already exists or is not a PDF
    )
    unavailable_count: int = 0  # URLs whose page says it may no longer exist
    failed_count: int = 0  # URLs that could not be downloaded this time
    consecutive_block_count: int = 0  # "Access Denied" pages seen in a row


# ============================================================== LOGGING


def configure_console_logging() -> None:  # Send every log message to the console at DEBUG level
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    download_logger.setLevel(logging.DEBUG)
    if (
        not download_logger.handlers
    ):  # Only attach a handler once, so lines are never printed twice
        download_logger.addHandler(console_handler)
    download_logger.propagate = False


def attach_browser_event_logging(browser_page: Page) -> None:
    """Log console output, errors, failed requests, navigation, downloads and popups of one tab."""
    browser_page.on(
        "console",
        lambda console_message: download_logger.debug(
            "BROWSER CONSOLE [%s] %s", console_message.type, console_message.text
        ),
    )
    browser_page.on(
        "pageerror",
        lambda page_error: download_logger.error("BROWSER PAGE ERROR %s", page_error),
    )
    browser_page.on(
        "requestfailed",
        lambda failed_request: download_logger.warning(
            "REQUEST FAILED %s %s -> %s",
            failed_request.method,
            failed_request.url,
            failed_request.failure,
        ),
    )
    browser_page.on(
        "response",
        lambda http_response: download_logger.warning(
            "HTTP %s %s", http_response.status, http_response.url
        )
        if http_response.status >= 400
        else None,
    )
    browser_page.on(
        "framenavigated",
        lambda navigated_frame: download_logger.info(
            "NAVIGATED to %s", navigated_frame.url
        )
        if navigated_frame.parent_frame is None
        else None,
    )
    browser_page.on(
        "download",
        lambda started_download: download_logger.info(
            "BROWSER DOWNLOAD STARTED url=%s suggested_name=%s",
            started_download.url,
            started_download.suggested_filename,
        ),
    )

    def handle_dialog_popup(
        dialog_popup: Dialog,
    ) -> None:  # Alert/confirm popups must never block us
        download_logger.warning(
            "BROWSER DIALOG [%s] %s", dialog_popup.type, dialog_popup.message
        )
        dialog_popup.dismiss()

    browser_page.on("dialog", handle_dialog_popup)


def log_run_statistics(run_statistics: RunStatistics, summary_label: str) -> None:
    download_logger.info(
        "%s: downloaded %d | skipped %d | page unavailable %d | failed %d",
        summary_label,
        run_statistics.downloaded_count,
        run_statistics.skipped_count,
        run_statistics.unavailable_count,
        run_statistics.failed_count,
    )


# ============================================================== SMALL HELPERS


def split_into_batches(items_to_split: list, maximum_batch_size: int) -> Iterator[list]:
    for batch_start_position in range(0, len(items_to_split), maximum_batch_size):
        yield items_to_split[
            batch_start_position : batch_start_position + maximum_batch_size
        ]


def remove_fragment_from_url(
    page_url: str,
) -> str:  # Remove the "#section" part of a web address
    return page_url.split("#")[0]


# ============================================================== DATABASE
#
# Three tables:
#   collected_urls   -- every PDF URL ever seen, and whether it has been handled
#   downloaded_files -- every file name actually saved, so duplicate names are caught too
#   sds_segments     -- Safety Data Sheets: every query piece (date range), its result count and the page it has reached


class DownloadDatabase:  # Wraps the SQLite connection and every query this script needs
    def __init__(self, database_path: Path):
        download_logger.info("Opening database: %s", database_path)
        self.database_connection = sqlite3.connect(database_path)
        self.safe_file_names_found_in_folder: set[
            str
        ] = set()  # Clean names of the PDFs seen in the download folder
        with self.database_connection:
            self.database_connection.execute(
                "CREATE TABLE IF NOT EXISTS collected_urls (source_url TEXT PRIMARY KEY, is_processed INTEGER NOT NULL DEFAULT 0)"
            )
            self.database_connection.execute(
                "CREATE TABLE IF NOT EXISTS downloaded_files (file_name TEXT PRIMARY KEY COLLATE NOCASE, source_url TEXT NOT NULL, downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            self.database_connection.execute(
                "CREATE TABLE IF NOT EXISTS sds_segments (segment_key TEXT PRIMARY KEY, total_results INTEGER NOT NULL, next_first_result INTEGER NOT NULL DEFAULT 0, is_finished INTEGER NOT NULL DEFAULT 0)"
            )

    def close_database(self) -> None:
        self.database_connection.close()

    def load_sds_segment(
        self, segment_key: str
    ) -> Optional[tuple]:  # (total, next first result, finished) or None
        database_row = self.database_connection.execute(
            "SELECT total_results, next_first_result, is_finished FROM sds_segments WHERE segment_key = ?",
            (segment_key,),
        ).fetchone()
        if database_row is None:
            return None
        return int(database_row[0]), int(database_row[1]), bool(database_row[2])

    def save_sds_segment(
        self,
        segment_key: str,
        total_results: int,
        next_first_result: int,
        is_finished: bool,
    ) -> None:
        with self.database_connection:
            self.database_connection.execute(
                "INSERT OR REPLACE INTO sds_segments (segment_key, total_results, next_first_result, is_finished) VALUES (?, ?, ?, ?)",
                (segment_key, total_results, next_first_result, int(is_finished)),
            )

    def register_pdf_files_already_on_disk(self, pdf_folder: Path) -> None:
        """Scan the download folder so every PDF that is already there is skipped and never downloaded again."""
        newly_registered_count = 0
        with self.database_connection:
            for pdf_path in pdf_folder.iterdir():
                if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
                    continue
                self.safe_file_names_found_in_folder.add(
                    build_safe_file_name(pdf_path.name)
                )
                insert_result = self.database_connection.execute(
                    "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",
                    (pdf_path.name, "found on disk before first run"),
                )
                newly_registered_count += insert_result.rowcount
        download_logger.info(
            "Download folder check: %d PDF file(s) found in %s (%d newly recorded); documents that already have a file are skipped",
            len(self.safe_file_names_found_in_folder),
            pdf_folder,
            newly_registered_count,
        )

    def ignore_urls_in_unwanted_formats(self) -> None:
        """Mark pending Chemical Standards URLs that are not in one of the wanted formats as done."""
        pending_urls = self.load_unprocessed_urls()
        ignored_urls = [
            pending_url
            for pending_url in pending_urls
            if not is_wanted_version_pdf_url(pending_url)
        ]
        for ignored_url in ignored_urls:
            self.mark_url_processed(ignored_url)
        download_logger.info(
            "Ignored %d pending URL(s) that are not in one of the %d wanted formats",
            len(ignored_urls),
            len(ALLOWED_VERSION_SUFFIXES_BEST_FIRST),
        )

    def save_newly_found_urls(self, found_urls: set) -> None:
        with self.database_connection:
            insert_result = self.database_connection.executemany(
                "INSERT OR IGNORE INTO collected_urls (source_url) VALUES (?)",
                [(found_url,) for found_url in found_urls],
            )
        download_logger.info(
            "Saved %d new URL(s) (%d were already known)",
            insert_result.rowcount,
            len(found_urls) - insert_result.rowcount,
        )

    def load_unprocessed_urls(self) -> list:
        database_rows = self.database_connection.execute(
            "SELECT source_url FROM collected_urls WHERE is_processed = 0 ORDER BY source_url"
        )
        return [database_row[0] for database_row in database_rows.fetchall()]

    def keep_only_unprocessed_urls(self, candidate_urls: list) -> list:
        pending_urls: set[str] = set()
        for url_batch in split_into_batches(
            candidate_urls, MAXIMUM_URLS_PER_DATABASE_QUERY
        ):
            placeholder_text = ",".join("?" for _url_in_batch in url_batch)
            database_rows = self.database_connection.execute(
                f"SELECT source_url FROM collected_urls WHERE is_processed = 0 AND source_url IN ({placeholder_text})",
                url_batch,
            )
            pending_urls.update(
                database_row[0] for database_row in database_rows.fetchall()
            )
        return [
            candidate_url
            for candidate_url in candidate_urls
            if candidate_url in pending_urls
        ]

    def mark_url_processed(self, source_url: str) -> None:
        with self.database_connection:
            self.database_connection.execute(
                "UPDATE collected_urls SET is_processed = 1 WHERE source_url = ?",
                (source_url,),
            )

    def is_file_name_already_saved(self, file_name: str) -> bool:
        """The download folder is checked first because the files on disk are the truth; the database is only a backup record."""
        if (
            file_name in self.safe_file_names_found_in_folder
            or (DOWNLOAD_FOLDER / file_name).exists()
        ):
            return True
        matching_row = self.database_connection.execute(
            "SELECT 1 FROM downloaded_files WHERE file_name = ?", (file_name,)
        ).fetchone()
        return matching_row is not None

    def record_saved_file(self, file_name: str, source_url: str) -> None:
        with self.database_connection:
            self.database_connection.execute(
                "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",
                (file_name, source_url),
            )
        self.safe_file_names_found_in_folder.add(file_name)

    def is_file_for_url_already_downloaded(self, source_url: str) -> bool:
        """If the file name that the URL implies is already known or on disk, there is no need to download it."""
        guessed_file_name = guess_file_name_from_url(source_url)
        if guessed_file_name is None:
            return False
        return self.is_file_name_already_saved(build_safe_file_name(guessed_file_name))

    def is_any_version_of_document_saved(self, document_key: str) -> bool:
        """A Chemical Standards document is finished as soon as one of its wanted versions has been saved."""
        for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:
            if self.is_file_name_already_saved(
                build_safe_file_name(document_key + version_suffix)
            ):
                return True
        return False


# ============================================================== FILE NAMES


def guess_file_name_from_url(
    source_url: str,
) -> Optional[str]:  # Predict the file name before downloading
    last_path_segment = Path(unquote(urlparse(source_url).path)).name
    return last_path_segment if last_path_segment.lower().endswith(".pdf") else None


def find_version_suffix_in_url(
    source_url: str,
) -> Optional[str]:  # Which wanted format (if any) is this address in
    lowercase_path = unquote(urlparse(source_url).path).lower()
    for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:
        if lowercase_path.endswith(version_suffix.lower()):
            return version_suffix
    return None


def is_wanted_version_pdf_url(source_url: str) -> bool:
    return find_version_suffix_in_url(source_url) is not None


def get_document_key_from_url(
    source_url: str,
) -> str:  # The file name without its format ending
    version_suffix = find_version_suffix_in_url(source_url) or ""
    lowercase_file_name = Path(unquote(urlparse(source_url).path)).name.lower()
    return lowercase_file_name[: len(lowercase_file_name) - len(version_suffix)]


def get_version_rank_from_url(
    source_url: str,
) -> int:  # 0 is the default version; higher numbers are less preferred
    version_suffix = find_version_suffix_in_url(source_url)
    if version_suffix is None:
        return len(ALLOWED_VERSION_SUFFIXES_BEST_FIRST)
    return ALLOWED_VERSION_SUFFIXES_BEST_FIRST.index(version_suffix)


def group_urls_by_document(
    source_urls,
) -> dict:  # Every version of the same document together, best version first
    urls_by_document: dict[str, list[str]] = {}
    for source_url in sorted(set(source_urls)):
        if not is_wanted_version_pdf_url(source_url):
            continue
        urls_by_document.setdefault(get_document_key_from_url(source_url), []).append(
            source_url
        )
    for candidate_urls in urls_by_document.values():
        candidate_urls.sort(key=get_version_rank_from_url)
    return urls_by_document


def build_safe_file_name(
    raw_file_name: str,
) -> str:  # Clean a file name so it follows the naming rules
    file_name_stem = (
        raw_file_name[:-4] if raw_file_name.lower().endswith(".pdf") else raw_file_name
    )
    file_name_stem = re.sub(r"[^a-z0-9_]+", "_", file_name_stem.lower())
    file_name_stem = re.sub(r"_+", "_", file_name_stem)
    version_ending = ""  # The format ending (such as _naenglish) must never be cut off when a long name is shortened
    for version_suffix in ALLOWED_VERSION_SUFFIXES_BEST_FIRST:
        if file_name_stem.endswith(version_suffix[:-4].lower()):
            version_ending = version_suffix[:-4].lower()
            break
    document_part = file_name_stem[: len(file_name_stem) - len(version_ending)]
    document_part = document_part[
        : MAXIMUM_FILE_NAME_STEM_LENGTH - len(version_ending)
    ].strip("_")
    return (document_part or "document") + version_ending + ".pdf"


# ============================================================== BLOCK TRACKING


def register_access_block(
    run_statistics: RunStatistics,
) -> None:  # Count one more "Access Denied" in a row
    run_statistics.consecutive_block_count += 1
    download_logger.warning(
        "Consecutive access blocks: %d/%d",
        run_statistics.consecutive_block_count,
        MAXIMUM_CONSECUTIVE_BLOCKS,
    )
    if run_statistics.consecutive_block_count >= MAXIMUM_CONSECUTIVE_BLOCKS:
        raise AccessBlockedError(
            f"The website blocked {MAXIMUM_CONSECUTIVE_BLOCKS} requests in a row. Stopping so the block does not get worse. Try again later or contact Agilent."
        )


def reset_access_blocks(
    run_statistics: RunStatistics,
) -> None:  # A page loaded fine, so the count starts again
    run_statistics.consecutive_block_count = 0


# ============================================================== PAGE CHECKS & ACTIONS

PAGE_UNAVAILABLE_MESSAGE = "the page may be temporarily unavailable or may no longer exist"  # Text on the website's error page


def is_access_denied_error_page(
    browser_page: Page,
) -> bool:  # Check whether the website blocked this visit
    try:
        page_title, page_source = browser_page.title(), browser_page.content()
    except PlaywrightError:
        return False
    lowercase_page_source = page_source.lower()
    is_blocked_by_website = (
        "access denied" in page_title.lower()
        or "errors.edgesuite.net" in lowercase_page_source
        or "you don't have permission to access" in lowercase_page_source
    )
    if is_blocked_by_website:
        block_reference_match = re.search(r"Reference #([0-9a-fA-F.]+)", page_source)
        download_logger.warning(
            "ACCESS DENIED page detected (reference %s) at %s",
            block_reference_match.group(1) if block_reference_match else "unknown",
            browser_page.url,
        )
    return is_blocked_by_website


def is_page_unavailable_error_page(
    browser_page: Page,
) -> bool:  # Check for the "page may no longer exist" error page
    try:
        visible_text = browser_page.inner_text("body", timeout=3000)
    except PlaywrightError:
        return False
    normalized_text = re.sub(r"\s+", " ", visible_text.lower())
    error_message_found = PAGE_UNAVAILABLE_MESSAGE in normalized_text
    if error_message_found:
        download_logger.warning(
            "PAGE UNAVAILABLE message found at %s", browser_page.url
        )
    return error_message_found


def is_results_page_usable(browser_page: Page, run_statistics: RunStatistics) -> bool:
    if is_page_unavailable_error_page(browser_page):
        return False
    if is_access_denied_error_page(browser_page):
        register_access_block(run_statistics)
        return False
    reset_access_blocks(run_statistics)
    return True


def wait_for_network_to_settle(
    browser_page: Page,
) -> None:  # Wait until the page stops loading data
    try:
        browser_page.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeoutError:
        pass
    time.sleep(2)  # Give the page's scripts a little extra time to draw the results


def dismiss_cookie_banner(
    browser_page: Page,
) -> None:  # Close the cookie popup if it shows up
    for accept_selector in ("#onetrust-accept-btn-handler", "button[id*='accept' i]"):
        try:
            accept_button = browser_page.locator(accept_selector).first
            if accept_button.is_visible():
                accept_button.click(timeout=3000)
                time.sleep(0.5)
                return
        except PlaywrightError:
            continue


def scroll_to_bottom_of_page(
    browser_page: Page, pause_seconds: float = 0.8
) -> None:  # Make lazily loaded results appear
    previous_page_height = 0
    for _scroll_round in range(15):
        browser_page.evaluate(
            "window.scrollTo(0, document.documentElement.scrollHeight)"
        )
        time.sleep(pause_seconds)
        current_page_height = browser_page.evaluate(
            "document.documentElement.scrollHeight"
        )
        if current_page_height == previous_page_height:
            break
        previous_page_height = current_page_height


PDF_OR_SAFETY_DATA_SHEET_LINK_PATTERN = re.compile(
    r"\.pdf(\?|#|$)|msds|/sds|sds[_/\-.]|safety[-_ ]?data", re.IGNORECASE
)  # Matches links that look like PDFs or safety data sheets
PDF_URL_INSIDE_PAGE_SOURCE_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE
)  # Matches .pdf addresses hidden inside the page source


def extract_pdf_urls_from_page(
    browser_page: Page,
) -> set:  # Every PDF address on the page that is in a wanted format
    found_urls = set()
    for link_url in browser_page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map(linkElement => linkElement.href)"
    ):
        if link_url.startswith("http") and PDF_OR_SAFETY_DATA_SHEET_LINK_PATTERN.search(
            link_url
        ):
            found_urls.add(link_url)
    page_source_text = browser_page.content().replace("\\/", "/")
    for url_found_in_source in PDF_URL_INSIDE_PAGE_SOURCE_PATTERN.findall(
        page_source_text
    ):
        found_urls.add(
            url_found_in_source.replace("\\u0026", "&").replace("&amp;", "&")
        )
    wanted_urls = {
        found_url for found_url in found_urls if is_wanted_version_pdf_url(found_url)
    }
    download_logger.debug(
        "Found %d PDF link(s), %d in a wanted format", len(found_urls), len(wanted_urls)
    )
    return wanted_urls


NEXT_PAGE_BUTTON_SELECTORS = [
    "button[aria-label*='next' i]",
    "a[aria-label*='next' i]",
    "li.next:not(.disabled) a",
    "a.next",
    "button.next",
    "[class*='pagination'] [class*='next']",
    "xpath=//button[normalize-space()='Next' or normalize-space()='>']",
    "xpath=//a[normalize-space()='Next' or normalize-space()='>']",
]


def click_next_page_button(
    browser_page: Page,
) -> bool:  # Go to the next page of results if there is one
    for next_button_selector in NEXT_PAGE_BUTTON_SELECTORS:
        matching_locator = browser_page.locator(next_button_selector)
        try:
            match_count = matching_locator.count()
        except PlaywrightError:
            continue
        for match_position in range(match_count):
            candidate_button = matching_locator.nth(match_position)
            try:
                if (
                    not candidate_button.is_visible()
                    or not candidate_button.is_enabled()
                ):
                    continue
                if candidate_button.get_attribute(
                    "aria-disabled"
                ) == "true" or "disabled" in (
                    candidate_button.get_attribute("class") or ""
                ):
                    continue
                click_element_like_a_human(browser_page, candidate_button)
                download_logger.info(
                    "Clicked next page button (%s)", next_button_selector
                )
                return True
            except PlaywrightError:
                continue
    download_logger.info("No usable next page button found")
    return False


# ============================================================== HUMAN-LIKE BEHAVIOUR
#
# Links, human-like input (curved mouse paths, wheel scrolling, random key presses) and the browsing routine.
# Random letters are only pressed, never submitted: Enter and other special keys are never used,
# and any focused text box is un-focused first, so no new search is ever started by accident.

LINKS_TO_AVOID_WHILE_BROWSING_PATTERN = re.compile(
    r"log-?in|log-?out|sign-?in|sign-?out|register|cart|checkout|account"
    r"|\.pdf|msds|/sds|mailto:|tel:|javascript:",
    re.IGNORECASE,
)
DESCRIBE_ALL_LINKS_SCRIPT = (
    "() => Array.from(document.querySelectorAll('a[href]')).map((linkElement, linkIndex) => {"
    "const linkBox = linkElement.getBoundingClientRect();"
    "const linkStyle = getComputedStyle(linkElement);"
    "return {"
    "index: linkIndex,"
    "href: linkElement.href,"
    "visible: linkBox.width > 20 && linkBox.height > 10 && linkStyle.visibility !== 'hidden' && linkStyle.display !== 'none',"
    "new_tab: linkElement.target === '_blank',"
    "download: linkElement.hasAttribute('download'),"
    "};"
    "})"
)
REMOVE_KEYBOARD_FOCUS_FROM_TEXT_BOXES_SCRIPT = (
    "() => { const focusedElement = document.activeElement;"
    " if (focusedElement && focusedElement !== document.body && (['INPUT', 'TEXTAREA', 'SELECT'].includes(focusedElement.tagName) || focusedElement.isContentEditable)) focusedElement.blur(); }"
)


def pick_random_internal_link(browser_page: Page, visited_urls: set) -> Optional[tuple]:
    candidate_links = []
    for link_description in browser_page.evaluate(DESCRIBE_ALL_LINKS_SCRIPT):
        link_url = link_description["href"]
        parsed_url = urlparse(link_url)
        website_host_name = parsed_url.hostname or ""
        if (
            not link_description["visible"]
            or link_description["new_tab"]
            or link_description["download"]
        ):
            continue
        if parsed_url.scheme not in ("http", "https"):
            continue
        if website_host_name != "agilent.com" and not website_host_name.endswith(
            ".agilent.com"
        ):
            continue
        if LINKS_TO_AVOID_WHILE_BROWSING_PATTERN.search(link_url):
            continue
        if remove_fragment_from_url(link_url) in visited_urls:
            continue
        candidate_links.append((link_description["index"], link_url))
    return random.choice(candidate_links) if candidate_links else None


current_mouse_x = (
    200.0  # Playwright cannot tell us where the pointer is, so we remember it
)
current_mouse_y = 200.0


def reset_mouse_position() -> None:
    global current_mouse_x, current_mouse_y
    current_mouse_x, current_mouse_y = 200.0, 200.0


def get_visible_page_size(browser_page: Page) -> tuple:
    visible_page_size = browser_page.viewport_size or {
        "width": BROWSER_WINDOW_WIDTH_PIXELS,
        "height": BROWSER_WINDOW_HEIGHT_PIXELS,
    }
    return visible_page_size["width"], visible_page_size["height"]


def move_mouse_like_a_human(
    browser_page: Page, pointer_target_x: float, pointer_target_y: float
) -> None:
    global current_mouse_x, current_mouse_y
    visible_page_width, visible_page_height = get_visible_page_size(browser_page)
    pointer_target_x = min(max(pointer_target_x, 1), visible_page_width - 1)
    pointer_target_y = min(max(pointer_target_y, 1), visible_page_height - 1)
    pointer_start_x, pointer_start_y = current_mouse_x, current_mouse_y
    travel_distance = math.hypot(
        pointer_target_x - pointer_start_x, pointer_target_y - pointer_start_y
    )
    if travel_distance < 2:
        return
    movement_step_count = max(12, min(60, int(travel_distance / 10)))
    curve_bend_amount = travel_distance * random.uniform(0.1, 0.3)
    control_point_x = (pointer_start_x + pointer_target_x) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()
    control_point_y = (pointer_start_y + pointer_target_y) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()
    for movement_step_number in range(1, movement_step_count + 1):
        progress_fraction = movement_step_number / movement_step_count
        eased_progress = (
            progress_fraction * progress_fraction * (3 - 2 * progress_fraction)
        )
        curve_point_x = (
            (1 - eased_progress) ** 2 * pointer_start_x
            + 2 * (1 - eased_progress) * eased_progress * control_point_x
            + eased_progress**2 * pointer_target_x
        )
        curve_point_y = (
            (1 - eased_progress) ** 2 * pointer_start_y
            + 2 * (1 - eased_progress) * eased_progress * control_point_y
            + eased_progress**2 * pointer_target_y
        )
        if movement_step_number < movement_step_count:
            curve_point_x += random.uniform(-1.0, 1.0)
            curve_point_y += random.uniform(-1.0, 1.0)
        browser_page.mouse.move(curve_point_x, curve_point_y)
        time.sleep(random.uniform(0.004, 0.014))
    current_mouse_x, current_mouse_y = pointer_target_x, pointer_target_y


def click_element_like_a_human(browser_page: Page, target_element) -> None:
    target_element.scroll_into_view_if_needed(timeout=5000)
    element_box = target_element.bounding_box()
    if element_box is None:
        target_element.click(timeout=5000)
        return
    click_position_x = element_box["x"] + element_box["width"] * random.uniform(
        0.3, 0.7
    )
    click_position_y = element_box["y"] + element_box["height"] * random.uniform(
        0.3, 0.7
    )
    move_mouse_like_a_human(browser_page, click_position_x, click_position_y)
    time.sleep(random.uniform(0.2, 0.6))
    browser_page.mouse.click(
        current_mouse_x, current_mouse_y, delay=random.randint(50, 150)
    )


def scroll_like_a_human(browser_page: Page, scroll_pixels: int) -> None:
    scroll_direction = 1 if scroll_pixels > 0 else -1
    remaining_pixels = abs(scroll_pixels)
    while remaining_pixels > 0:
        wheel_notch_pixels = min(remaining_pixels, random.randint(40, 120))
        browser_page.mouse.wheel(0, scroll_direction * wheel_notch_pixels)
        remaining_pixels -= wheel_notch_pixels
        time.sleep(random.uniform(0.02, 0.09))
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 0.9))


def press_random_letter_keys_like_a_human(browser_page: Page) -> None:
    browser_page.evaluate(REMOVE_KEYBOARD_FOCUS_FROM_TEXT_BOXES_SCRIPT)
    for _key_press_number in range(random.randint(2, 6)):
        random_letter = random.choice(string.ascii_lowercase)
        browser_page.keyboard.press(random_letter, delay=random.randint(40, 140))
        time.sleep(random.uniform(0.08, 0.35))
        if random.random() < 0.15:
            time.sleep(random.uniform(0.4, 1.0))


def act_like_a_human_on_page(browser_page: Page, duration_seconds: float) -> None:
    activity_end_time = time.time() + duration_seconds
    visible_page_width, visible_page_height = get_visible_page_size(browser_page)
    while time.time() < activity_end_time:
        try:
            random_action_choice = random.random()
            if random_action_choice < 0.2:
                press_random_letter_keys_like_a_human(browser_page)
            elif random_action_choice < 0.55:
                scroll_like_a_human(browser_page, random.randint(200, 650))
            elif random_action_choice < 0.9:
                move_mouse_like_a_human(
                    browser_page,
                    random.uniform(40, visible_page_width - 40),
                    random.uniform(40, visible_page_height - 40),
                )
            else:
                scroll_like_a_human(browser_page, -random.randint(100, 300))
        except PlaywrightError as browser_error:
            if is_browser_closed_error(browser_error):
                raise
        remaining_seconds = activity_end_time - time.time()
        if remaining_seconds > 0:
            time.sleep(min(remaining_seconds, random.uniform(0.5, 2.0)))


def pause_while_acting_like_a_human(
    browser_page: Page,
    minimum_seconds: float,
    maximum_seconds: float,
    pause_reason: str,
) -> None:  # Wait a random time while still looking active
    pause_seconds = random.uniform(minimum_seconds, maximum_seconds)
    download_logger.debug(
        "Pausing %.1f seconds with mouse and keyboard activity (%s)",
        pause_seconds,
        pause_reason,
    )
    try:
        act_like_a_human_on_page(browser_page, pause_seconds)
    except (PlaywrightError, PlaywrightTimeoutError) as browser_error:
        download_logger.debug(
            "Activity during the pause failed: %s", str(browser_error).splitlines()[0]
        )
        time.sleep(pause_seconds)


def open_random_link(browser_page: Page, visited_urls: set) -> Optional[str]:
    for _attempt_number in range(5):
        chosen_candidate = pick_random_internal_link(browser_page, visited_urls)
        if chosen_candidate is None:
            return None
        link_position, link_url = chosen_candidate
        visited_urls.add(remove_fragment_from_url(link_url))
        page_url_before_click = remove_fragment_from_url(browser_page.url)
        try:
            chosen_link = browser_page.locator("a[href]").nth(link_position)
            click_element_like_a_human(browser_page, chosen_link)
            browser_page.wait_for_url(
                lambda new_page_url, previous_page_url=page_url_before_click: remove_fragment_from_url(
                    str(new_page_url)
                )
                != previous_page_url,
                wait_until="domcontentloaded",
                timeout=8000,
            )  # type: ignore[misc]
        except (PlaywrightError, PlaywrightTimeoutError) as browser_error:
            if is_browser_closed_error(browser_error):
                raise
            download_logger.debug("Browsing: link %s did not open a page", link_url)
            continue
        return browser_page.url
    return None


def return_to_home_page(browser_page: Page, home_page_url: str) -> None:
    try:
        browser_page.go_back(wait_until="domcontentloaded", timeout=15_000)
    except (PlaywrightError, PlaywrightTimeoutError) as browser_error:
        if is_browser_closed_error(browser_error):
            raise
    if remove_fragment_from_url(browser_page.url) != remove_fragment_from_url(
        home_page_url
    ):
        browser_page.goto(home_page_url, wait_until="domcontentloaded")
    time.sleep(random.uniform(1.0, 2.0))


def browse_like_a_normal_visitor(
    browser_page: Page,
) -> None:  # Move around the home page, then open a few other pages
    try:
        home_page_url = browser_page.url
        visited_urls = {
            remove_fragment_from_url(home_page_url),
            remove_fragment_from_url(HOME_PAGE_URL),
        }
        download_logger.info(
            "Browsing: mouse, scrolling and random keys on the home page for about %d seconds",
            HOME_PAGE_BROWSING_SECONDS,
        )
        act_like_a_human_on_page(browser_page, HOME_PAGE_BROWSING_SECONDS)
        for visit_number in range(1, OTHER_PAGES_TO_VISIT + 1):
            visit_start_time = time.time()
            opened_page_url = open_random_link(browser_page, visited_urls)
            if opened_page_url is None:
                download_logger.warning(
                    "Browsing: could not open another page, stopping early"
                )
                break
            download_logger.info(
                "Browsing: page %d/%d opened (about %d seconds on it): %s",
                visit_number,
                OTHER_PAGES_TO_VISIT,
                OTHER_PAGE_BROWSING_SECONDS,
                opened_page_url,
            )
            seconds_left_on_page = max(
                2.0, OTHER_PAGE_BROWSING_SECONDS - (time.time() - visit_start_time)
            )
            act_like_a_human_on_page(browser_page, seconds_left_on_page)
            if visit_number < OTHER_PAGES_TO_VISIT:
                return_to_home_page(browser_page, home_page_url)
    except (PlaywrightError, PlaywrightTimeoutError) as browser_error:
        download_logger.warning(
            "Browsing like a normal visitor ended early: %s",
            str(browser_error).splitlines()[0],
        )
        return
    download_logger.info("Finished browsing like a normal visitor")


def warm_up_session_on_home_page(
    browser_page: Page,
) -> bool:  # Open the home page and browse around a little first
    download_logger.info("Warm-up: opening the home page %s", HOME_PAGE_URL)
    try:
        browser_page.goto(HOME_PAGE_URL, wait_until="domcontentloaded")
    except PlaywrightError as browser_error:
        download_logger.error(
            "Warm-up: could not open the home page: %s", browser_error
        )
        return False
    wait_for_network_to_settle(browser_page)
    dismiss_cookie_banner(browser_page)
    if is_access_denied_error_page(browser_page):
        download_logger.error("Warm-up: the home page itself shows Access Denied")
        return False
    browse_like_a_normal_visitor(browser_page)
    return True


PAGE_STATUS_OPENED = "opened"
PAGE_STATUS_UNAVAILABLE = "unavailable"
PAGE_STATUS_BLOCKED = "blocked"
PAGE_STATUS_FAILED = "failed"


def open_page_politely(
    browser_page: Page, page_url: str, run_statistics: RunStatistics
) -> str:
    """Open a page; if blocked, wait, visit the home page again, and retry a limited number of times."""
    for attempt_number in range(1, MAXIMUM_ACCESS_DENIED_ATTEMPTS + 1):
        download_logger.info(
            "Opening %s (attempt %d/%d)",
            page_url,
            attempt_number,
            MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        )
        try:
            browser_page.goto(page_url, wait_until="domcontentloaded")
        except PlaywrightError as browser_error:
            if is_browser_closed_error(browser_error):
                raise
            download_logger.error("Could not open %s: %s", page_url, browser_error)
            return PAGE_STATUS_FAILED
        wait_for_network_to_settle(browser_page)
        if is_page_unavailable_error_page(browser_page):
            return PAGE_STATUS_UNAVAILABLE
        if not is_access_denied_error_page(browser_page):
            reset_access_blocks(run_statistics)
            return PAGE_STATUS_OPENED
        register_access_block(run_statistics)
        if attempt_number < MAXIMUM_ACCESS_DENIED_ATTEMPTS:
            backoff_seconds = ACCESS_DENIED_BACKOFF_SECONDS * (
                2 ** (attempt_number - 1)
            ) + random.uniform(0, 5)
            download_logger.info(
                "Backing off for %.0f seconds, then revisiting the home page",
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
            warm_up_session_on_home_page(browser_page)
    download_logger.error(
        "Still blocked after %d attempt(s): %s",
        MAXIMUM_ACCESS_DENIED_ATTEMPTS,
        page_url,
    )
    return PAGE_STATUS_BLOCKED


# ============================================================== DOWNLOADING
#
# Order of attempts for one PDF address (this is what makes the Safety Data Sheets downloads work):
#   1. Open the address in the download tab. The browser either starts a download, or shows the PDF
#      (https://www.agilent.com/library/msds/<name>.pdf). When it shows the PDF, the bytes of that very page are saved.
#   2. If that gave nothing, request the PDF directly, reusing the browser's cookies.
#   3. If that gave nothing, look for a "download" button on the opened page and click it.

DOWNLOAD_OUTCOME_STARTED = "download started"  # The browser started downloading a file
DOWNLOAD_OUTCOME_PDF_PAGE = (
    "pdf page"  # The browser showed the PDF; its bytes were read from that page
)
DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE = "page unavailable"
DOWNLOAD_OUTCOME_ACCESS_DENIED = "access denied"
DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD = (
    "not a download"  # The link led to an ordinary web page
)
DOWNLOAD_OUTCOME_TIMED_OUT = "timeout"

PDF_CONTENT_TYPE_HINTS = (
    "pdf",
    "octet-stream",
    "force-download",
)  # Content types that mean "this is a file"


def start_browser_download(browser_page: Page, source_url: str) -> tuple:
    """Open a URL; returns (download or pdf bytes or None, outcome)."""
    first_started_download = None

    def remember_first_download(started_download: Download) -> None:
        nonlocal first_started_download
        if first_started_download is None:
            first_started_download = started_download

    browser_page.on("download", remember_first_download)
    try:
        try:
            navigation_response = browser_page.goto(
                source_url, wait_until="commit", timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS
            )
        except PlaywrightError as navigation_error:
            if "Download is starting" in str(
                navigation_error
            ) or "NS_BINDING_ABORTED" in str(navigation_error):
                navigation_response = (
                    None  # The navigation became a download straight away
                )
            else:
                raise

        if navigation_response is not None:
            response_content_type = navigation_response.headers.get(
                "content-type", ""
            ).lower()
            download_logger.debug(
                "  Server answered HTTP %s, content-type '%s' for %s",
                navigation_response.status,
                response_content_type,
                source_url,
            )
            if (
                "text/html" in response_content_type
            ):  # The server answered with a web page
                try:
                    browser_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                if is_page_unavailable_error_page(browser_page):
                    return None, DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE
                if is_access_denied_error_page(browser_page):
                    return None, DOWNLOAD_OUTCOME_ACCESS_DENIED
                return None, DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD
            if any(
                content_type_hint in response_content_type
                for content_type_hint in PDF_CONTENT_TYPE_HINTS
            ):
                try:  # The browser shows the PDF: read the bytes of this very page
                    pdf_bytes = navigation_response.body()
                except PlaywrightError as body_error:
                    pdf_bytes = None
                    download_logger.debug(
                        "  Could not read the PDF bytes of the page: %s",
                        str(body_error).splitlines()[0],
                    )
                if pdf_bytes and pdf_bytes[:4] == b"%PDF":
                    return pdf_bytes, DOWNLOAD_OUTCOME_PDF_PAGE

        wait_limit_seconds = (
            PDF_RESPONSE_DOWNLOAD_WAIT_SECONDS
            if navigation_response is not None
            else DOWNLOAD_START_TIMEOUT_MILLISECONDS / 1000
        )  # A real answer without a download event means the PDF is shown inline, so do not wait long
        download_wait_deadline = time.time() + wait_limit_seconds
        while first_started_download is None and time.time() < download_wait_deadline:
            browser_page.wait_for_timeout(200)
        if first_started_download is not None:
            return first_started_download, DOWNLOAD_OUTCOME_STARTED
        return None, DOWNLOAD_OUTCOME_TIMED_OUT
    finally:
        browser_page.remove_listener("download", remember_first_download)


URL_RESULT_SAVED = "saved"
URL_RESULT_FILE_ALREADY_EXISTS = "file already exists"
URL_RESULT_VERSION_NOT_AVAILABLE = "version not available"  # This version does not exist, so the next best version may be tried
URL_RESULT_TRY_AGAIN_LATER = "try again later"  # A temporary problem; no lower-priority version may be used instead


def mark_versions_as_skipped(
    download_database: DownloadDatabase,
    version_urls: list,
    run_statistics: RunStatistics,
    reason_text: str,
) -> None:
    for version_url in version_urls:
        download_logger.info("  SKIP: %s: %s", reason_text, version_url)
        download_database.mark_url_processed(version_url)
        run_statistics.skipped_count += 1


def skip_documents_already_in_download_folder(
    download_database: DownloadDatabase, found_urls: set, run_statistics: RunStatistics
) -> set:
    """Documents that already have a file are marked as finished and never requested."""
    urls_still_needed: set[str] = set()
    for document_key, version_urls in group_urls_by_document(found_urls).items():
        if download_database.is_any_version_of_document_saved(document_key):
            mark_versions_as_skipped(
                download_database,
                version_urls,
                run_statistics,
                "this document already has a file in the download folder",
            )
        else:
            urls_still_needed.update(version_urls)
    return urls_still_needed


def save_pdf_bytes(
    pdf_bytes: bytes,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
) -> str:
    """Save PDF bytes that were read from the browser page or from a direct request."""
    safe_file_name = build_safe_file_name(
        guess_file_name_from_url(source_url) or "document.pdf"
    )
    if download_database.is_file_name_already_saved(safe_file_name):
        download_logger.info("  DUPLICATE file name, not saved: %s", safe_file_name)
        download_database.mark_url_processed(source_url)
        run_statistics.skipped_count += 1
        return URL_RESULT_FILE_ALREADY_EXISTS
    safe_file_path = DOWNLOAD_FOLDER / safe_file_name
    safe_file_path.write_bytes(pdf_bytes)
    download_database.record_saved_file(safe_file_name, source_url)
    download_database.mark_url_processed(source_url)
    run_statistics.downloaded_count += 1
    download_logger.info("  SAVED: %s (%d bytes)", safe_file_path, len(pdf_bytes))
    return URL_RESULT_SAVED


def fetch_pdf_bytes_with_browser_cookies(
    download_page: Page, source_url: str
) -> Optional[bytes]:
    """Fallback: request the PDF directly, reusing the browser's cookies."""
    try:
        api_response = download_page.context.request.get(
            source_url,
            timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS,
            headers={"Accept": "application/pdf,*/*", "Referer": SDS_BASE_URL},
        )
        if not api_response.ok:
            download_logger.warning(
                "  Direct request answered HTTP %s for %s",
                api_response.status,
                source_url,
            )
            return None
        response_body = api_response.body()
    except PlaywrightError as browser_error:
        if is_browser_closed_error(browser_error):
            raise
        download_logger.warning(
            "  Direct request failed for %s: %s",
            source_url,
            str(browser_error).splitlines()[0],
        )
        return None
    if response_body[:4] != b"%PDF":
        download_logger.warning(
            "  Direct request did not return a PDF for %s", source_url
        )
        return None
    return response_body


VIEWER_DOWNLOAD_BUTTON_SELECTORS = (
    "#downloadButton",
    "#download",
    "button[title*='Download' i]",
    "button[aria-label*='Download' i]",
    "a[download]",
)


def click_viewer_download_button(download_page: Page) -> Optional[Download]:
    """Last resort: click a download button on the opened page and catch the download."""
    for button_selector in VIEWER_DOWNLOAD_BUTTON_SELECTORS:
        try:
            download_button = download_page.locator(button_selector).first
            if download_button.count() == 0:
                continue
            with download_page.expect_download(timeout=15_000) as download_event_info:
                download_button.click(timeout=5000)
            return download_event_info.value
        except (PlaywrightError, PlaywrightTimeoutError) as browser_error:
            if is_browser_closed_error(browser_error):
                raise
            continue
    return None


def try_fallback_downloads(
    download_page: Page,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
) -> Optional[str]:
    """Used when the browser did not hand over a file; returns a URL result, or None when nothing worked."""
    download_logger.info(
        "  The browser did not hand over a file, trying a direct request"
    )
    fetched_pdf_bytes = fetch_pdf_bytes_with_browser_cookies(download_page, source_url)
    if fetched_pdf_bytes is not None:
        return save_pdf_bytes(
            fetched_pdf_bytes, source_url, download_database, run_statistics
        )
    download_logger.info(
        "  Direct request gave nothing, looking for a download button on the page"
    )
    button_download = click_viewer_download_button(download_page)
    if button_download is not None:
        return check_and_save_finished_download(
            button_download, source_url, download_database, run_statistics
        )
    return None


def download_single_url(
    download_page: Page,
    download_database: DownloadDatabase,
    source_url: str,
    run_statistics: RunStatistics,
    internet_monitor: "InternetMonitor",
) -> str:  # Download one version and say what happened
    try:
        download_result, download_outcome = start_browser_download(
            download_page, source_url
        )
    except PlaywrightError as browser_error:
        if is_browser_closed_error(browser_error):
            raise  # The restart logic deals with it; this URL stays pending
        if internet_monitor.confirm_internet_is_down():
            raise InternetOutageError(
                "The internet went down while downloading"
            ) from browser_error
        download_logger.error(
            "  FAILED to open %s: %s", source_url, str(browser_error).splitlines()[0]
        )
        run_statistics.failed_count += 1
        return URL_RESULT_TRY_AGAIN_LATER

    if (
        download_outcome == DOWNLOAD_OUTCOME_PDF_PAGE
    ):  # The PDF was shown by the browser and its bytes were read
        reset_access_blocks(run_statistics)
        return save_pdf_bytes(
            download_result, source_url, download_database, run_statistics
        )

    if download_outcome in (
        DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD,
        DOWNLOAD_OUTCOME_TIMED_OUT,
    ):
        if (
            download_outcome == DOWNLOAD_OUTCOME_TIMED_OUT
            and internet_monitor.confirm_internet_is_down()
        ):
            raise InternetOutageError("The internet went down while downloading")
        fallback_result = try_fallback_downloads(
            download_page, source_url, download_database, run_statistics
        )
        if fallback_result is not None:
            reset_access_blocks(run_statistics)
            return fallback_result

    if download_outcome == DOWNLOAD_OUTCOME_PAGE_UNAVAILABLE:
        download_logger.warning("  SKIP: page unavailable, no PDF at %s", source_url)
        download_database.mark_url_processed(source_url)
        run_statistics.unavailable_count += 1
        reset_access_blocks(run_statistics)
        return URL_RESULT_VERSION_NOT_AVAILABLE
    if download_outcome == DOWNLOAD_OUTCOME_ACCESS_DENIED:
        download_logger.error("  FAILED: access denied for %s", source_url)
        run_statistics.failed_count += 1
        register_access_block(run_statistics)
        return URL_RESULT_TRY_AGAIN_LATER
    if download_outcome == DOWNLOAD_OUTCOME_NOT_A_DOWNLOAD:
        download_logger.warning(
            "  SKIP: the link is a web page, not a PDF, and no fallback worked: %s",
            source_url,
        )
        download_database.mark_url_processed(source_url)
        run_statistics.skipped_count += 1
        reset_access_blocks(run_statistics)
        return URL_RESULT_VERSION_NOT_AVAILABLE
    if download_result is None:  # Timed out and no fallback worked
        download_logger.error(
            "  FAILED: no download (%s) for %s", download_outcome, source_url
        )
        run_statistics.failed_count += 1
        return URL_RESULT_TRY_AGAIN_LATER
    reset_access_blocks(run_statistics)
    return check_and_save_finished_download(
        download_result, source_url, download_database, run_statistics
    )


def check_and_save_finished_download(
    started_download: Download,
    source_url: str,
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
) -> str:
    """Check that the file is a real PDF, give it a clean name, skip duplicates, and save it."""
    suggested_file_name = started_download.suggested_filename
    download_failure = started_download.failure()  # Waits for the download to end
    if download_failure:
        download_logger.error(
            "  FAILED: browser download error '%s' for %s", download_failure, source_url
        )
        run_statistics.failed_count += 1
        return URL_RESULT_TRY_AGAIN_LATER

    with open(started_download.path(), "rb") as downloaded_file_handle:
        file_start_bytes = downloaded_file_handle.read(4)
    if file_start_bytes != b"%PDF":
        download_logger.error(
            "  FAILED: downloaded file is not a PDF, treating this version as unavailable: %s",
            source_url,
        )
        started_download.delete()
        download_database.mark_url_processed(source_url)
        run_statistics.failed_count += 1
        return URL_RESULT_VERSION_NOT_AVAILABLE

    safe_file_name = build_safe_file_name(suggested_file_name)
    safe_file_path = DOWNLOAD_FOLDER / safe_file_name
    if download_database.is_file_name_already_saved(safe_file_name):
        download_logger.info("  DUPLICATE file name, not saved: %s", safe_file_name)
        started_download.delete()
        download_database.mark_url_processed(source_url)
        run_statistics.skipped_count += 1
        return URL_RESULT_FILE_ALREADY_EXISTS

    started_download.save_as(safe_file_path)
    download_database.record_saved_file(safe_file_name, source_url)
    download_database.mark_url_processed(source_url)
    run_statistics.downloaded_count += 1
    download_logger.info(
        "  SAVED: %s (%d bytes)", safe_file_path, safe_file_path.stat().st_size
    )
    return URL_RESULT_SAVED


def download_best_version_of_each_document(
    download_page: Page,
    download_database: DownloadDatabase,
    documents_to_download: dict,
    run_statistics: RunStatistics,
    internet_monitor: "InternetMonitor",
    page_for_mouse_activity: Page,
) -> None:
    """Try the versions of each document best first, stop at the first one that is saved, skip all the others."""
    already_contacted_website = False
    for document_position, (document_key, candidate_urls) in enumerate(
        documents_to_download.items(), start=1
    ):
        download_logger.info(
            "(%d/%d) Document '%s' has %d version(s) to try, best first",
            document_position,
            len(documents_to_download),
            document_key,
            len(candidate_urls),
        )
        for version_position, source_url in enumerate(candidate_urls):
            internet_monitor.raise_error_if_internet_is_down()
            lower_priority_urls = candidate_urls[version_position + 1 :]
            download_logger.info(
                "  Trying version %d of %d: %s",
                version_position + 1,
                len(candidate_urls),
                source_url,
            )

            if download_database.is_file_for_url_already_downloaded(source_url):
                download_logger.info("  SKIP: the file for this URL already exists")
                download_database.mark_url_processed(source_url)
                run_statistics.skipped_count += 1
                url_result = URL_RESULT_FILE_ALREADY_EXISTS
            else:
                if already_contacted_website:
                    page_for_mouse_activity.bring_to_front()
                    pause_while_acting_like_a_human(
                        page_for_mouse_activity,
                        *PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,
                        "between downloads",
                    )
                already_contacted_website = True
                url_result = download_single_url(
                    download_page,
                    download_database,
                    source_url,
                    run_statistics,
                    internet_monitor,
                )

            if url_result in (URL_RESULT_SAVED, URL_RESULT_FILE_ALREADY_EXISTS):
                mark_versions_as_skipped(
                    download_database,
                    lower_priority_urls,
                    run_statistics,
                    "a better version of this document is already saved",
                )
                break
            if url_result == URL_RESULT_TRY_AGAIN_LATER:
                download_logger.warning(
                    "  This version will be retried later, so no lower-priority version is downloaded now"
                )
                break
            if lower_priority_urls:
                download_logger.info(
                    "  This version is not available, trying the next best version"
                )
            else:
                download_logger.warning(
                    "  No wanted version of this document could be downloaded, skipping it"
                )

    log_run_statistics(run_statistics, "Totals so far")


def keep_only_documents_with_default_version(urls_grouped_by_document: dict) -> dict:
    return {
        document_key: [candidate_urls[0]]
        for document_key, candidate_urls in urls_grouped_by_document.items()
        if candidate_urls and get_version_rank_from_url(candidate_urls[0]) == 0
    }


def select_documents_with_default_version(
    download_database: DownloadDatabase, found_urls: set
) -> dict:
    pending_urls = download_database.keep_only_unprocessed_urls(sorted(found_urls))
    return keep_only_documents_with_default_version(
        group_urls_by_document(pending_urls)
    )


def select_documents_for_final_download(
    download_database: DownloadDatabase, some_keywords_were_not_searched: bool
) -> dict:
    urls_grouped_by_document = group_urls_by_document(
        download_database.load_unprocessed_urls()
    )
    if some_keywords_were_not_searched:
        download_logger.warning(
            "Some keywords were not searched, so only default versions are downloaded now; fallback versions wait for the next run"
        )
        urls_grouped_by_document = keep_only_documents_with_default_version(
            urls_grouped_by_document
        )
    return urls_grouped_by_document


def download_documents_using_temporary_tab(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    documents_to_download: dict,
    run_statistics: RunStatistics,
) -> None:
    """Skip documents that already have a file, then open the download tab only if something is left to do."""
    documents_still_needed = {}
    for document_key, candidate_urls in documents_to_download.items():
        if download_database.is_any_version_of_document_saved(document_key):
            mark_versions_as_skipped(
                download_database,
                candidate_urls,
                run_statistics,
                "this document already has a saved version",
            )
        else:
            documents_still_needed[document_key] = candidate_urls
    if not documents_still_needed:
        download_logger.info(
            "Nothing to download in this batch, so no second tab is opened"
        )
        return
    browser_session.internet_monitor.raise_error_if_internet_is_down()
    with browser_session.temporary_download_tab() as download_page:
        download_best_version_of_each_document(
            download_page,
            download_database,
            documents_still_needed,
            run_statistics,
            browser_session.internet_monitor,
            browser_session.active_results_page,
        )


# ============================================================== CHEMICAL STANDARDS (KEYWORD SEARCH)


def search_one_keyword_and_download_pdfs(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    search_keyword: str,
    run_statistics: RunStatistics,
) -> bool:  # Search one keyword; returns True when it was searched
    results_page = browser_session.active_results_page
    browser_session.close_unneeded_tabs()
    browser_session.internet_monitor.raise_error_if_internet_is_down()
    search_url = SEARCH_URL_TEMPLATE.format(search_keyword=search_keyword)
    download_logger.info(
        "Opening search page for keyword '%s': %s", search_keyword, search_url
    )

    page_status = open_page_politely(results_page, search_url, run_statistics)
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):
        raise InternetOutageError(
            "The internet went down while opening the search page"
        )
    if page_status != PAGE_STATUS_OPENED:
        download_logger.warning(
            "[%s] skipping this keyword for now (page status: %s)",
            search_keyword,
            page_status,
        )
        return False
    dismiss_cookie_banner(results_page)
    pause_while_acting_like_a_human(
        results_page,
        *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
        "after opening the search page",
    )

    all_keyword_urls: set[str] = set()
    for page_number in range(1, MAXIMUM_RESULT_PAGES_PER_KEYWORD + 1):
        browser_session.close_unneeded_tabs()
        browser_session.internet_monitor.raise_error_if_internet_is_down()
        results_page.bring_to_front()
        if page_number > 1:
            pause_while_acting_like_a_human(
                results_page,
                *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
                "after opening a result page",
            )
        scroll_to_bottom_of_page(results_page)

        urls_on_this_page = extract_pdf_urls_from_page(results_page)
        new_urls = urls_on_this_page - all_keyword_urls
        all_keyword_urls |= urls_on_this_page
        download_logger.info(
            "[%s] page %d: %d link(s), %d new, %d total",
            search_keyword,
            page_number,
            len(urls_on_this_page),
            len(new_urls),
            len(all_keyword_urls),
        )

        if new_urls:
            download_database.save_newly_found_urls(new_urls)
            urls_without_files = skip_documents_already_in_download_folder(
                download_database, new_urls, run_statistics
            )
            default_version_documents = select_documents_with_default_version(
                download_database, urls_without_files
            )
            download_logger.info(
                "[%s] page %d: downloading %d default version(s) right away; fallback versions wait until every keyword is searched",
                search_keyword,
                page_number,
                len(default_version_documents),
            )
            download_documents_using_temporary_tab(
                browser_session,
                download_database,
                default_version_documents,
                run_statistics,
            )

        if page_number > 1 and not new_urls:
            download_logger.info(
                "[%s] no new links on this page, stopping", search_keyword
            )
            break

        pause_while_acting_like_a_human(
            results_page,
            *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
            "before the next result page",
        )
        results_page.bring_to_front()
        if not click_next_page_button(results_page):
            break
        wait_for_network_to_settle(results_page)
        if not is_results_page_usable(results_page, run_statistics):
            download_logger.warning(
                "[%s] the next result page is not usable, moving on", search_keyword
            )
            break

    download_logger.info(
        "[%s] finished with %d PDF link(s)", search_keyword, len(all_keyword_urls)
    )
    del all_keyword_urls
    browser_session.close_unneeded_tabs()
    return True


# ============================================================== SAFETY DATA SHEETS (US ENGLISH)
#
# The page https://www.agilent.com/en/search/safety-data-sheets shows 10 cards per page. Every card has
#   <a class="pub-ext" href="/library/msds/5190-8406_US.pdf">  and tags such as  <span class="meta-text">English - United States</span>
# Clicking the link opens the PDF at https://www.agilent.com/library/msds/<name>.pdf.
#
# Result pages are opened directly by address:
#   ...?cq=<query>&searchAttempt=modified&c_ds_f-agilent_doc_language_long=English&c_ds_firstResult=10   (page 2)
#
# The site never shows more than 5,000 results (500 pages) for one query. So the query is cut into "segments":
#   * The first segment is every US English document.
#   * If a segment has more than 5,000 results, its publication-date range is split in two halves,
#     and each half is a new segment, until every segment fits.
# The state of every segment (total, page reached, finished) is stored in the database, so a stopped run
# continues exactly where it stopped and finished segments are never requested again.

US_ENGLISH_LABEL = (
    "English - United States"  # The language tag on the cards that is wanted
)

READ_SDS_CARDS_SCRIPT = (
    "(scopeSelector) => {"
    "const scope = document.querySelector(scopeSelector);"
    "if (!scope) return [];"
    "return Array.from(scope.querySelectorAll('.compendiumcard')).map(card => {"
    "const link = card.querySelector('a.pub-ext') || card.querySelector('a[href]');"
    "const labels = Array.from(card.querySelectorAll('.meta-text')).map(tag => tag.innerText.trim());"
    "return {href: link ? link.href : null, labels: labels};"
    "});"
    "}"
)
READ_SDS_TOTAL_SCRIPT = (
    "(scopeSelector) => {"
    "const scope = document.querySelector(scopeSelector);"
    "if (!scope) return null;"
    "const pagination = scope.querySelector('agt-pagination');"
    "const rawTotal = (pagination && pagination.getAttribute('data-total-results')) || scope.getAttribute('data-no-of-results');"
    "const total = parseInt(rawTotal, 10);"
    "return Number.isNaN(total) ? null : total;"
    "}"
)
READ_SDS_NO_RESULTS_SCRIPT = (
    "(scopeSelector) => {"
    "const scope = document.querySelector(scopeSelector);"
    "if (!scope) return false;"
    "return /no results|showing 0 results/i.test(scope.innerText || '');"
    "}"
)


def normalize_label(
    label_text: str,
) -> str:  # Lowercase, and turn every dash/space/odd character into one space
    return re.sub(r"[^a-z]+", " ", label_text.lower()).strip()


def card_is_us_english(
    card_description: dict,
) -> bool:  # True when any tag on the card says English + United States
    for label_text in card_description.get("labels", []):
        normalized_label = normalize_label(label_text)
        if "english" in normalized_label and "united states" in normalized_label:
            return True
    return False


def read_sds_cards(
    browser_page: Page,
) -> list:  # Every card of the current result page that has a link
    card_descriptions = browser_page.evaluate(
        READ_SDS_CARDS_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
    )
    return [
        card_description
        for card_description in card_descriptions
        if card_description.get("href")
    ]


def get_us_english_urls_from_cards(
    card_descriptions: list,
) -> set:  # Keep only the cards tagged "English - United States"
    us_english_urls = {
        card_description["href"]
        for card_description in card_descriptions
        if card_is_us_english(card_description)
        and ".pdf" in urlparse(card_description["href"]).path.lower()
    }
    if card_descriptions and not us_english_urls:
        download_logger.warning(
            "[SDS page] %d card(s) but none tagged '%s'. First tags seen: %s",
            len(card_descriptions),
            US_ENGLISH_LABEL,
            [card_description["labels"] for card_description in card_descriptions[:3]],
        )
    return us_english_urls


def wait_for_sds_cards_to_appear(browser_page: Page, timeout_seconds: float) -> bool:
    wait_deadline = time.time() + timeout_seconds
    while time.time() < wait_deadline:
        try:
            if read_sds_cards(browser_page):
                return True
        except PlaywrightError as browser_error:
            if is_browser_closed_error(browser_error):
                raise
        time.sleep(0.5)
    return False


def wait_for_sds_total_results(
    browser_page: Page, timeout_seconds: float
) -> Optional[int]:
    wait_deadline = time.time() + timeout_seconds
    while time.time() < wait_deadline:
        try:
            total_results = browser_page.evaluate(
                READ_SDS_TOTAL_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
            )
            if total_results is not None:
                return int(total_results)
        except PlaywrightError as browser_error:
            if is_browser_closed_error(browser_error):
                raise
        time.sleep(0.5)
    try:  # The total never appeared; look at what the page shows instead
        if read_sds_cards(browser_page):
            download_logger.warning(
                "[SDS page] the total is not readable, assuming the maximum reachable number of results"
            )
            return SDS_MAXIMUM_REACHABLE_RESULTS
        if browser_page.evaluate(
            READ_SDS_NO_RESULTS_SCRIPT, SDS_RESULTS_SCOPE_SELECTOR
        ):
            return 0
    except PlaywrightError as browser_error:
        if is_browser_closed_error(browser_error):
            raise
    return None


def format_date_clause(range_start: Optional[date], range_end: Optional[date]) -> list:
    if range_start is None or range_end is None:
        return []
    return [
        f"@{SDS_DATE_FIELD}>={range_start.strftime('%Y/%m/%d')}",
        f"@{SDS_DATE_FIELD}<={range_end.strftime('%Y/%m/%d')}",
    ]


def build_sds_page_url(
    use_locale_clause: bool,
    range_start: Optional[date],
    range_end: Optional[date],
    first_result: int,
) -> str:  # The address of one result page, in the same style as the addresses of the site
    query_parts = [SDS_OBJECT_TYPE_CLAUSE]
    if use_locale_clause:
        query_parts.append(SDS_LOCALE_CLAUSE)
    query_parts.extend(format_date_clause(range_start, range_end))
    address_parameters = [
        ("cq", " AND ".join(query_parts)),
        ("searchAttempt", "modified"),
        (SDS_LANGUAGE_FACET_PARAMETER, SDS_LANGUAGE_FACET_VALUE),
    ]
    if first_result > 0:
        address_parameters.append((SDS_FIRST_RESULT_PARAMETER, str(first_result)))
    return SDS_BASE_URL + "?" + urlencode(address_parameters)


def build_sds_segment_key(
    use_locale_clause: bool, range_start: Optional[date], range_end: Optional[date]
) -> str:
    mode_name = "us" if use_locale_clause else "en"
    start_text = range_start.isoformat() if range_start else "all"
    end_text = range_end.isoformat() if range_end else "all"
    return f"{mode_name}|{start_text}|{end_text}"


def split_date_range(
    range_start: Optional[date], range_end: Optional[date]
) -> Optional[tuple]:
    if range_start is None or range_end is None:
        range_start, range_end = SDS_EARLIEST_DATE, SDS_LATEST_DATE
    if range_start >= range_end:  # A single day cannot be cut
        return None
    middle_date = range_start + (range_end - range_start) // 2
    return (range_start, middle_date), (middle_date + timedelta(days=1), range_end)


def probe_sds_segment_total(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
    use_locale_clause: bool,
    range_start: Optional[date],
    range_end: Optional[date],
) -> Optional[
    int
]:  # How many results a segment has; None when the page could not be read
    segment_key = build_sds_segment_key(use_locale_clause, range_start, range_end)
    saved_state = download_database.load_sds_segment(segment_key)
    if saved_state is not None:
        return saved_state[0]
    results_page = browser_session.active_results_page
    browser_session.close_unneeded_tabs()
    browser_session.internet_monitor.raise_error_if_internet_is_down()
    page_url = build_sds_page_url(use_locale_clause, range_start, range_end, 0)
    page_status = open_page_politely(results_page, page_url, run_statistics)
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_is_down()
    ):
        raise InternetOutageError("The internet went down while counting the results")
    if page_status != PAGE_STATUS_OPENED:
        download_logger.warning(
            "[SDS page] could not count the results of segment %s (status: %s)",
            segment_key,
            page_status,
        )
        return None
    dismiss_cookie_banner(results_page)
    total_results = wait_for_sds_total_results(
        results_page, SDS_WAIT_FOR_RESULTS_SECONDS
    )
    if total_results is None:
        download_logger.warning(
            "[SDS page] the result count of segment %s could not be read", segment_key
        )
        return None
    download_database.save_sds_segment(segment_key, total_results, 0, False)
    download_logger.info(
        "[SDS page] segment %s has %d result(s)", segment_key, total_results
    )
    pause_while_acting_like_a_human(
        results_page,
        *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
        "after counting a segment",
    )
    return total_results


def choose_sds_query_mode(
    browser_session: "BrowserSession", run_statistics: RunStatistics
) -> Optional[bool]:
    """Returns True (US English query works), False (use the plain English filter) or None (nothing works)."""
    results_page = browser_session.active_results_page
    modes_to_try = [True, False] if USE_LOCALE_CLAUSE else [False]
    for use_locale_clause in modes_to_try:
        browser_session.internet_monitor.raise_error_if_internet_is_down()
        page_url = build_sds_page_url(use_locale_clause, None, None, 0)
        page_status = open_page_politely(results_page, page_url, run_statistics)
        if (
            page_status == PAGE_STATUS_FAILED
            and browser_session.internet_monitor.confirm_internet_is_down()
        ):
            raise InternetOutageError(
                "The internet went down while opening the SDS page"
            )
        if page_status != PAGE_STATUS_OPENED:
            download_logger.warning(
                "[SDS page] could not open %s (status: %s)", page_url, page_status
            )
            continue
        dismiss_cookie_banner(results_page)
        if not wait_for_sds_cards_to_appear(results_page, SDS_WAIT_FOR_RESULTS_SECONDS):
            download_logger.warning(
                "[SDS page] no result cards at %s, trying the next query style",
                page_url,
            )
            continue
        card_descriptions = read_sds_cards(results_page)
        if use_locale_clause and not all(
            card_is_us_english(card_description)
            for card_description in card_descriptions
        ):
            download_logger.warning(
                "[SDS page] the US English query is not honoured by the site, using the plain English filter instead"
            )
            continue
        download_logger.info(
            "[SDS page] query style chosen: %s",
            "US English query"
            if use_locale_clause
            else "plain English filter (cards are filtered by their tag)",
        )
        return use_locale_clause
    return None


def download_us_english_urls(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    us_english_urls: set,
    run_statistics: RunStatistics,
) -> bool:  # Download every address that has no file yet; returns True when a request was made
    pending_urls = sorted(
        us_english_url
        for us_english_url in us_english_urls
        if not download_database.is_file_for_url_already_downloaded(us_english_url)
    )  # The download folder is checked first, so existing files are never requested again
    skipped_count = len(us_english_urls) - len(pending_urls)
    run_statistics.skipped_count += skipped_count
    if not pending_urls:
        download_logger.info(
            "[SDS page] nothing new to download (%d already saved)", skipped_count
        )
        return False
    if skipped_count:
        download_logger.info(
            "[SDS page] %d of %d file(s) on this page already exist and are skipped",
            skipped_count,
            len(us_english_urls),
        )
    download_database.save_newly_found_urls(set(pending_urls))
    browser_session.internet_monitor.raise_error_if_internet_is_down()
    with browser_session.temporary_download_tab() as download_page:
        for url_position, pending_url in enumerate(pending_urls):
            browser_session.internet_monitor.raise_error_if_internet_is_down()
            if url_position > 0:
                browser_session.active_results_page.bring_to_front()
                pause_while_acting_like_a_human(
                    browser_session.active_results_page,
                    *PAUSE_SECONDS_RANGE_BETWEEN_DOWNLOADS,
                    "between downloads",
                )
            download_logger.info(
                "  (%d/%d) US English: %s",
                url_position + 1,
                len(pending_urls),
                pending_url,
            )
            download_single_url(
                download_page,
                download_database,
                pending_url,
                run_statistics,
                browser_session.internet_monitor,
            )
    log_run_statistics(run_statistics, "Totals so far")
    return True


def walk_sds_segment(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
    use_locale_clause: bool,
    range_start: Optional[date],
    range_end: Optional[date],
    total_results: int,
    first_result: int,
) -> bool:  # Open the pages of one segment one by one and download the US English cards; True when finished
    segment_key = build_sds_segment_key(use_locale_clause, range_start, range_end)
    results_page = browser_session.active_results_page
    reachable_results = min(total_results, SDS_MAXIMUM_REACHABLE_RESULTS)
    previous_page_links: Optional[tuple] = None  # To notice a page that does not change
    while first_result < reachable_results:
        browser_session.close_unneeded_tabs()
        browser_session.internet_monitor.raise_error_if_internet_is_down()
        results_page.bring_to_front()
        page_url = build_sds_page_url(
            use_locale_clause, range_start, range_end, first_result
        )
        page_status = open_page_politely(results_page, page_url, run_statistics)
        if (
            page_status == PAGE_STATUS_FAILED
            and browser_session.internet_monitor.confirm_internet_is_down()
        ):
            raise InternetOutageError(
                "The internet went down while opening a result page"
            )
        if page_status != PAGE_STATUS_OPENED:
            download_logger.warning(
                "[SDS page] segment %s: could not open the page at result %d (status: %s); it will be continued next run",
                segment_key,
                first_result,
                page_status,
            )
            return False
        dismiss_cookie_banner(results_page)
        if not wait_for_sds_cards_to_appear(results_page, SDS_WAIT_FOR_RESULTS_SECONDS):
            download_logger.warning(
                "[SDS page] segment %s: no cards at result %d; it will be continued next run",
                segment_key,
                first_result,
            )
            return False

        card_descriptions = read_sds_cards(results_page)
        page_links = tuple(
            card_description["href"] for card_description in card_descriptions
        )
        if page_links == previous_page_links:
            download_logger.warning(
                "[SDS page] segment %s: result %d shows the same cards as the previous page, stopping this segment",
                segment_key,
                first_result,
            )
            break
        previous_page_links = page_links
        us_english_urls = get_us_english_urls_from_cards(card_descriptions)
        download_logger.info(
            "[SDS page] segment %s: page %d of %d: %d card(s), %d US English",
            segment_key,
            first_result // SDS_RESULTS_PER_PAGE + 1,
            (reachable_results + SDS_RESULTS_PER_PAGE - 1) // SDS_RESULTS_PER_PAGE,
            len(card_descriptions),
            len(us_english_urls),
        )
        if us_english_urls:
            download_us_english_urls(
                browser_session, download_database, us_english_urls, run_statistics
            )

        first_result += SDS_RESULTS_PER_PAGE
        download_database.save_sds_segment(
            segment_key, total_results, first_result, False
        )
        if first_result < reachable_results:
            pause_while_acting_like_a_human(
                results_page,
                *PAUSE_SECONDS_RANGE_BETWEEN_RESULT_PAGES,
                "before the next result page",
            )

    download_database.save_sds_segment(segment_key, total_results, first_result, True)
    download_logger.info("[SDS page] segment %s finished", segment_key)
    return True


def process_sds_segment(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
    use_locale_clause: bool,
    range_start: Optional[date],
    range_end: Optional[date],
    known_total: Optional[int] = None,
) -> bool:  # Split the segment if it is too big, otherwise walk its pages; True when finished
    segment_key = build_sds_segment_key(use_locale_clause, range_start, range_end)
    saved_state = download_database.load_sds_segment(segment_key)
    if saved_state is not None and saved_state[2]:
        download_logger.info(
            "[SDS page] segment %s was finished earlier, skipping", segment_key
        )
        return True
    total_results = (
        known_total
        if known_total is not None
        else probe_sds_segment_total(
            browser_session,
            download_database,
            run_statistics,
            use_locale_clause,
            range_start,
            range_end,
        )
    )
    if total_results is None:
        return False
    if total_results == 0:
        download_database.save_sds_segment(segment_key, 0, 0, True)
        return True

    if (
        total_results > SDS_MAXIMUM_REACHABLE_RESULTS
    ):  # Too many results to reach with page numbers alone
        halves = split_date_range(range_start, range_end)
        if halves is not None:
            (first_start, first_end), (second_start, second_end) = halves
            first_total = probe_sds_segment_total(
                browser_session,
                download_database,
                run_statistics,
                use_locale_clause,
                first_start,
                first_end,
            )
            second_total = probe_sds_segment_total(
                browser_session,
                download_database,
                run_statistics,
                use_locale_clause,
                second_start,
                second_end,
            )
            if first_total is None or second_total is None:
                return False
            combined_total = first_total + second_total
            if (
                combined_total > total_results or combined_total == 0
            ):  # The site ignores the date part of the query
                download_logger.warning(
                    "[SDS page] segment %s: the date filter does not seem to work (%d + %d vs %d); only the first %d results of it can be reached",
                    segment_key,
                    first_total,
                    second_total,
                    total_results,
                    SDS_MAXIMUM_REACHABLE_RESULTS,
                )
            else:
                download_logger.info(
                    "[SDS page] segment %s has %d results, splitting it into %s..%s (%d) and %s..%s (%d)",
                    segment_key,
                    total_results,
                    first_start,
                    first_end,
                    first_total,
                    second_start,
                    second_end,
                    second_total,
                )
                first_finished = process_sds_segment(
                    browser_session,
                    download_database,
                    run_statistics,
                    use_locale_clause,
                    first_start,
                    first_end,
                    first_total,
                )
                second_finished = process_sds_segment(
                    browser_session,
                    download_database,
                    run_statistics,
                    use_locale_clause,
                    second_start,
                    second_end,
                    second_total,
                )
                if first_finished and second_finished:
                    download_database.save_sds_segment(
                        segment_key, total_results, 0, True
                    )
                return first_finished and second_finished
        else:
            download_logger.warning(
                "[SDS page] segment %s cannot be cut any more; only the first %d results of it can be reached",
                segment_key,
                SDS_MAXIMUM_REACHABLE_RESULTS,
            )

    saved_state = download_database.load_sds_segment(segment_key)
    first_result = (
        saved_state[1] if saved_state is not None else 0
    )  # Continue where an earlier run stopped
    return walk_sds_segment(
        browser_session,
        download_database,
        run_statistics,
        use_locale_clause,
        range_start,
        range_end,
        total_results,
        first_result,
    )


def download_all_us_english_sds(
    browser_session: "BrowserSession",
    download_database: DownloadDatabase,
    run_statistics: RunStatistics,
) -> None:  # Safety Data Sheets: download the "English - United States" documents of the whole page
    browser_session.close_unneeded_tabs()
    browser_session.internet_monitor.raise_error_if_internet_is_down()
    download_logger.info(
        "Safety Data Sheets: US English documents from %s", SDS_BASE_URL
    )
    use_locale_clause = choose_sds_query_mode(browser_session, run_statistics)
    if use_locale_clause is None:
        download_logger.error(
            "[SDS page] could not open the page with results, giving up on Safety Data Sheets"
        )
        return
    pause_while_acting_like_a_human(
        browser_session.active_results_page,
        *ACTIVITY_SECONDS_RANGE_AFTER_OPENING_SEARCH_PAGE,
        "after opening the SDS page",
    )
    all_segments_finished = process_sds_segment(
        browser_session,
        download_database,
        run_statistics,
        use_locale_clause,
        None,
        None,
    )
    if all_segments_finished:
        download_logger.info(
            "[SDS page] Safety Data Sheets finished: every segment is complete"
        )
    else:
        download_logger.warning(
            "[SDS page] Safety Data Sheets is not complete: some pages could not be read; run the script again to continue"
        )
    browser_session.close_unneeded_tabs()


# ============================================================== INTERNET MONITOR


def is_host_responding(
    host_address: str,
) -> bool:  # Check whether one address answers us
    if shutil.which("ping"):
        if sys.platform.startswith("win"):
            ping_command = [
                "ping",
                "-n",
                "1",
                "-w",
                str(PING_TIMEOUT_SECONDS * 1000),
                host_address,
            ]
        elif sys.platform.startswith("linux"):
            ping_command = [
                "ping",
                "-c",
                "1",
                "-W",
                str(PING_TIMEOUT_SECONDS),
                host_address,
            ]
        else:
            ping_command = ["ping", "-c", "1", host_address]
        try:
            ping_result = subprocess.run(
                ping_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PING_TIMEOUT_SECONDS + 2,
                check=False,
            )
            if ping_result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:  # Some networks block ping, so also try a plain connection
        with socket.create_connection(
            (host_address, FALLBACK_CONNECTION_TEST_PORT),
            timeout=FALLBACK_CONNECTION_TEST_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        return False


async def check_one_host_in_background(host_address: str) -> bool:
    return await asyncio.to_thread(is_host_responding, host_address)


async def check_all_hosts_at_once() -> bool:
    host_responded_flags = await asyncio.gather(
        *(
            check_one_host_in_background(host_address)
            for host_address in INTERNET_CHECK_HOSTS
        )
    )
    return any(host_responded_flags)


def check_internet_connection() -> bool:  # The internet works when at least one address answers
    # Playwright's sync API may already own an event loop in the main thread, so the async check
    # always runs in its own short-lived thread, where asyncio.run() is safe.
    with ThreadPoolExecutor(max_workers=1) as single_worker_pool:
        return single_worker_pool.submit(
            asyncio.run, check_all_hosts_at_once()
        ).result()


class InternetMonitor:
    """Watches the internet in a background thread and flags the browser for a restart after an outage."""

    def __init__(self):
        self.internet_available_event = (
            threading.Event()
        )  # Set while the internet works
        self.stop_requested_event = threading.Event()
        self.internet_state_lock = threading.Lock()
        self.outage_callback = None
        self.internet_outage_occurred = (
            False  # True when the browser must be restarted because of an outage
        )
        self.monitor_thread = None

    def start_monitoring(self, outage_callback) -> None:
        self.outage_callback = outage_callback
        if check_internet_connection():
            self.internet_available_event.set()
            download_logger.info("Internet check: the internet is working")
        else:
            download_logger.warning(
                "Internet check: no answer yet, waiting for the internet"
            )
        self.monitor_thread = threading.Thread(
            target=self._keep_checking_internet, name="internet-monitor", daemon=True
        )
        self.monitor_thread.start()

    def stop_monitoring(self) -> None:
        self.stop_requested_event.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=15)

    def _keep_checking_internet(self) -> None:
        while not self.stop_requested_event.is_set():
            self.update_internet_status(check_internet_connection())
            self.stop_requested_event.wait(INTERNET_CHECK_INTERVAL_SECONDS)

    def update_internet_status(self, internet_is_working: bool) -> None:
        internet_just_went_down = False
        with self.internet_state_lock:
            internet_was_available = self.internet_available_event.is_set()
            if internet_is_working and not internet_was_available:
                self.internet_available_event.set()
                download_logger.warning("INTERNET IS BACK")
            elif not internet_is_working and internet_was_available:
                self.internet_available_event.clear()
                self.internet_outage_occurred = True
                internet_just_went_down = True
                download_logger.error("INTERNET IS DOWN")
        if internet_just_went_down and self.outage_callback:
            try:
                self.outage_callback()
            except Exception:
                download_logger.exception("Could not handle the internet outage")

    def confirm_internet_is_down(
        self,
    ) -> bool:  # Check right now whether the internet is down
        internet_is_working = check_internet_connection()
        self.update_internet_status(internet_is_working)
        return not internet_is_working

    def raise_error_if_internet_is_down(
        self,
    ) -> None:  # Cheap check used before every request
        if not self.internet_available_event.is_set():
            raise InternetOutageError("The internet is down")

    def read_and_clear_outage_flag(self) -> bool:
        with self.internet_state_lock:
            outage_had_occurred, self.internet_outage_occurred = (
                self.internet_outage_occurred,
                False,
            )
            return outage_had_occurred

    def wait_until_internet_available(self) -> None:
        waited_seconds = 0
        while not self.internet_available_event.wait(
            timeout=INTERNET_CHECK_INTERVAL_SECONDS
        ):
            waited_seconds += INTERNET_CHECK_INTERVAL_SECONDS
            download_logger.info(
                "Still waiting for the internet (%.0f seconds so far)", waited_seconds
            )
        if waited_seconds:
            download_logger.info(
                "The internet is back after about %.0f seconds", waited_seconds
            )


# ============================================================== BROWSER SESSION

ERROR_PHRASES_MEANING_BROWSER_IS_CLOSED = (
    "has been closed",
    "target closed",
    "browser closed",
    "connection closed",
    "browser has disconnected",
)


def is_browser_closed_error(browser_error: Exception) -> bool:
    error_text = str(browser_error).lower()
    return any(
        closed_phrase in error_text
        for closed_phrase in ERROR_PHRASES_MEANING_BROWSER_IS_CLOSED
    )


class BrowserSession:
    """Owns the browser and both tabs; can restart everything if the browser closes or crashes."""

    def __init__(self, internet_monitor: InternetMonitor):
        self.internet_monitor = internet_monitor
        self.download_page: Optional[
            Page
        ] = None  # The temporary download tab, only set while it is open
        self.is_download_tab_being_opened = False
        self.invisible_playwright_launcher: Optional[InvisiblePlaywright] = None
        self.connected_browser: Any = None
        self.browser_context: Any = None
        self.results_page: Optional[Page] = None
        self.browser_restart_count = 0

    @property
    def active_results_page(self) -> Page:
        if self.results_page is None:
            raise RuntimeError("The browser is not running, so there is no results tab")
        return self.results_page

    def start_browser(self) -> None:
        download_logger.info(
            "Starting the browser through invisible_playwright at %dx%d",
            BROWSER_WINDOW_WIDTH_PIXELS,
            BROWSER_WINDOW_HEIGHT_PIXELS,
        )
        self.invisible_playwright_launcher = InvisiblePlaywright(
            extra_prefs={"pdfjs.disabled": True}
        )  # Without this Firefox shows PDFs in its viewer instead of downloading them
        self.connected_browser = self.invisible_playwright_launcher.__enter__()
        self.browser_context = self.connected_browser.new_context(
            viewport={
                "width": BROWSER_WINDOW_WIDTH_PIXELS,
                "height": BROWSER_WINDOW_HEIGHT_PIXELS,
            },
            screen={
                "width": BROWSER_WINDOW_WIDTH_PIXELS,
                "height": BROWSER_WINDOW_HEIGHT_PIXELS,
            },
        )
        self.browser_context.set_default_timeout(PAGE_LOAD_TIMEOUT_MILLISECONDS)
        self.results_page = self.browser_context.new_page()
        attach_browser_event_logging(self.results_page)
        self.browser_context.on("page", self._close_unexpected_new_tab)
        self.results_page.bring_to_front()
        reset_mouse_position()
        self.results_page.mouse.move(current_mouse_x, current_mouse_y)

    @staticmethod
    def _close_tab_ignoring_errors(tab_to_close: Page) -> None:
        try:
            tab_to_close.close()
        except PlaywrightError:
            pass

    def _close_unexpected_new_tab(
        self, new_tab: Page
    ) -> None:  # Close tabs that nobody asked for
        if self.is_download_tab_being_opened or new_tab in (
            self.results_page,
            self.download_page,
        ):
            return
        download_logger.info("Closing an unexpected new tab: %s", new_tab.url)
        self._close_tab_ignoring_errors(new_tab)

    def _list_all_browser_contexts(
        self,
    ) -> list:  # Every browser context, so tabs in stray windows are found too
        try:
            all_browser_contexts = list(self.connected_browser.contexts)
        except (PlaywrightError, AttributeError):
            all_browser_contexts = []
        if (
            self.browser_context is not None
            and self.browser_context not in all_browser_contexts
        ):
            all_browser_contexts.append(self.browser_context)
        return all_browser_contexts

    def _close_tabs_except(self, tabs_to_keep: tuple) -> None:
        for browser_context in self._list_all_browser_contexts():
            try:
                open_tabs = list(browser_context.pages)
            except PlaywrightError:
                continue
            for open_tab in open_tabs:
                if open_tab not in tabs_to_keep:
                    download_logger.info("Closing an unneeded tab: %s", open_tab.url)
                    self._close_tab_ignoring_errors(open_tab)

    def close_unneeded_tabs(
        self,
    ) -> None:  # Keep only the results tab (and the download tab while it is in use)
        self._close_tabs_except((self.results_page, self.download_page))

    def close_all_tabs(self) -> None:
        self._close_tabs_except(())

    def handle_internet_outage(
        self,
    ) -> None:  # Called by the monitor thread when the internet goes down
        # Playwright's sync API must only be used from the thread that started it, so this thread
        # cannot close the browser. The main thread notices the outage at its next step
        # (raise_error_if_internet_is_down) and restart_browser() closes the old browser.
        download_logger.error(
            "The internet is down: the browser will be closed and restarted by the main thread"
        )

    @contextmanager
    def temporary_download_tab(
        self,
    ) -> Iterator[Page]:  # Open a second tab only while downloads need it
        download_logger.info("Opening a temporary second tab for downloading")
        self.is_download_tab_being_opened = True
        try:
            try:
                with self.browser_context.expect_page(
                    timeout=PAGE_LOAD_TIMEOUT_MILLISECONDS
                ) as new_tab_event_info:
                    self.active_results_page.evaluate(
                        "() => { window.open('about:blank', '_blank'); }"
                    )
                new_download_page = new_tab_event_info.value
            except PlaywrightTimeoutError:  # Probably blocked as a popup
                download_logger.warning(
                    "The tab did not open from the page, opening it directly instead"
                )
                new_download_page = self.browser_context.new_page()
            self.active_results_page.bring_to_front()  # Downloads work from a background tab
        finally:
            self.is_download_tab_being_opened = False

        self.download_page = new_download_page
        attach_browser_event_logging(new_download_page)
        try:
            yield new_download_page
        finally:
            self.download_page = None
            download_logger.info("Closing the temporary download tab")
            self._close_tab_ignoring_errors(new_download_page)
            try:
                self.close_unneeded_tabs()
                self.active_results_page.bring_to_front()
            except PlaywrightError:
                pass

    def stop_browser(self) -> None:
        if self.invisible_playwright_launcher is not None:
            try:
                self.close_all_tabs()
                if self.browser_context is not None:
                    self.browser_context.close()
            except PlaywrightError:
                pass
            try:
                self.invisible_playwright_launcher.__exit__(None, None, None)
            except Exception as closing_error:
                download_logger.debug(
                    "Ignoring error while closing the browser: %s", closing_error
                )
        self.invisible_playwright_launcher = None
        self.connected_browser = None
        self.browser_context = None
        self.results_page = None
        self.download_page = None

    def restart_browser(
        self,
    ) -> None:  # Start a fresh browser after the old one closed or crashed
        if self.internet_monitor.read_and_clear_outage_flag():
            download_logger.info(
                "This restart is due to an internet outage, so it does not count towards the limit"
            )
        else:
            self.browser_restart_count += 1
            if self.browser_restart_count > MAXIMUM_BROWSER_RESTARTS:
                raise RuntimeError(
                    f"The browser closed more than {MAXIMUM_BROWSER_RESTARTS} times, giving up"
                )
        download_logger.error(
            "Browser problem detected. Restarting (%d/%d)",
            self.browser_restart_count,
            MAXIMUM_BROWSER_RESTARTS,
        )
        self.stop_browser()
        self.internet_monitor.wait_until_internet_available()
        time.sleep(3)
        self.start_browser()
        warm_up_session_on_home_page(self.active_results_page)
        self.close_unneeded_tabs()


def run_step_and_restart_browser_if_needed(
    browser_session: BrowserSession, step_description: str, step_to_run
):
    """Run a step, transparently restarting the browser and retrying after a crash or internet outage."""
    while True:
        try:
            return step_to_run()
        except InternetOutageError as outage_error:
            download_logger.warning(
                "Internet outage during %s: %s", step_description, outage_error
            )
            browser_session.restart_browser()
        except PlaywrightError as browser_error:
            if is_browser_closed_error(browser_error):
                download_logger.error(
                    "Browser closed during %s: %s",
                    step_description,
                    str(browser_error).splitlines()[0],
                )
                browser_session.restart_browser()
            elif browser_session.internet_monitor.confirm_internet_is_down():
                download_logger.warning(
                    "The internet is down, so the error during %s is expected",
                    step_description,
                )
                browser_session.restart_browser()
            else:
                download_logger.exception(
                    "Browser error during %s, skipping this step", step_description
                )
                return None


# ============================================================== MAIN PROGRAM


def search_keywords_and_return_failed_ones(
    browser_session: BrowserSession,
    download_database: DownloadDatabase,
    keywords_to_search: list,
    run_statistics: RunStatistics,
) -> list:  # Search a list of keywords and return the ones that failed
    failed_keywords = []
    for keyword_position, search_keyword in enumerate(keywords_to_search, start=1):
        if keyword_position > 1:
            pause_while_acting_like_a_human(
                browser_session.active_results_page,
                *PAUSE_SECONDS_RANGE_BETWEEN_KEYWORDS,
                "before the next keyword",
            )
        keyword_was_searched = run_step_and_restart_browser_if_needed(
            browser_session,
            f"keyword '{search_keyword}'",
            lambda keyword_to_search=search_keyword: search_one_keyword_and_download_pdfs(
                browser_session, download_database, keyword_to_search, run_statistics
            ),
        )
        if keyword_was_searched is not True:
            failed_keywords.append(search_keyword)
    return failed_keywords


def parse_command_line_arguments() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(
        description="Agilent PDF downloader. Jobs run in this order: Chemical Standards, then Safety Data Sheets."
    )
    argument_parser.add_argument(
        "--start-from",
        choices=(JOB_CHEMICAL_STANDARDS, JOB_SAFETY_DATA_SHEETS),
        default=JOB_CHEMICAL_STANDARDS,
        help=(
            "Which job to start from. 'chemical-standards' (the default) runs Chemical Standards and then "
            "Safety Data Sheets. 'safety-data-sheets' skips Chemical Standards and runs Safety Data Sheets only."
        ),
    )
    return argument_parser.parse_args()


def run_agilent_downloader(
    start_from: str = JOB_CHEMICAL_STANDARDS,
) -> None:  # Run the whole job from start to finish
    configure_console_logging()
    validate_settings()
    run_chemical_standards = (
        start_from == JOB_CHEMICAL_STANDARDS
    )  # Safety Data Sheets always runs; it is the last job
    download_logger.info(
        "Starting from: %s | jobs to run: %s",
        start_from,
        "Chemical Standards, then Safety Data Sheets"
        if run_chemical_standards
        else "Safety Data Sheets",
    )
    download_logger.info("Download folder: %s", DOWNLOAD_FOLDER)

    DOWNLOAD_FOLDER.mkdir(exist_ok=True)
    download_database = DownloadDatabase(DATABASE_FILE_PATH)
    download_database.register_pdf_files_already_on_disk(DOWNLOAD_FOLDER)
    download_database.ignore_urls_in_unwanted_formats()

    run_statistics = RunStatistics()

    try:
        internet_monitor = InternetMonitor()
        browser_session = BrowserSession(internet_monitor)
        try:
            internet_monitor.start_monitoring(browser_session.handle_internet_outage)
            internet_monitor.wait_until_internet_available()
            browser_session.start_browser()
            if not warm_up_session_on_home_page(browser_session.active_results_page):
                download_logger.warning(
                    "Warm-up did not finish cleanly, continuing anyway"
                )
            browser_session.close_unneeded_tabs()

            # ---------------- CHEMICAL STANDARDS: keyword search on the chemical standards pages
            if run_chemical_standards:
                download_logger.info("Keywords: %s", " ".join(SEARCH_KEYWORDS))
                still_failed_keywords: list = []
                failed_keywords = search_keywords_and_return_failed_ones(
                    browser_session, download_database, SEARCH_KEYWORDS, run_statistics
                )
                if failed_keywords:
                    download_logger.info(
                        "Retrying %d keyword(s) that failed: %s",
                        len(failed_keywords),
                        " ".join(failed_keywords),
                    )
                    still_failed_keywords = search_keywords_and_return_failed_ones(
                        browser_session,
                        download_database,
                        failed_keywords,
                        run_statistics,
                    )
                    if still_failed_keywords:
                        download_logger.warning(
                            "Keywords that still failed: %s",
                            " ".join(still_failed_keywords),
                        )

                download_logger.info(
                    "All keywords searched: downloading the best available version of every remaining document"
                )
                run_step_and_restart_browser_if_needed(
                    browser_session,
                    "the final download of the best remaining versions",
                    lambda: download_documents_using_temporary_tab(
                        browser_session,
                        download_database,
                        select_documents_for_final_download(
                            download_database, bool(still_failed_keywords)
                        ),
                        run_statistics,
                    ),
                )
                log_run_statistics(run_statistics, "CHEMICAL STANDARDS FINISHED")

            # ---------------- SAFETY DATA SHEETS: every "English - United States" document
            run_step_and_restart_browser_if_needed(
                browser_session,
                "the US English Safety Data Sheets pages",
                lambda: download_all_us_english_sds(
                    browser_session, download_database, run_statistics
                ),
            )
            log_run_statistics(run_statistics, "SAFETY DATA SHEETS FINISHED")
        finally:
            internet_monitor.stop_monitoring()
            browser_session.close_all_tabs()
            browser_session.stop_browser()
    except AccessBlockedError as blocked_error:
        download_logger.error("STOPPED EARLY: %s", blocked_error)
    except KeyboardInterrupt:
        download_logger.warning(
            "Stopped by the user; progress is saved and the next run continues from here"
        )
    except Exception:
        download_logger.exception("Unexpected error, stopping")
        raise
    finally:
        log_run_statistics(run_statistics, "FINAL")
        download_database.close_database()
        download_logger.info("Database closed, finished")


if __name__ == "__main__":
    command_line_arguments = parse_command_line_arguments()
    run_agilent_downloader(command_line_arguments.start_from)
