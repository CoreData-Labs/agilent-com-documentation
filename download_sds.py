#!/usr/bin/env python3
# Download all USA English SDS / MSDS PDFs from Agilent's chemical-standards search page.  # Purpose of the script
# The browser is started through invisible_playwright (Playwright on an anti-detect Firefox).  # Browser used
# 1. Start the browser at 1920x1080 and open the first tab (the results tab).  # Step 1
# 2. Open the home page, browse it for about 30 seconds, then open 3 other pages for about 30 seconds each.  # Step 2
# 3. Search every keyword a-z and 0-9 (all letters and digits of the site's search).  # Step 3
# 4. After every result page, download the PDFs found there using a temporary second tab.  # Step 4
# 5. A SQLite database remembers every URL and file name so nothing is downloaded twice.  # Step 5
# 6. If the browser crashes or closes, it is restarted and the work continues.  # Step 6
# 7. "Page may no longer exist" pages are marked as done and never retried.  # Step 7
# 8. Random pauses, exponential back-off and a stop-after-N-blocks rule keep the traffic gentle.  # Step 8
# 9. A background thread checks the internet; the main thread restarts the browser after an outage.  # Step 9
# 10. Only USA English files (names ending in _NAEnglish.pdf) are downloaded.  # Step 10
# 11. Only the results tab stays open; the download tab exists only while downloading.  # Step 11
# 12. Everything is logged to the console at DEBUG level.  # Step 12

import logging  # Prints log lines to the console
import math  # Distance and curve calculations for human-like mouse movement
import random  # Picks random pause lengths and random actions
import re  # Regular expressions for link matching and text cleaning
import shutil  # Finds the "ping" program on the system PATH
import socket  # Opens a plain TCP connection when ping is unavailable
import sqlite3  # Built-in database used to remember what is already downloaded
import string  # Supplies the lowercase letters and digits used as search keywords
import subprocess  # Runs "ping" as an external process
import sys  # Gives access to stdout and the current platform name
import threading  # Runs the internet check in the background
import time  # Provides sleep() and the current time
from contextlib import contextmanager  # Turns a method into a usable "with" block
from pathlib import Path  # Object-oriented filesystem paths
from typing import Iterator, Optional  # Type hints for generators and optional values
from urllib.parse import unquote, urlparse  # Helpers to read a file name out of a URL

from invisible_playwright import (
    InvisiblePlaywright,
)  # Playwright on an anti-detect Firefox

from invisible_playwright._pw.sync_api import (  # Its bundled Playwright classes (errors only match this copy)
    Dialog,  # Type of alert/confirm popups
    Download,  # Type of a browser download
    Page,  # Type of a browser tab
    Error as PlaywrightError,  # Base error class raised by Playwright
    TimeoutError as PlaywrightTimeoutError,  # Error raised when Playwright waits too long
)  # End of the import list

# ============================================================== SETTINGS

SEARCH_URL_TEMPLATE = "https://www.agilent.com/en/product/chemical-standards/search-results?tabname=related_part_number&keyword={search_keyword}"  # Search URL with a keyword placeholder
SEARCH_KEYWORDS = list(
    string.ascii_lowercase + string.digits
)  # Every keyword: letters a-z then digits 0-9
HOME_PAGE_URL = "https://www.agilent.com/"  # Page visited first, like a normal visitor
HOME_PAGE_BROWSING_SECONDS = (
    30  # Seconds of mouse and scrolling activity on the home page
)
OTHER_PAGES_TO_VISIT = 3  # How many different pages are opened from the home page
OTHER_PAGE_BROWSING_SECONDS = 30  # Seconds spent on each of those pages
REQUIRED_FILE_NAME_ENDING = (
    "_NAEnglish.pdf"  # Only files whose name ends like this are downloaded
)

VIEWPORT_WIDTH_PIXELS = 1920  # Browser window width (full HD)
VIEWPORT_HEIGHT_PIXELS = 1080  # Browser window height (full HD)

DOWNLOAD_FOLDER = Path(
    "PDFs"
).resolve()  # Absolute path of the folder where PDFs are saved
DATABASE_FILE_PATH = Path(
    "download_history.sqlite3"
).resolve()  # Absolute path of the SQLite file
MAXIMUM_FILE_NAME_STEM_LENGTH = 150  # Longest allowed file name, not counting ".pdf"
SQL_PLACEHOLDER_CHUNK_SIZE = (
    500  # Maximum number of "?" placeholders used in one SQL query
)

MAXIMUM_RESULT_PAGES_PER_KEYWORD = (
    200  # Safety limit on result pages followed per keyword
)
NAVIGATION_TIMEOUT_MS = 30_000  # How long to wait for a page to open
DOWNLOAD_TIMEOUT_MS = 60_000  # How long to wait for the browser to start one download

PAUSE_BETWEEN_RESULT_PAGES = (
    2.0,
    5.0,
)  # Random pause range (seconds) before the next result page
PAUSE_BETWEEN_DOWNLOADS = (
    1.0,
    3.0,
)  # Random pause range (seconds) between two downloads
PAUSE_BETWEEN_KEYWORDS = (
    5.0,
    10.0,
)  # Random pause range (seconds) before the next keyword
ACTIVITY_AFTER_OPENING_SEARCH_PAGE = (
    3.0,
    6.0,
)  # Random activity time (seconds) after a search page opens
MAX_ACCESS_DENIED_ATTEMPTS = (
    3  # How many times to retry a page that shows "Access Denied"
)
ACCESS_DENIED_BACKOFF_SECONDS = (
    30  # First back-off wait; doubles on each further attempt
)
MAX_CONSECUTIVE_BLOCKS = 5  # Stop the whole run after this many blocks in a row
MAX_BROWSER_RESTARTS = (
    5  # How many times the browser may be restarted after closing or crashing
)

INTERNET_CHECK_HOSTS = (
    "1.1.1.1",
    "8.8.8.8",
)  # Addresses pinged to check whether the internet works
INTERNET_CHECK_INTERVAL_SECONDS = 10  # How often the background internet check runs
PING_TIMEOUT_SECONDS = 5  # How long to wait for one ping answer
TCP_FALLBACK_PORT = (
    53  # Port used for a plain connection test when ping is unavailable (DNS port)
)
TCP_FALLBACK_TIMEOUT_SECONDS = 2  # How long to wait for that fallback connection test

download_logger = logging.getLogger(
    "sds_downloader"
)  # Logger used by every function in this script


class AccessBlockedError(
    Exception
):  # Raised when the site keeps blocking us and we should stop
    """The website kept blocking us; the whole run should stop."""  # Explanation of the error


class InternetOutageError(
    Exception
):  # Raised when the internet is down in the middle of a step
    """The internet is down; the current step should be retried once it is back."""  # Explanation of the error


# ============================================================== LOGGING


def configure_console_logging() -> None:  # Send every log message to the console at DEBUG level
    console_handler = logging.StreamHandler(
        sys.stdout
    )  # Create a handler that writes to standard output
    console_handler.setLevel(
        logging.DEBUG
    )  # The handler lets every level through, including DEBUG
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )  # Choose the line format
    download_logger.setLevel(
        logging.DEBUG
    )  # The logger level is DEBUG, the most detailed level
    if (
        not download_logger.handlers
    ):  # Only attach a handler once, so lines are never printed twice
        download_logger.addHandler(
            console_handler
        )  # Attach the console handler to our logger
    download_logger.propagate = (
        False  # Prevent every line from being printed twice by the root logger
    )


def attach_browser_event_logging(
    browser_page: Page,
) -> None:  # Connect logging to a single browser tab
    """Wire up console/error/request/response/navigation/download/dialog logging for one tab."""  # Explanation
    browser_page.on(
        "console",
        lambda console_message: download_logger.debug(
            "BROWSER CONSOLE [%s] %s", console_message.type, console_message.text
        ),
    )  # Log the website's own console output
    browser_page.on(
        "pageerror",
        lambda page_error: download_logger.error("BROWSER PAGE ERROR %s", page_error),
    )  # Log JavaScript errors raised by the website
    browser_page.on(
        "requestfailed",
        lambda failed_request: download_logger.warning(
            "REQUEST FAILED %s %s -> %s",
            failed_request.method,
            failed_request.url,
            failed_request.failure,
        ),
    )  # Log failed network requests
    browser_page.on(
        "response",
        lambda http_response: download_logger.warning(
            "HTTP %s %s", http_response.status, http_response.url
        )
        if http_response.status >= 400
        else None,
    )  # Log HTTP error responses
    browser_page.on(
        "framenavigated",
        lambda navigated_frame: download_logger.info(
            "NAVIGATED to %s", navigated_frame.url
        )
        if navigated_frame.parent_frame is None
        else None,
    )  # Log top-level page changes
    browser_page.on(
        "download",
        lambda started_download: download_logger.info(
            "BROWSER DOWNLOAD STARTED url=%s suggested_name=%s",
            started_download.url,
            started_download.suggested_filename,
        ),
    )  # Log every download the browser starts

    def handle_dialog_popup(
        dialog_popup: Dialog,
    ) -> None:  # Deal with alert/confirm popups so they cannot block us
        download_logger.warning(
            "BROWSER DIALOG [%s] %s", dialog_popup.type, dialog_popup.message
        )  # Log what the popup said
        dialog_popup.dismiss()  # Close the popup

    browser_page.on(
        "dialog", handle_dialog_popup
    )  # Attach the dialog handler to this tab


def log_run_statistics(
    run_statistics: dict, label_text: str
) -> None:  # Print the running totals
    download_logger.info(
        "%s: downloaded %d | skipped %d | page unavailable %d | failed %d",
        label_text,
        run_statistics["downloaded"],
        run_statistics["skipped"],
        run_statistics["unavailable"],
        run_statistics["failed"],
    )  # Write one summary line


# ============================================================== DATABASE
#
# Two tables:
#   collected_urls   -- every PDF URL ever seen, and whether it has been handled
#   downloaded_files -- every file name actually saved, so duplicate names are caught too


class DownloadDatabase:  # Wraps the SQLite connection and every query this script needs
    def __init__(self, database_path: Path):  # Open (or create) the database file
        download_logger.info(
            "Opening database: %s", database_path
        )  # Tell the user which database file is used
        self.database_connection = sqlite3.connect(
            database_path
        )  # Connect to the database file on disk
        with self.database_connection:  # Commit automatically when this block finishes
            self.database_connection.execute(
                "CREATE TABLE IF NOT EXISTS collected_urls (source_url TEXT PRIMARY KEY, is_processed INTEGER NOT NULL DEFAULT 0)"
            )  # One row per URL, with a done flag
            self.database_connection.execute(
                "CREATE TABLE IF NOT EXISTS downloaded_files (file_name TEXT PRIMARY KEY COLLATE NOCASE, source_url TEXT NOT NULL, downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )  # One row per saved file name

    def close_database(self) -> None:  # Close the database connection
        self.database_connection.close()  # Release the file handle

    def register_existing_pdf_files(
        self, pdf_folder: Path
    ) -> None:  # Remember PDFs already on disk
        """Record PDFs already on disk so they are never downloaded again."""  # Explanation
        newly_registered_count = (
            0  # Counts how many existing files were newly registered
        )
        with self.database_connection:  # Commit automatically when this block finishes
            for pdf_path in pdf_folder.glob(
                "*.pdf"
            ):  # Look at every PDF already in the folder
                insert_cursor = self.database_connection.execute(
                    "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",
                    (pdf_path.name, "found on disk before first run"),
                )  # Insert the file name unless it is already recorded
                newly_registered_count += (
                    insert_cursor.rowcount
                )  # Add 1 when a new row was really inserted
        download_logger.info(
            "Registered %d existing PDF file(s) from %s",
            newly_registered_count,
            pdf_folder,
        )  # Report the result

    def ignore_non_usa_english_urls(
        self,
    ) -> None:  # Drop old pending URLs we no longer want
        """Mark pending URLs that fail the language filter as done (e.g. after a settings change)."""  # Explanation
        pending_urls = self.load_unprocessed_urls()  # Every URL that is still pending
        ignored_urls = [
            pending_url
            for pending_url in pending_urls
            if not is_usa_english_pdf_url(pending_url)
        ]  # Keep only the ones that fail the filter
        for ignored_url in ignored_urls:  # Go through each URL that should be ignored
            self.mark_url_processed(
                ignored_url
            )  # Mark it as done so it is never downloaded
        download_logger.info(
            "Ignored %d pending URL(s) that do not end in %s",
            len(ignored_urls),
            REQUIRED_FILE_NAME_ENDING,
        )  # Report the result

    def save_new_urls(self, found_urls: set) -> None:  # Store newly found URLs
        with self.database_connection:  # Commit automatically when this block finishes
            insert_cursor = self.database_connection.executemany(
                "INSERT OR IGNORE INTO collected_urls (source_url) VALUES (?)",
                [(found_url,) for found_url in found_urls],
            )  # Insert many URLs, ignoring known ones
        download_logger.info(
            "Saved %d new URL(s) (%d were already known)",
            insert_cursor.rowcount,
            len(found_urls) - insert_cursor.rowcount,
        )  # Report the result

    def load_unprocessed_urls(self) -> list:  # Get every URL that still needs work
        result_rows = self.database_connection.execute(
            "SELECT source_url FROM collected_urls WHERE is_processed = 0 ORDER BY source_url"
        )  # Ask the database for the pending URLs
        return [
            result_row[0] for result_row in result_rows.fetchall()
        ]  # Return just the URL text of each row

    def filter_unprocessed(
        self, candidate_urls: list
    ) -> list:  # Keep only the URLs from this list that are still pending
        pending_urls = set()  # Collects the URLs the database says are pending
        for chunk_start in range(
            0, len(candidate_urls), SQL_PLACEHOLDER_CHUNK_SIZE
        ):  # Work in chunks so SQLite's variable limit is never exceeded
            url_chunk = candidate_urls[
                chunk_start : chunk_start + SQL_PLACEHOLDER_CHUNK_SIZE
            ]  # The URLs of this chunk
            placeholder_text = ",".join(
                "?" for chunk_url in url_chunk
            )  # One "?" per URL for the SQL query
            result_rows = self.database_connection.execute(
                f"SELECT source_url FROM collected_urls WHERE is_processed = 0 AND source_url IN ({placeholder_text})",
                url_chunk,
            )  # Ask which of these URLs are pending
            pending_urls.update(
                result_row[0] for result_row in result_rows.fetchall()
            )  # Remember the pending ones
        return [
            candidate_url
            for candidate_url in candidate_urls
            if candidate_url in pending_urls
        ]  # Keep the candidates in their original order

    def mark_url_processed(self, source_url: str) -> None:  # Mark one URL as done
        with self.database_connection:  # Commit automatically when this block finishes
            self.database_connection.execute(
                "UPDATE collected_urls SET is_processed = 1 WHERE source_url = ?",
                (source_url,),
            )  # Flip the done flag

    def is_file_name_downloaded(
        self, file_name: str
    ) -> bool:  # Check whether a file name was already saved
        matching_row = self.database_connection.execute(
            "SELECT 1 FROM downloaded_files WHERE file_name = ?", (file_name,)
        ).fetchone()  # Look the name up
        return matching_row is not None  # True when a matching row exists

    def record_downloaded_file(
        self, file_name: str, source_url: str
    ) -> None:  # Save a record of a downloaded file
        with self.database_connection:  # Commit automatically when this block finishes
            self.database_connection.execute(
                "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",
                (file_name, source_url),
            )  # Insert a row describing the saved file

    def is_url_already_downloaded_by_name(
        self, source_url: str
    ) -> bool:  # Cheap pre-check before downloading
        """If the URL's own file name is already known or on disk, there is no need to download it."""  # Explanation
        guessed_file_name = guess_file_name_from_url(
            source_url
        )  # Predict the file name from the URL itself
        if guessed_file_name is None:  # The URL does not reveal a trustworthy name
            return False  # We cannot tell, so assume it still needs downloading
        safe_file_name = build_safe_file_name(
            guessed_file_name
        )  # Apply the naming rules to the guess
        return (
            self.is_file_name_downloaded(safe_file_name)
            or (DOWNLOAD_FOLDER / safe_file_name).exists()
        )  # Known in the database or already on disk


# ============================================================== FILE NAMES


def guess_file_name_from_url(
    source_url: str,
) -> Optional[str]:  # Predict the file name before downloading
    last_path_segment = Path(
        unquote(urlparse(source_url).path)
    ).name  # Take the last part of the URL's path, decoded
    return (
        last_path_segment if last_path_segment.lower().endswith(".pdf") else None
    )  # Only trust names that really end in .pdf


def is_usa_english_pdf_url(
    source_url: str,
) -> bool:  # Check whether a URL points to a USA English file
    return (
        unquote(urlparse(source_url).path)
        .lower()
        .endswith(REQUIRED_FILE_NAME_ENDING.lower())
    )  # True when the path ends in _NAEnglish.pdf


def build_safe_file_name(
    raw_file_name: str,
) -> str:  # Clean a file name to follow the naming rules
    file_name_stem = (
        raw_file_name[:-4] if raw_file_name.lower().endswith(".pdf") else raw_file_name
    )  # Strip ".pdf" before cleaning
    file_name_stem = re.sub(
        r"[^a-z0-9_]+", "_", file_name_stem.lower()
    )  # Lowercase, then replace anything but a-z/0-9/_ with _
    file_name_stem = re.sub(r"_+", "_", file_name_stem)[
        :MAXIMUM_FILE_NAME_STEM_LENGTH
    ].strip(
        "_"
    )  # Collapse underscores, cut to length, trim the ends
    return (
        file_name_stem or "document"
    ) + ".pdf"  # Fall back to "document" if nothing is left, then add .pdf again


# ============================================================== PACING AND BLOCK TRACKING


def register_access_block(
    run_statistics: dict,
) -> None:  # Count one more "Access Denied" in a row
    run_statistics["consecutive_blocks"] += 1  # Add one to the blocks-in-a-row counter
    download_logger.warning(
        "Consecutive access blocks: %d/%d",
        run_statistics["consecutive_blocks"],
        MAX_CONSECUTIVE_BLOCKS,
    )  # Record the current count
    if (
        run_statistics["consecutive_blocks"] >= MAX_CONSECUTIVE_BLOCKS
    ):  # Check whether the limit is reached
        raise AccessBlockedError(
            f"The website blocked {MAX_CONSECUTIVE_BLOCKS} requests in a row. Stopping so the block does not get worse. Try again later or contact Agilent."
        )  # Stop the whole run


def reset_access_blocks(
    run_statistics: dict,
) -> None:  # A page opened fine, so the blocks-in-a-row count restarts
    run_statistics["consecutive_blocks"] = 0  # Set the counter back to zero


# ============================================================== PAGE CHECKS & ACTIONS

PAGE_UNAVAILABLE_MESSAGE = "the page may be temporarily unavailable or may no longer exist"  # Text of the error page


def is_access_denied_page(
    browser_page: Page,
) -> bool:  # Check whether the website blocked this visit
    try:  # Reading the page can fail while it is changing
        page_title, page_source = (
            browser_page.title(),
            browser_page.content(),
        )  # Read the page title and source
    except PlaywrightError:  # The page could not be read
        return False  # Assume the page is fine
    lowercase_source = (
        page_source.lower()
    )  # Lowercase copy so the checks ignore capitals
    is_blocked = (  # Decide whether this is the "Access Denied" block page
        "access denied" in page_title.lower()  # The title says Access Denied
        or "errors.edgesuite.net"
        in lowercase_source  # The page links to the Akamai error site
        or "you don't have permission to access"
        in lowercase_source  # The usual block message
    )  # End of the check
    if is_blocked:  # The website blocked us
        reference_match = re.search(
            r"Reference #([0-9a-fA-F.]+)", page_source
        )  # Look for the reference number
        download_logger.warning(
            "ACCESS DENIED page detected (reference %s) at %s",
            reference_match.group(1) if reference_match else "unknown",
            browser_page.url,
        )  # Record it
    return is_blocked  # Tell the caller whether we were blocked


def is_page_unavailable_page(
    browser_page: Page,
) -> bool:  # Check for the "page may no longer exist" error page
    try:  # Reading the page can fail while it is changing
        visible_text = browser_page.inner_text(
            "body"
        )  # Read the text a person sees (markup cannot split the sentence)
    except PlaywrightError:  # The page could not be read
        return False  # Assume the page is fine
    normalized_text = re.sub(
        r"\s+", " ", visible_text.lower()
    )  # Lowercase and squeeze all whitespace to one space
    message_found = (
        PAGE_UNAVAILABLE_MESSAGE in normalized_text
    )  # Look for the known error message
    if message_found:  # The page is the error page
        download_logger.warning(
            "PAGE UNAVAILABLE message found at %s", browser_page.url
        )  # Record it
    return message_found  # Tell the caller what we found


def is_results_page_usable(
    browser_page: Page, run_statistics: dict
) -> bool:  # Check a result page before reading it
    if is_page_unavailable_page(
        browser_page
    ):  # Check for the "page may no longer exist" message
        return False  # Skip this page
    if is_access_denied_page(browser_page):  # Check for the "Access Denied" page
        register_access_block(
            run_statistics
        )  # Count the block, which may stop the whole run
        return False  # Skip this page
    reset_access_blocks(
        run_statistics
    )  # The page is fine, so the blocks-in-a-row count restarts
    return True  # The page can be read


def wait_for_network_to_settle(
    browser_page: Page,
) -> None:  # Wait until the page stops loading data
    try:  # Waiting can time out on pages that never stop loading
        browser_page.wait_for_load_state(
            "networkidle", timeout=20_000
        )  # Wait up to 20 seconds for a quiet network
    except PlaywrightTimeoutError:  # The page kept loading data
        pass  # Continue anyway
    time.sleep(2)  # Give the page's JavaScript a little extra time to draw the results


def dismiss_cookie_banner(
    browser_page: Page,
) -> None:  # Close the cookie popup if it shows up
    for accept_selector in (
        "#onetrust-accept-btn-handler",
        "button[id*='accept' i]",
    ):  # Known accept buttons
        try:  # Ignore any problem finding or clicking the button
            accept_button = browser_page.locator(
                accept_selector
            ).first  # Find the first matching button
            if accept_button.is_visible():  # Only click it if it is visible
                accept_button.click(timeout=3000)  # Accept the cookies
                time.sleep(0.5)  # Give the popup a moment to close
                return  # Stop after the first button that worked
        except PlaywrightError:  # The button was missing or not clickable
            continue  # Try the next selector


def scroll_to_bottom_of_page(
    browser_page: Page, pause_seconds: float = 0.8
) -> None:  # Load lazy content by scrolling down
    previous_page_height = 0  # Remember the page height from the previous round
    for scroll_round in range(15):  # Scroll at most 15 times
        browser_page.evaluate(
            "window.scrollTo(0, document.documentElement.scrollHeight)"
        )  # Jump to the page bottom
        time.sleep(pause_seconds)  # Wait for more content to load
        current_page_height = browser_page.evaluate(
            "document.documentElement.scrollHeight"
        )  # Measure the page again
        if (
            current_page_height == previous_page_height
        ):  # Check whether the page stopped growing
            break  # Everything is loaded, so stop scrolling
        previous_page_height = (
            current_page_height  # Remember this height for the next comparison
        )


PDF_OR_SDS_LINK_PATTERN = re.compile(
    r"\.pdf(\?|#|$)|msds|/sds|sds[_/\-.]|safety[-_ ]?data", re.IGNORECASE
)  # Matches links that look like PDFs/SDS
RAW_PDF_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE
)  # Matches raw .pdf URLs hidden in page source


def extract_pdf_urls_from_page(
    browser_page: Page,
) -> set:  # Find every USA English PDF URL on the page
    found_urls = set()  # Collects the URLs found on this page
    for link_address in browser_page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map(linkElement => linkElement.href)"
    ):  # Read the address of every link on the page
        if link_address.startswith("http") and PDF_OR_SDS_LINK_PATTERN.search(
            link_address
        ):  # Looks like a PDF or SDS link
            found_urls.add(link_address)  # Keep it
    page_source_text = browser_page.content().replace(
        "\\/", "/"
    )  # Page source with JSON-escaped slashes turned back into normal ones
    for raw_url in RAW_PDF_URL_PATTERN.findall(
        page_source_text
    ):  # Search the raw page source too
        found_urls.add(
            raw_url.replace("\\u0026", "&").replace("&amp;", "&")
        )  # Keep the URL after decoding escaped ampersands
    usa_english_urls = {
        found_url for found_url in found_urls if is_usa_english_pdf_url(found_url)
    }  # Keep USA English links only
    download_logger.debug(
        "Found %d PDF link(s), %d after the USA English filter",
        len(found_urls),
        len(usa_english_urls),
    )  # Record the counts
    return usa_english_urls  # Give only the USA English URLs back


NEXT_PAGE_BUTTON_SELECTORS = [  # Different ways the "next page" button might be found
    "button[aria-label*='next' i]",  # Buttons whose label mentions "next"
    "a[aria-label*='next' i]",  # Links whose label mentions "next"
    "li.next:not(.disabled) a",  # A link inside an enabled "next" list item
    "a.next",  # A link with a "next" class
    "button.next",  # A button with a "next" class
    "[class*='pagination'] [class*='next']",  # A "next" element inside pagination
    "xpath=//button[normalize-space()='Next' or normalize-space()='>']",  # Button with text Next or >
    "xpath=//a[normalize-space()='Next' or normalize-space()='>']",  # Link with text Next or >
]  # End of the selector list


def click_next_page_button(
    browser_page: Page,
) -> bool:  # Go to the next result page if possible
    for (
        next_button_selector
    ) in NEXT_PAGE_BUTTON_SELECTORS:  # Try each known way of finding the button
        matching_locator = browser_page.locator(
            next_button_selector
        )  # Find every element matching this selector
        try:  # Counting can fail if the page changes at the wrong moment
            match_count = matching_locator.count()  # Count the matches
        except PlaywrightError:  # The selector could not be used
            continue  # Try the next selector
        for match_position in range(match_count):  # Check each candidate by position
            candidate_button = matching_locator.nth(
                match_position
            )  # Pick the candidate
            try:  # The element may go stale or be covered
                if (
                    not candidate_button.is_visible()
                    or not candidate_button.is_enabled()
                ):  # Hidden or disabled
                    continue  # Skip this candidate
                if candidate_button.get_attribute(
                    "aria-disabled"
                ) == "true" or "disabled" in (
                    candidate_button.get_attribute("class") or ""
                ):  # Other disabled markers
                    continue  # Skip this candidate
                human_click_element(
                    browser_page, candidate_button
                )  # Move the pointer to the button like a person and click it
                download_logger.info(
                    "Clicked next page button (%s)", next_button_selector
                )  # Record the click
                return True  # Report that we moved to another page
            except PlaywrightError:  # The click did not work
                continue  # Try the next candidate
    download_logger.info(
        "No usable next page button found"
    )  # Record that the last page was reached
    return False  # No usable next page button was found


# ============================================================== HUMAN-LIKE BEHAVIOUR
#
# Three parts:
#   1. Links and URLs     -- which links a casual visitor might click
#   2. Human-like input   -- curved mouse paths, wheel scrolling, random key presses, clicking,
#                            and pausing with activity instead of sitting still
#   3. The routine        -- home page first, then three other pages, then scraping starts
#
# Random letters are only pressed, never submitted: Enter and other special keys are never used,
# and any focused text box is un-focused first, so no new search is ever started.

# ---------------------------------------------------------------- 1. LINKS AND URLS

EXCLUDED_BROWSING_LINK_PATTERN = re.compile(  # Pattern for links a casual visitor would not click
    r"log-?in|log-?out|sign-?in|sign-?out|register|cart|checkout|account"  # Login, logout, sign-in, registration, shopping and account pages
    r"|\.pdf|msds|/sds|mailto:|tel:|javascript:",  # Documents, e-mail, phone and script links
    re.IGNORECASE,  # Ignore capital letters
)  # End of the pattern
LINK_CANDIDATES_SCRIPT = (  # JavaScript that describes every link on the page, in page order
    "() => Array.from(document.querySelectorAll('a[href]')).map((linkElement, linkIndex) => {"  # Go through every link and remember its position
    "const linkBox = linkElement.getBoundingClientRect();"  # Size and position of the link on screen
    "const linkStyle = getComputedStyle(linkElement);"  # How the link is displayed
    "return {"  # Start the description of this link
    "index: linkIndex,"  # Position of the link among all links
    "href: linkElement.href,"  # Full address of the link
    "visible: linkBox.width > 20 && linkBox.height > 10 && linkStyle.visibility !== 'hidden' && linkStyle.display !== 'none',"  # Is it big enough and shown
    "new_tab: linkElement.target === '_blank',"  # Does it open a new tab
    "download: linkElement.hasAttribute('download'),"  # Is it a download link
    "};"  # End of the description of this link
    "})"  # End of the loop over all links
)  # End of the script
UNFOCUS_TEXT_BOXES_SCRIPT = (  # JavaScript that takes the keyboard focus away from any text box
    "() => { const focusedElement = document.activeElement;"  # The element that currently has the focus
    " if (focusedElement && focusedElement !== document.body && (['INPUT', 'TEXTAREA', 'SELECT'].includes(focusedElement.tagName) || focusedElement.isContentEditable)) focusedElement.blur(); }"  # If it is a text box, take the focus away
)  # End of the script


def without_fragment(page_url: str) -> str:  # Drop the "#section" part of a URL
    return page_url.split("#")[0]  # Everything before the first "#"


def pick_random_internal_link(
    browser_page: Page, visited_urls: set
) -> Optional[tuple]:  # Choose a random visible Agilent link we have not used
    candidate_links = []  # Collects (position on the page, address) pairs
    for link_description in browser_page.evaluate(
        LINK_CANDIDATES_SCRIPT
    ):  # Look at every link on the page
        link_address = link_description["href"]  # The full address of the link
        parsed_address = urlparse(link_address)  # Split the address into parts
        host_name = parsed_address.hostname or ""  # The website name, empty if missing
        if (
            not link_description["visible"]
            or link_description["new_tab"]
            or link_description["download"]
        ):  # Hidden, popup or download link
            continue  # A normal visitor would not click it here
        if parsed_address.scheme not in ("http", "https"):  # Not a normal web address
            continue  # Skip it
        if host_name != "agilent.com" and not host_name.endswith(
            ".agilent.com"
        ):  # Not an Agilent page (so "evilagilent.com" is rejected)
            continue  # Stay on Agilent's own website
        if EXCLUDED_BROWSING_LINK_PATTERN.search(
            link_address
        ):  # Login, cart, PDFs and similar
            continue  # Skip it
        if (
            without_fragment(link_address) in visited_urls
        ):  # Already visited, or the page we are on
            continue  # We want different pages
        candidate_links.append(
            (link_description["index"], link_address)
        )  # This link is a good candidate
    return (
        random.choice(candidate_links) if candidate_links else None
    )  # One at random, if any


# ---------------------------------------------------------------- 2. HUMAN-LIKE INPUT

mouse_position = {
    "x": 200.0,
    "y": 200.0,
}  # Where the pointer is now (Playwright cannot tell us, so we remember it)


def get_viewport_size(
    browser_page: Page,
) -> tuple:  # Width and height of the visible page area
    viewport_size = browser_page.viewport_size or {
        "width": VIEWPORT_WIDTH_PIXELS,
        "height": VIEWPORT_HEIGHT_PIXELS,
    }  # Fall back to full HD
    return viewport_size["width"], viewport_size["height"]  # Give both numbers back


def human_mouse_move(
    browser_page: Page, target_x: float, target_y: float
) -> None:  # Move the pointer along a curved, uneven path
    viewport_width, viewport_height = get_viewport_size(
        browser_page
    )  # Size of the page area
    target_x = min(
        max(target_x, 1), viewport_width - 1
    )  # Keep the target inside the page horizontally
    target_y = min(
        max(target_y, 1), viewport_height - 1
    )  # Keep the target inside the page vertically
    start_x, start_y = (
        mouse_position["x"],
        mouse_position["y"],
    )  # Where the pointer is now
    travel_distance = math.hypot(
        target_x - start_x, target_y - start_y
    )  # Straight-line distance to travel
    if travel_distance < 2:  # Already there
        return  # Nothing to do
    movement_step_count = max(
        12, min(60, int(travel_distance / 10))
    )  # Longer moves use more small steps
    curve_bend_amount = travel_distance * random.uniform(
        0.1, 0.3
    )  # How much the path curves
    control_point_x = (start_x + target_x) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()  # Curve control point, pulled sideways
    control_point_y = (start_y + target_y) / 2 + random.choice(
        (-1, 1)
    ) * curve_bend_amount * random.random()  # Curve control point, pulled up or down
    for step_number in range(1, movement_step_count + 1):  # Walk along the curve
        progress_fraction = (
            step_number / movement_step_count
        )  # How far along the path we are (0 to 1)
        eased_progress = (
            progress_fraction * progress_fraction * (3 - 2 * progress_fraction)
        )  # Start slowly, speed up in the middle, slow down at the end
        curve_point_x = (
            (1 - eased_progress) ** 2 * start_x
            + 2 * (1 - eased_progress) * eased_progress * control_point_x
            + eased_progress**2 * target_x
        )  # Point on the curve, horizontal
        curve_point_y = (
            (1 - eased_progress) ** 2 * start_y
            + 2 * (1 - eased_progress) * eased_progress * control_point_y
            + eased_progress**2 * target_y
        )  # Point on the curve, vertical
        if step_number < movement_step_count:  # Every step except the last one
            curve_point_x += random.uniform(-1.0, 1.0)  # Tiny hand shake, horizontal
            curve_point_y += random.uniform(-1.0, 1.0)  # Tiny hand shake, vertical
        browser_page.mouse.move(
            curve_point_x, curve_point_y
        )  # Move the pointer to this point
        time.sleep(random.uniform(0.004, 0.014))  # Tiny pause between steps
    mouse_position["x"], mouse_position["y"] = (
        target_x,
        target_y,
    )  # Remember where the pointer ended up


def human_click_element(
    browser_page: Page, target_element
) -> None:  # Move the pointer to an element like a person and click it
    target_element.scroll_into_view_if_needed(
        timeout=5000
    )  # Bring the element into view
    element_box = (
        target_element.bounding_box()
    )  # Position and size of the element on screen
    if element_box is None:  # The element has no size right now
        target_element.click(timeout=5000)  # Fall back to a normal click
        return  # Done
    click_x = element_box["x"] + element_box["width"] * random.uniform(
        0.3, 0.7
    )  # A spot inside the element, horizontal
    click_y = element_box["y"] + element_box["height"] * random.uniform(
        0.3, 0.7
    )  # A spot inside the element, vertical
    human_mouse_move(
        browser_page, click_x, click_y
    )  # Move the pointer there like a person
    time.sleep(random.uniform(0.2, 0.6))  # Short hesitation before clicking
    browser_page.mouse.click(
        mouse_position["x"], mouse_position["y"], delay=random.randint(50, 150)
    )  # Click, holding the button briefly


def human_scroll(
    browser_page: Page, scroll_pixels: int
) -> None:  # Scroll with the mouse wheel in small, uneven notches
    scroll_direction = (
        1 if scroll_pixels > 0 else -1
    )  # Down is positive, up is negative
    remaining_pixels = abs(scroll_pixels)  # How much is still left to scroll
    while remaining_pixels > 0:  # Keep scrolling until we have covered the distance
        notch_pixels = min(
            remaining_pixels, random.randint(40, 120)
        )  # Size of this wheel notch
        browser_page.mouse.wheel(
            0, scroll_direction * notch_pixels
        )  # Turn the wheel a little
        remaining_pixels -= notch_pixels  # Count it off
        time.sleep(random.uniform(0.02, 0.09))  # Short pause between notches
        if random.random() < 0.08:  # Now and then
            time.sleep(random.uniform(0.3, 0.9))  # Stop to read for a moment


def human_type_random_letters(
    browser_page: Page,
) -> None:  # Press a few random letter keys with human-like timing
    browser_page.evaluate(
        UNFOCUS_TEXT_BOXES_SCRIPT
    )  # No text box may have the focus, so the letters cannot change a search box
    for key_press_number in range(random.randint(2, 6)):  # A few key presses
        random_letter = random.choice(
            string.ascii_lowercase
        )  # Pick a random letter a-z (never Enter or other special keys)
        browser_page.keyboard.press(
            random_letter, delay=random.randint(40, 140)
        )  # Press and release it, holding the key briefly
        time.sleep(random.uniform(0.08, 0.35))  # Typing rhythm between keys
        if random.random() < 0.15:  # Sometimes
            time.sleep(random.uniform(0.4, 1.0))  # Hesitate like someone thinking


def act_like_a_person_on_page(
    browser_page: Page, duration_seconds: float
) -> None:  # Move the mouse, scroll and press random letters for a while
    end_time = time.time() + duration_seconds  # Moment when we stop
    viewport_width, viewport_height = get_viewport_size(
        browser_page
    )  # Size of the page area
    while time.time() < end_time:  # Keep going until the time is up
        try:  # Browser actions can fail while the page is changing
            action_roll = random.random()  # Pick what to do next
            if action_roll < 0.2:  # Sometimes
                human_type_random_letters(browser_page)  # Press a few random letters
            elif action_roll < 0.55:  # Mostly read the page
                human_scroll(browser_page, random.randint(200, 650))  # Scroll down
            elif action_roll < 0.9:  # Often move the mouse somewhere else
                human_mouse_move(
                    browser_page,
                    random.uniform(40, viewport_width - 40),
                    random.uniform(40, viewport_height - 40),
                )  # Move to a random spot
            else:  # Occasionally look back up
                human_scroll(
                    browser_page, -random.randint(100, 300)
                )  # Scroll up a little
        except PlaywrightError as browser_error:  # The action did not work
            if is_browser_closed_error(browser_error):  # The browser itself is gone
                raise  # Let the caller deal with it
        remaining_seconds = end_time - time.time()  # Time left
        if remaining_seconds > 0:  # Still time to spend
            time.sleep(
                min(remaining_seconds, random.uniform(0.5, 2.0))
            )  # Human-like pause between actions


def pause_while_acting_like_a_person(
    browser_page: Page,
    minimum_seconds: float,
    maximum_seconds: float,
    pause_reason: str,
) -> None:  # Wait a random time while acting like a person
    pause_seconds = random.uniform(
        minimum_seconds, maximum_seconds
    )  # Pick a random length inside the range
    download_logger.debug(
        "Pausing %.1f seconds with mouse and keyboard activity (%s)",
        pause_seconds,
        pause_reason,
    )  # Record the pause and why it happens
    try:  # The browser can fail while we act
        act_like_a_person_on_page(
            browser_page, pause_seconds
        )  # Move the mouse, scroll and press random letters
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # The browser had a problem
        download_logger.debug(
            "Activity during the pause failed: %s", str(browser_error).splitlines()[0]
        )  # Record it
        time.sleep(
            pause_seconds
        )  # Just wait instead, the next step deals with the browser


# ---------------------------------------------------------------- 3. THE BROWSING ROUTINE


def open_random_link(
    browser_page: Page, visited_urls: set
) -> Optional[str]:  # Click a random link and return the address of the page it opens
    for attempt_number in range(5):  # Try a few different links if one does not work
        chosen_candidate = pick_random_internal_link(
            browser_page, visited_urls
        )  # Choose a link
        if chosen_candidate is None:  # No suitable link on this page
            return None  # Give up
        (
            link_position,
            link_address,
        ) = chosen_candidate  # Position and address of the link
        visited_urls.add(without_fragment(link_address))  # Never choose this one again
        address_before_click = without_fragment(browser_page.url)  # Where we are now
        try:  # Clicking can fail
            chosen_link = browser_page.locator("a[href]").nth(
                link_position
            )  # Find the link by its position
            human_click_element(
                browser_page, chosen_link
            )  # Move the pointer to it like a person and click it
            browser_page.wait_for_url(
                lambda new_address: without_fragment(new_address)
                != address_before_click,
                wait_until="domcontentloaded",
                timeout=8000,
            )  # Wait until we really moved to another page
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as browser_error:  # The click did not lead anywhere
            if is_browser_closed_error(browser_error):  # The browser itself is gone
                raise  # Stop browsing
            download_logger.debug(
                "Browsing: link %s did not open a page", link_address
            )  # Record it
            continue  # Try another link
        return browser_page.url  # We are on a new page
    return None  # None of the links worked


def return_to_home_page(
    browser_page: Page, home_page_address: str
) -> None:  # Go back to the home page like a person would
    try:  # Going back can fail
        browser_page.go_back(
            wait_until="domcontentloaded", timeout=15_000
        )  # Use the back button
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # Back did not work
        if is_browser_closed_error(browser_error):  # The browser itself is gone
            raise  # Stop browsing
    if without_fragment(browser_page.url) != without_fragment(
        home_page_address
    ):  # Not on the home page
        browser_page.goto(
            home_page_address, wait_until="domcontentloaded"
        )  # Open it directly instead
    time.sleep(random.uniform(1.0, 2.0))  # Let the home page draw itself


def browse_like_a_normal_user(
    browser_page: Page,
) -> None:  # Move around the home page, then open a few different pages from it
    try:  # Any browser problem just ends the browsing early
        home_page_address = (
            browser_page.url
        )  # The page we start from and keep returning to
        visited_urls = {
            without_fragment(home_page_address),
            without_fragment(HOME_PAGE_URL),
        }  # Pages already seen
        download_logger.info(
            "Browsing: mouse, scrolling and random keys on the home page for about %d seconds",
            HOME_PAGE_BROWSING_SECONDS,
        )  # Record the first step
        act_like_a_person_on_page(
            browser_page, HOME_PAGE_BROWSING_SECONDS
        )  # Home page: mouse, scrolling and random letters
        for visit_number in range(
            1, OTHER_PAGES_TO_VISIT + 1
        ):  # Open the other pages one by one
            visit_start_time = time.time()  # When this visit began
            opened_address = open_random_link(
                browser_page, visited_urls
            )  # Click a random link on the home page
            if opened_address is None:  # No link worked
                download_logger.warning(
                    "Browsing: could not open another page, stopping early"
                )  # Record it
                break  # Stop browsing
            download_logger.info(
                "Browsing: page %d/%d opened (about %d seconds on it): %s",
                visit_number,
                OTHER_PAGES_TO_VISIT,
                OTHER_PAGE_BROWSING_SECONDS,
                opened_address,
            )  # Record the visit
            seconds_left_on_page = max(
                2.0, OTHER_PAGE_BROWSING_SECONDS - (time.time() - visit_start_time)
            )  # Rest of this visit's time
            act_like_a_person_on_page(
                browser_page, seconds_left_on_page
            )  # Other pages: mouse, scrolling and random letters
            if visit_number < OTHER_PAGES_TO_VISIT:  # Another visit follows
                return_to_home_page(
                    browser_page, home_page_address
                )  # Go back to the home page first
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as browser_error:  # Something went wrong in the browser
        download_logger.warning(
            "Browsing like a normal visitor ended early: %s",
            str(browser_error).splitlines()[0],
        )  # Record it, scraping goes on anyway
        return  # Leave the function
    download_logger.info("Finished browsing like a normal visitor")  # Record the end


def warm_up_session_on_home_page(
    browser_page: Page,
) -> bool:  # Open the home page and browse around a little first
    download_logger.info(
        "Warm-up: opening the home page %s", HOME_PAGE_URL
    )  # Record the first step
    try:  # Opening the page can fail
        browser_page.goto(
            HOME_PAGE_URL, wait_until="domcontentloaded"
        )  # Open the home page
    except PlaywrightError as browser_error:  # The page could not be opened
        download_logger.error(
            "Warm-up: could not open the home page: %s", browser_error
        )  # Record the error
        return False  # The warm-up failed
    wait_for_network_to_settle(browser_page)  # Let the home page finish loading
    dismiss_cookie_banner(browser_page)  # Accept the cookie popup like a normal visitor
    if is_access_denied_page(
        browser_page
    ):  # Check whether even the home page is blocked
        download_logger.error(
            "Warm-up: the home page itself shows Access Denied"
        )  # Record the problem
        return False  # The warm-up failed
    browse_like_a_normal_user(
        browser_page
    )  # Move around the home page and open a few different pages
    return True  # The warm-up worked


PAGE_STATUS_OPENED = "opened"  # The page opened normally
PAGE_STATUS_UNAVAILABLE = (
    "unavailable"  # The page shows the "page may no longer exist" message
)
PAGE_STATUS_BLOCKED = "blocked"  # The page kept showing "Access Denied"
PAGE_STATUS_FAILED = "failed"  # The page could not be opened at all


def open_page_politely(
    browser_page: Page, page_url: str, run_statistics: dict
) -> str:  # Open a page, backing off if blocked
    """Open a page, backing off and retrying through the home page if blocked."""  # Explanation
    for attempt_number in range(
        1, MAX_ACCESS_DENIED_ATTEMPTS + 1
    ):  # Try a limited number of times
        download_logger.info(
            "Opening %s (attempt %d/%d)",
            page_url,
            attempt_number,
            MAX_ACCESS_DENIED_ATTEMPTS,
        )  # Record the attempt
        try:  # Opening the page can fail
            browser_page.goto(page_url, wait_until="domcontentloaded")  # Open the page
        except PlaywrightError as browser_error:  # The page could not be opened
            if is_browser_closed_error(browser_error):  # The browser itself is gone
                raise  # Let the restart logic start a new browser and retry this keyword
            download_logger.error(
                "Could not open %s: %s", page_url, browser_error
            )  # Record the error
            return PAGE_STATUS_FAILED  # Give up on this page
        wait_for_network_to_settle(browser_page)  # Wait for the page to finish loading
        if is_page_unavailable_page(
            browser_page
        ):  # Check for the "page may no longer exist" message
            return PAGE_STATUS_UNAVAILABLE  # Skip this page, retrying will not help
        if not is_access_denied_page(
            browser_page
        ):  # Check whether the website let us in
            reset_access_blocks(run_statistics)  # The blocks-in-a-row count restarts
            return PAGE_STATUS_OPENED  # The page opened normally
        register_access_block(
            run_statistics
        )  # Count the block, which may stop the whole run
        if (
            attempt_number < MAX_ACCESS_DENIED_ATTEMPTS
        ):  # Check whether another attempt is allowed
            backoff_seconds = ACCESS_DENIED_BACKOFF_SECONDS * (
                2 ** (attempt_number - 1)
            ) + random.uniform(
                0, 5
            )  # Longer each time
            download_logger.info(
                "Backing off for %.0f seconds, then revisiting the home page",
                backoff_seconds,
            )  # Explain
            time.sleep(backoff_seconds)  # Wait before trying again
            warm_up_session_on_home_page(
                browser_page
            )  # Visit the home page again to refresh the session
    download_logger.error(
        "Still blocked after %d attempt(s): %s", MAX_ACCESS_DENIED_ATTEMPTS, page_url
    )  # Record the failure
    return PAGE_STATUS_BLOCKED  # The page could not be opened


# ============================================================== DOWNLOADING

OUTCOME_DOWNLOAD_STARTED = "download started"  # The browser started downloading a file
OUTCOME_PAGE_UNAVAILABLE = (
    "page unavailable"  # The link led to the "page may no longer exist" message
)
OUTCOME_ACCESS_DENIED = "access denied"  # The link led to the "Access Denied" page
OUTCOME_NOT_A_DOWNLOAD = (
    "not a download"  # The link led to some other web page instead of a file
)
OUTCOME_TIMEOUT = "timeout"  # Nothing happened before the time limit


def start_download(
    browser_page: Page, source_url: str
) -> tuple:  # Ask the browser to download one URL
    """Navigate to a URL and wait for the browser to report a download, or classify what happened instead."""  # Explanation
    received_downloads = []  # Collects the Download objects as the browser reports them

    def collect_started_download(
        started_download: Download,
    ) -> None:  # Handler must be a normal function
        received_downloads.append(started_download)  # Remember the download

    browser_page.on(
        "download", collect_started_download
    )  # Start listening for downloads on this tab
    try:  # Make sure the listener is always removed again
        try:  # Opening a download link can raise an error, which is normal
            navigation_response = browser_page.goto(
                source_url, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS
            )  # Open the link
        except PlaywrightError as navigation_error:  # The browser reported an error for the navigation
            if "Download is starting" in str(
                navigation_error
            ) or "NS_BINDING_ABORTED" in str(
                navigation_error
            ):  # Expected when the link turns into a download
                navigation_response = (
                    None  # The navigation turned straight into a download
                )
            else:  # Some other, real error
                raise  # Let the calling code handle it

        if (
            navigation_response is not None
            and "text/html"
            in navigation_response.headers.get("content-type", "").lower()
        ):  # The server returned a web page
            try:  # Waiting for the page can time out
                browser_page.wait_for_load_state(
                    "domcontentloaded", timeout=15_000
                )  # Let the page finish loading
            except PlaywrightTimeoutError:  # The page took too long
                pass  # Continue with whatever loaded
            if is_page_unavailable_page(
                browser_page
            ):  # Check for the "page may no longer exist" message
                return None, OUTCOME_PAGE_UNAVAILABLE  # No PDF exists at this address
            if is_access_denied_page(
                browser_page
            ):  # Check for the "Access Denied" page
                return None, OUTCOME_ACCESS_DENIED  # The website blocked this request
            return None, OUTCOME_NOT_A_DOWNLOAD  # Some other web page, not a file

        wait_deadline = (
            time.time() + DOWNLOAD_TIMEOUT_MS / 1000
        )  # Moment when we give up waiting
        while (
            not received_downloads and time.time() < wait_deadline
        ):  # Wait for the browser to report the download
            browser_page.wait_for_timeout(
                200
            )  # Wait a moment while Playwright processes browser events
        if received_downloads:  # The browser reported a download
            return (
                received_downloads[0],
                OUTCOME_DOWNLOAD_STARTED,
            )  # Hand back the download
        return None, OUTCOME_TIMEOUT  # Nothing happened in time
    finally:  # Always runs, even after an error
        browser_page.remove_listener(
            "download", collect_started_download
        )  # Stop listening for downloads


def download_pdf_urls(
    download_page: Page,
    database: DownloadDatabase,
    urls_to_download: list,
    run_statistics: dict,
    internet_monitor: "InternetMonitor",
    activity_page: Page,
) -> None:  # Download a batch of URLs
    """Download each URL in turn on the given (temporary) tab; mouse and key activity happens on the results tab."""  # Explanation
    already_contacted_website = (
        False  # Tracks whether we already contacted the website in this batch
    )
    for url_position, source_url in enumerate(
        urls_to_download, start=1
    ):  # Handle each URL one at a time
        download_logger.info(
            "(%d/%d) Processing %s", url_position, len(urls_to_download), source_url
        )  # Record the progress
        internet_monitor.raise_if_internet_down()  # Stop here and wait if the internet is down

        if database.is_url_already_downloaded_by_name(
            source_url
        ):  # Skip when the predicted file already exists
            download_logger.info(
                "  SKIP: the file for this URL already exists"
            )  # Explain the skip
            database.mark_url_processed(source_url)  # Remember this URL is finished
            run_statistics["skipped"] += 1  # Count the skip
            continue  # Move on to the next URL

        if already_contacted_website:  # Only pause between two real requests
            activity_page.bring_to_front()  # Show the results tab, a hidden tab would look odd to the website
            pause_while_acting_like_a_person(
                activity_page, *PAUSE_BETWEEN_DOWNLOADS, "between downloads"
            )  # Move the mouse, scroll and press random letters instead of sitting still
        already_contacted_website = True  # From now on we have contacted the website

        try:  # Starting a download can raise browser errors
            started_download, download_outcome = start_download(
                download_page, source_url
            )  # Let the browser download it
        except PlaywrightError as browser_error:  # A real browser error happened
            if is_browser_closed_error(
                browser_error
            ):  # Check whether the browser itself has closed
                raise  # Let the restart logic deal with it, this URL stays pending
            if (
                internet_monitor.confirm_internet_down()
            ):  # Check whether the internet being down caused the error
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
            download_logger.error(
                "  FAILED to open %s: %s",
                source_url,
                str(browser_error).splitlines()[0],
            )  # Record it
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL

        if (
            download_outcome == OUTCOME_PAGE_UNAVAILABLE
        ):  # The link led to the "page may no longer exist" message
            download_logger.warning(
                "  SKIP: page unavailable, no PDF at %s", source_url
            )  # Explain the skip
            database.mark_url_processed(source_url)  # Never try this URL again
            run_statistics["unavailable"] += 1  # Count it as unavailable
            reset_access_blocks(
                run_statistics
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if (
            download_outcome == OUTCOME_ACCESS_DENIED
        ):  # The website blocked this request
            download_logger.error(
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
            download_logger.warning(
                "  SKIP: the link is a web page, not a PDF: %s", source_url
            )  # Explain the skip
            database.mark_url_processed(source_url)  # Never try this URL again
            run_statistics["skipped"] += 1  # Count it as skipped
            reset_access_blocks(
                run_statistics
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if started_download is None:  # The browser never produced a download
            if (
                internet_monitor.confirm_internet_down()
            ):  # Check whether the internet being down is the reason
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
            download_logger.error(
                "  FAILED: no download (%s) for %s", download_outcome, source_url
            )  # Record the failure
            run_statistics["failed"] += 1  # Count the failure
            continue  # Move on to the next URL
        reset_access_blocks(
            run_statistics
        )  # A download started, so the blocks-in-a-row count restarts

        finish_download(
            started_download, source_url, database, run_statistics
        )  # Validate, name and save the finished download

    log_run_statistics(
        run_statistics, "Totals so far"
    )  # Show the totals after this batch


def finish_download(
    started_download: Download,
    source_url: str,
    database: DownloadDatabase,
    run_statistics: dict,
) -> None:  # Validate and save a completed download
    """Validate, name, de-duplicate and save a download that the browser already started."""  # Explanation
    suggested_file_name = (
        started_download.suggested_filename
    )  # The file name the browser would have used
    download_failure = (
        started_download.failure()
    )  # Wait for the download to end and get any error
    if download_failure:  # The browser reported that the download failed
        download_logger.error(
            "  FAILED: browser download error '%s' for %s", download_failure, source_url
        )  # Log the failure
        run_statistics["failed"] += 1  # Count the failure
        return  # Stop here, nothing more to do

    with open(
        started_download.path(), "rb"
    ) as downloaded_file:  # Open the downloaded file for reading
        file_start_bytes = downloaded_file.read(4)  # Read the first four bytes
    if file_start_bytes != b"%PDF":  # Real PDF files always start with %PDF
        download_logger.error(
            "  FAILED: downloaded file is not a PDF: %s", source_url
        )  # Record the problem
        started_download.delete()  # Throw the file away
        run_statistics["failed"] += 1  # Count the failure
        return  # Stop here, nothing more to do

    safe_file_name = build_safe_file_name(
        suggested_file_name
    )  # Apply the naming rules to the real name
    safe_file_path = (
        DOWNLOAD_FOLDER / safe_file_name
    )  # Full path where the PDF will be saved
    if (
        database.is_file_name_downloaded(safe_file_name) or safe_file_path.exists()
    ):  # Decide whether this file name is already saved
        download_logger.info(
            "  DUPLICATE file name, not saved: %s", safe_file_name
        )  # Explain what happens
        started_download.delete()  # Discard the extra copy
        database.mark_url_processed(source_url)  # Remember this URL is finished
        run_statistics["skipped"] += 1  # Count it as skipped
        return  # Stop here, nothing more to do

    started_download.save_as(safe_file_path)  # Save the PDF into the PDFs folder
    database.record_downloaded_file(
        safe_file_name, source_url
    )  # Save the file name in the database
    database.mark_url_processed(source_url)  # Remember this URL is finished
    run_statistics["downloaded"] += 1  # Count the success
    download_logger.info(
        "  SAVED: %s (%d bytes)", safe_file_path, safe_file_path.stat().st_size
    )  # Record the result


def download_pdf_urls_in_temporary_tab(
    browser_session: "BrowserSession",
    database: DownloadDatabase,
    urls_to_download: list,
    run_statistics: dict,
) -> None:  # Download, opening a tab only if needed
    """Pre-filter already-downloaded URLs, then open the download tab only if something is left to do."""  # Explanation
    urls_still_needed = []  # Collects the URLs whose files are really missing
    for source_url in urls_to_download:  # Check each URL before opening any tab
        if database.is_url_already_downloaded_by_name(
            source_url
        ):  # The file already exists
            download_logger.info(
                "  SKIP: the file for %s already exists", source_url
            )  # Explain the skip
            database.mark_url_processed(source_url)  # Remember this URL is finished
            run_statistics["skipped"] += 1  # Count the skip
        else:  # The file is missing
            urls_still_needed.append(source_url)  # Keep the URL for downloading
    if not urls_still_needed:  # Check whether anything is left to download
        download_logger.info(
            "Nothing to download in this batch, so no second tab is opened"
        )  # Record the decision
        return  # Leave without opening a tab
    browser_session.internet_monitor.raise_if_internet_down()  # Do not open a tab while the internet is down
    with browser_session.temporary_download_tab() as download_page:  # Open the second tab and close it afterwards
        download_pdf_urls(
            download_page,
            database,
            urls_still_needed,
            run_statistics,
            browser_session.internet_monitor,
            browser_session.results_page,
        )  # Download the missing files


# ============================================================== SCRAPING


def collect_and_download_pdf_urls_for_keyword(
    browser_session: "BrowserSession",
    database: DownloadDatabase,
    search_keyword: str,
    run_statistics: dict,
) -> None:  # Scrape one keyword
    results_page = (
        browser_session.results_page
    )  # The tab used for browsing the search results
    browser_session.internet_monitor.raise_if_internet_down()  # Wait for the internet before opening the search page
    search_url = SEARCH_URL_TEMPLATE.format(
        search_keyword=search_keyword
    )  # Build the search address
    download_logger.info(
        "Opening search page for keyword '%s': %s", search_keyword, search_url
    )  # Record it

    page_status = open_page_politely(
        results_page, search_url, run_statistics
    )  # Open the page, backing off if blocked
    if (
        page_status == PAGE_STATUS_FAILED
        and browser_session.internet_monitor.confirm_internet_down()
    ):  # No internet?
        raise InternetOutageError(
            "The internet went down while opening the search page"
        )  # Wait for it, then retry
    if page_status != PAGE_STATUS_OPENED:  # Check whether the page opened normally
        download_logger.warning(
            "[%s] skipping this keyword (page status: %s)", search_keyword, page_status
        )  # Explain the skip
        return  # Nothing was collected, so go on to the next keyword
    dismiss_cookie_banner(results_page)  # Close the cookie popup if present
    pause_while_acting_like_a_person(
        results_page,
        *ACTIVITY_AFTER_OPENING_SEARCH_PAGE,
        "after opening the search page",
    )  # Move the mouse, scroll and press random letters before reading the results

    all_keyword_urls = set()  # Collects the URLs from every result page of this keyword
    for page_number in range(
        1, MAXIMUM_RESULT_PAGES_PER_KEYWORD + 1
    ):  # Walk through the result pages
        browser_session.close_unneeded_tabs()  # Close any tab other than the results and download tabs
        browser_session.internet_monitor.raise_if_internet_down()  # Stop here and wait if the internet is down
        results_page.bring_to_front()  # Keep the results tab in front
        if page_number > 1:  # A next result page has just opened
            pause_while_acting_like_a_person(
                results_page,
                *ACTIVITY_AFTER_OPENING_SEARCH_PAGE,
                "after opening a result page",
            )  # Move the mouse, scroll and press random letters before reading the results
        scroll_to_bottom_of_page(
            results_page
        )  # Make sure all results on this page are loaded

        urls_on_this_page = extract_pdf_urls_from_page(
            results_page
        )  # Read the URLs from this page
        new_urls = urls_on_this_page - all_keyword_urls  # Work out which ones are new
        all_keyword_urls |= (
            urls_on_this_page  # Add this page's URLs to the keyword total
        )
        download_logger.info(
            "[%s] page %d: %d link(s), %d new, %d total",
            search_keyword,
            page_number,
            len(urls_on_this_page),
            len(new_urls),
            len(all_keyword_urls),
        )  # Show progress

        if new_urls:  # Only touch the database when there is something new
            database.save_new_urls(new_urls)  # Store the new URLs in the database
            urls_pending_download = database.filter_unprocessed(
                sorted(new_urls)
            )  # Work out which of them are still pending
            download_logger.info(
                "[%s] page %d: downloading %d PDF(s) right away",
                search_keyword,
                page_number,
                len(urls_pending_download),
            )  # Say what happens next
            download_pdf_urls_in_temporary_tab(
                browser_session, database, urls_pending_download, run_statistics
            )  # Download them now

        if page_number > 1 and not new_urls:  # Check whether paging gave us nothing new
            download_logger.info(
                "[%s] no new links on this page, stopping", search_keyword
            )  # Explain why we stop
            break  # Stop, we are probably seeing the same page again

        pause_while_acting_like_a_person(
            results_page, *PAUSE_BETWEEN_RESULT_PAGES, "before the next result page"
        )  # Move the mouse, scroll and press random letters instead of sitting still
        results_page.bring_to_front()  # Return to the results tab before looking for the next page button
        if not click_next_page_button(results_page):  # Try to go to the next page
            break  # There is no next page, so this keyword is finished
        wait_for_network_to_settle(
            results_page
        )  # Wait for the next page of results to load
        if not is_results_page_usable(
            results_page, run_statistics
        ):  # Check for the error page or "Access Denied"
            download_logger.warning(
                "[%s] the next result page is not usable, moving on", search_keyword
            )  # Explain the skip
            break  # Skip the rest of this keyword and go to the next one

    download_logger.info(
        "[%s] finished with %d PDF link(s)", search_keyword, len(all_keyword_urls)
    )  # Keyword summary


# ============================================================== INTERNET MONITOR


def is_host_responding(
    host_address: str,
) -> bool:  # Check whether one address answers us
    if shutil.which("ping"):  # Check whether the ping program is installed
        if sys.platform.startswith("win"):  # Windows uses different ping options
            ping_command = [
                "ping",
                "-n",
                "1",
                "-w",
                str(PING_TIMEOUT_SECONDS * 1000),
                host_address,
            ]  # One ping, timeout in milliseconds
        elif sys.platform.startswith(
            "linux"
        ):  # Linux uses -W for the timeout in seconds
            ping_command = [
                "ping",
                "-c",
                "1",
                "-W",
                str(PING_TIMEOUT_SECONDS),
                host_address,
            ]  # One ping, timeout in seconds
        else:  # macOS and other systems
            ping_command = [
                "ping",
                "-c",
                "1",
                host_address,
            ]  # One ping, time limit enforced by subprocess.run below
        try:  # Running ping can fail or take too long
            ping_result = subprocess.run(
                ping_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PING_TIMEOUT_SECONDS + 2,
            )  # Run ping quietly
            if ping_result.returncode == 0:  # Exit code 0 means it answered
                return True  # The host responded
        except (
            subprocess.TimeoutExpired,
            OSError,
        ):  # Ping took too long or could not run
            pass  # Fall through to the TCP fallback
    try:  # Some networks block ping, so also try a plain connection
        with socket.create_connection(
            (host_address, TCP_FALLBACK_PORT), timeout=TCP_FALLBACK_TIMEOUT_SECONDS
        ):  # Connect
            return True  # The connection worked, so the host responded
    except OSError:  # The connection did not work
        return False  # The host did not respond


def check_internet_connection() -> bool:  # The internet works when at least one address answers
    return any(
        is_host_responding(host_address) for host_address in INTERNET_CHECK_HOSTS
    )  # One answer is enough


class InternetMonitor:  # Checks the internet in a background thread and tells the session when it goes down
    """Watches the internet in a background thread and flags the browser for restarting on an outage."""  # Explanation

    def __init__(self):  # Prepare the shared state
        self.internet_available_event = (
            threading.Event()
        )  # Set while the internet works
        self.stop_requested_event = (
            threading.Event()
        )  # Set when the monitor should stop
        self.state_lock = (
            threading.Lock()
        )  # Prevents two threads from changing the state at the same time
        self.outage_callback = (
            None  # Function that is called when the internet goes down
        )
        self.outage_occurred = (
            False  # True when the browser must be restarted because of an outage
        )
        self.monitor_thread = None  # The background thread

    def start_monitoring(
        self, outage_callback
    ) -> None:  # Do a first check and start the background thread
        self.outage_callback = outage_callback  # Remember what to call on an outage
        if check_internet_connection():  # First check, done right now
            self.internet_available_event.set()  # The internet works
            download_logger.info("Internet check: the internet is working")  # Record it
        else:  # No answer from either address
            download_logger.warning(
                "Internet check: no answer yet, waiting for the internet"
            )  # Say so
        self.monitor_thread = threading.Thread(
            target=self._run_check_loop, name="internet-monitor", daemon=True
        )  # Create the monitor thread
        self.monitor_thread.start()  # Start checking in the background

    def stop_monitoring(self) -> None:  # Stop the background thread
        self.stop_requested_event.set()  # Ask the thread to finish
        if self.monitor_thread:  # Check whether it was started
            self.monitor_thread.join(timeout=15)  # Wait a little for it to finish

    def _run_check_loop(self) -> None:  # The work done by the background thread
        while (
            not self.stop_requested_event.is_set()
        ):  # Keep going until we are told to stop
            self.update_internet_state(
                check_internet_connection()
            )  # Check the internet and react to any change
            self.stop_requested_event.wait(
                INTERNET_CHECK_INTERVAL_SECONDS
            )  # Sleep, but wake up early when stopping

    def update_internet_state(
        self, internet_works: bool
    ) -> None:  # React when the internet goes down or comes back
        internet_just_went_down = (
            False  # Becomes True when the internet has just gone down
        )
        with self.state_lock:  # Only one thread may change the state at a time
            was_available = (
                self.internet_available_event.is_set()
            )  # What we believed until now
            if internet_works and not was_available:  # The internet came back
                self.internet_available_event.set()  # Record that it works again
                download_logger.warning("INTERNET IS BACK")  # Tell the user
            elif not internet_works and was_available:  # The internet just went down
                self.internet_available_event.clear()  # Record that it does not work
                self.outage_occurred = (
                    True  # Remember that the browser must be restarted afterwards
                )
                internet_just_went_down = True  # The outage callback has to be called
                download_logger.error("INTERNET IS DOWN")  # Tell the user
        if (
            internet_just_went_down and self.outage_callback
        ):  # Call the outage callback outside the lock
            try:  # Calling it can fail
                self.outage_callback()  # Handle the outage
            except Exception:  # Anything unexpected
                download_logger.exception(
                    "Could not handle the internet outage"
                )  # Log the details

    def confirm_internet_down(
        self,
    ) -> bool:  # Check right now whether the internet is down
        internet_works = check_internet_connection()  # Do a live check
        self.update_internet_state(
            internet_works
        )  # Update the shared state and handle the outage if needed
        return not internet_works  # True when the internet is down

    def raise_if_internet_down(self) -> None:  # Cheap check used before every request
        if (
            not self.internet_available_event.is_set()
        ):  # The monitor says the internet is down
            raise InternetOutageError(
                "The internet is down"
            )  # Make the restart logic wait for it

    def consume_outage_flag(
        self,
    ) -> bool:  # Tell whether an outage happened since the last restart
        with self.state_lock:  # Only one thread may change the state at a time
            outage_flag_value, self.outage_occurred = (
                self.outage_occurred,
                False,
            )  # Read the flag and reset it
            return outage_flag_value  # Give the old value back

    def wait_until_internet_available(self) -> None:  # Block until the internet works
        waited_seconds = 0  # Counts how long we have waited
        while not self.internet_available_event.wait(
            timeout=INTERNET_CHECK_INTERVAL_SECONDS
        ):  # Wait for the "works" signal
            waited_seconds += (
                INTERNET_CHECK_INTERVAL_SECONDS  # Add the time we just waited
            )
            download_logger.info(
                "Still waiting for the internet (%.0f seconds so far)", waited_seconds
            )  # Show progress
        if waited_seconds:  # Check whether we really had to wait
            download_logger.info(
                "The internet is back after about %.0f seconds", waited_seconds
            )  # Record it


# ============================================================== BROWSER SESSION

BROWSER_CLOSED_MESSAGES = (
    "has been closed",
    "target closed",
    "browser closed",
    "connection closed",
    "browser has disconnected",
)  # Known "browser is gone" phrases


def is_browser_closed_error(
    browser_error: Exception,
) -> bool:  # Check whether an error means the browser is gone
    error_text = str(
        browser_error
    ).lower()  # Lowercase the error text so the comparison ignores capitals
    return any(
        closed_message in error_text for closed_message in BROWSER_CLOSED_MESSAGES
    )  # True if any known phrase matches


class BrowserSession:  # Owns the browser and both tabs; can restart everything if the browser closes or crashes
    """Owns the browser and both tabs; can restart everything if the browser closes or crashes."""  # Explanation

    def __init__(self, internet_monitor: InternetMonitor):  # Setup
        self.internet_monitor = internet_monitor  # Tells us whether the internet works
        self.download_page: Optional[
            Page
        ] = None  # The temporary download tab, only set while it is open
        self.is_opening_download_tab = (
            False  # True for a moment while we open the download tab ourselves
        )
        self.invisible_playwright_launcher: Optional[
            InvisiblePlaywright
        ] = None  # The launcher, once started
        self.connected_browser = None  # The Playwright Browser object
        self.browser_context = None  # The browser context that owns both tabs
        self.results_page: Optional[
            Page
        ] = None  # Tab used for browsing the search results
        self.restart_count = 0  # How many times the browser had to be restarted

    def start_browser(self) -> None:  # Start the browser and open the results tab
        download_logger.info(
            "Starting the browser through invisible_playwright at %dx%d",
            VIEWPORT_WIDTH_PIXELS,
            VIEWPORT_HEIGHT_PIXELS,
        )  # Record it
        self.invisible_playwright_launcher = InvisiblePlaywright(
            extra_prefs={"pdfjs.disabled": True}
        )  # Without this Firefox shows PDFs in its viewer instead of downloading them
        self.connected_browser = (
            self.invisible_playwright_launcher.__enter__()
        )  # Launch the browser
        self.browser_context = self.connected_browser.new_context(
            viewport={"width": VIEWPORT_WIDTH_PIXELS, "height": VIEWPORT_HEIGHT_PIXELS},
            screen={"width": VIEWPORT_WIDTH_PIXELS, "height": VIEWPORT_HEIGHT_PIXELS},
        )  # The context both tabs live in, with a full HD window
        self.browser_context.set_default_timeout(
            NAVIGATION_TIMEOUT_MS
        )  # Default time limit for page actions in every tab
        self.results_page = self.browser_context.new_page()  # Open the first tab
        attach_browser_event_logging(
            self.results_page
        )  # Start logging everything the results tab does
        self.browser_context.on(
            "page", self._close_unexpected_new_tab
        )  # Close any popup or extra tab the moment it opens
        self.results_page.bring_to_front()  # Show the results tab
        mouse_position["x"], mouse_position["y"] = (
            200.0,
            200.0,
        )  # A fresh browser starts with the pointer at a known place

    @staticmethod  # Does not need access to the session
    def _close_quietly(
        tab_to_close: Page,
    ) -> None:  # Close a tab, ignoring any error if it is already gone
        try:  # Closing can fail if the tab is already gone
            tab_to_close.close()  # Close the tab
        except PlaywrightError:  # The tab or browser was already gone
            pass  # Nothing more to do

    def _close_unexpected_new_tab(
        self, new_tab: Page
    ) -> None:  # Close tabs that nobody asked for
        if self.is_opening_download_tab or new_tab in (
            self.results_page,
            self.download_page,
        ):  # A wanted tab
            return  # Leave it open
        download_logger.info(
            "Closing an unexpected new tab: %s", new_tab.url
        )  # Record which tab is closed
        self._close_quietly(new_tab)  # Close it

    def close_unneeded_tabs(
        self,
    ) -> None:  # Close every tab except the results tab and the download tab
        for open_tab in list(self.browser_context.pages):  # Look at every open tab
            if open_tab not in (
                self.results_page,
                self.download_page,
            ):  # A tab we do not need
                download_logger.info(
                    "Closing an unneeded tab: %s", open_tab.url
                )  # Record which tab is closed
                self._close_quietly(open_tab)  # Close it

    def handle_internet_outage(
        self,
    ) -> None:  # Called by the monitor thread when the internet goes down
        # Playwright's sync API must only be used from the thread that started it, so this thread
        # cannot close the browser. The main thread sees the outage at its next step
        # (raise_if_internet_down) and restart_browser() closes the old browser.
        download_logger.error(
            "The internet is down: the browser will be closed and restarted by the main thread"
        )  # Record it

    @contextmanager  # Turns the method below into something that can be used in a "with" block
    def temporary_download_tab(
        self,
    ) -> Iterator[Page]:  # Open a second tab only while downloads need it
        download_logger.info(
            "Opening a temporary second tab for downloading"
        )  # Record it
        self.is_opening_download_tab = (
            True  # Tell the popup guard that this new tab is wanted
        )
        try:  # Make sure the flag is always reset
            try:  # Opening the tab through the page can be blocked as a popup
                with self.browser_context.expect_page(
                    timeout=NAVIGATION_TIMEOUT_MS
                ) as new_tab_information:  # Wait for the new tab to appear
                    self.results_page.evaluate(
                        "() => { window.open('about:blank', '_blank'); }"
                    )  # Firefox opens this as a tab in the same window
                new_download_page = new_tab_information.value  # The second tab
            except PlaywrightTimeoutError:  # No tab appeared, probably blocked as a popup
                download_logger.warning(
                    "The tab did not open from the page, opening it directly instead"
                )  # Record the fallback
                new_download_page = (
                    self.browser_context.new_page()
                )  # Open the tab directly
            self.results_page.bring_to_front()  # Keep showing the results tab, downloads work from the background tab
        finally:  # Always runs
            self.is_opening_download_tab = (
                False  # The popup guard may close unexpected tabs again
            )

        self.download_page = new_download_page  # Remember it so the guards leave it alone while it is open
        attach_browser_event_logging(
            new_download_page
        )  # Start logging everything the download tab does
        try:  # Make sure the tab is always closed again
            yield new_download_page  # Hand the tab to the code inside the "with" block
        finally:  # Always runs, even after an error
            self.download_page = None  # The download tab is going away
            download_logger.info("Closing the temporary download tab")  # Record it
            try:  # Closing can fail if the browser itself is gone
                new_download_page.close()  # Close the second tab
                self.results_page.bring_to_front()  # Show the results tab again
            except PlaywrightError:  # The tab or browser was already gone
                pass  # Nothing more to do

    def stop_browser(self) -> None:  # Close the browser
        if (
            self.invisible_playwright_launcher is not None
        ):  # Check whether a browser was started
            try:  # The browser may already be dead
                self.invisible_playwright_launcher.__exit__(
                    None, None, None
                )  # Close the browser
            except Exception as closing_error:  # Anything raised by a browser that is already gone
                download_logger.debug(
                    "Ignoring error while closing the browser: %s", closing_error
                )  # Record it
        self.invisible_playwright_launcher = None  # Forget the old launcher
        self.connected_browser = None  # Forget the old browser
        self.browser_context = None  # Forget the old context
        self.results_page = None  # Forget the old tab
        self.download_page = None  # Forget the old download tab

    def restart_browser(
        self,
    ) -> None:  # Start a fresh browser after the old one closed or crashed
        if (
            self.internet_monitor.consume_outage_flag()
        ):  # Check whether this restart is only because the internet was down
            download_logger.info(
                "This restart is due to an internet outage, so it does not count towards the limit"
            )  # Say so
        else:  # The restart has another reason, such as a crash
            self.restart_count += 1  # Count this restart
            if (
                self.restart_count > MAX_BROWSER_RESTARTS
            ):  # Check whether we have restarted too often
                raise RuntimeError(
                    f"The browser closed more than {MAX_BROWSER_RESTARTS} times, giving up"
                )  # Stop the run
        download_logger.error(
            "Browser problem detected. Restarting (%d/%d)",
            self.restart_count,
            MAX_BROWSER_RESTARTS,
        )  # Record it
        self.stop_browser()  # Close whatever is left of the old browser
        self.internet_monitor.wait_until_internet_available()  # Do not start the browser until the internet works
        time.sleep(3)  # Give the system a moment to free resources
        self.start_browser()  # Start a fresh browser with a new results tab
        warm_up_session_on_home_page(
            self.results_page
        )  # Visit the home page again to refresh the session


def run_with_browser_restarts(
    browser_session: BrowserSession, step_description: str, step_function
) -> None:  # Run a step, restarting the browser if needed
    """Run a step, transparently restarting the browser (and retrying) on a crash or internet outage."""  # Explanation
    while True:  # Keep trying until the step finishes or we give up
        try:  # The step may fail if the browser closes
            step_function()  # Run the step
            return  # The step finished, so we are done
        except InternetOutageError as outage_error:  # The internet went down
            download_logger.warning(
                "Internet outage during %s: %s", step_description, outage_error
            )  # Record it
            browser_session.restart_browser()  # Wait for the internet, then start a fresh browser
        except PlaywrightError as browser_error:  # A browser error happened
            if is_browser_closed_error(
                browser_error
            ):  # Check whether the browser or its tab closed
                download_logger.error(
                    "Browser closed during %s: %s",
                    step_description,
                    str(browser_error).splitlines()[0],
                )  # Log it
                browser_session.restart_browser()  # Start a new browser, which raises an error if restarts ran out
            elif (
                browser_session.internet_monitor.confirm_internet_down()
            ):  # Check whether the internet being down is the cause
                download_logger.warning(
                    "The internet is down, so the error during %s is expected",
                    step_description,
                )  # Say so
                browser_session.restart_browser()  # Wait for the internet, then start a fresh browser
            else:  # Some other, real browser error
                download_logger.exception(
                    "Browser error during %s, skipping this step", step_description
                )  # Log the full error
                return  # Go on with the next step instead of crashing the whole run


# ============================================================== MAIN PROGRAM


def run_downloader() -> None:  # Run the whole job from start to finish
    configure_console_logging()  # Turn on console logging first so everything is shown
    download_logger.info(
        "Download folder: %s", DOWNLOAD_FOLDER
    )  # Record where PDFs are saved
    download_logger.info(
        "Keywords: %s", " ".join(SEARCH_KEYWORDS)
    )  # Record which keywords will be searched

    DOWNLOAD_FOLDER.mkdir(exist_ok=True)  # Make sure the PDFs folder exists
    database = DownloadDatabase(DATABASE_FILE_PATH)  # Open the SQLite database
    database.register_existing_pdf_files(
        DOWNLOAD_FOLDER
    )  # Remember PDFs that are already in the folder
    database.ignore_non_usa_english_urls()  # Skip old pending URLs that are not USA English

    run_statistics = {
        "downloaded": 0,
        "skipped": 0,
        "unavailable": 0,
        "failed": 0,
        "consecutive_blocks": 0,
    }  # Running totals for the whole run

    try:  # Make sure the database is closed even if something fails
        internet_monitor = InternetMonitor()  # Prepare the background internet check
        browser_session = BrowserSession(internet_monitor)  # Build the browser session
        try:  # Make sure the monitor and the browser are always stopped at the end
            internet_monitor.start_monitoring(
                browser_session.handle_internet_outage
            )  # Check the internet in the background
            internet_monitor.wait_until_internet_available()  # Do not start the browser while the internet is down
            browser_session.start_browser()  # Start the browser with a single results tab
            if not warm_up_session_on_home_page(
                browser_session.results_page
            ):  # Visit the home page and browse around first
                download_logger.warning(
                    "Warm-up did not finish cleanly, continuing anyway"
                )  # Record the problem

            for keyword_position, search_keyword in enumerate(
                SEARCH_KEYWORDS, start=1
            ):  # Go through a-z and 0-9
                if keyword_position > 1:  # Only pause between two keywords
                    pause_while_acting_like_a_person(
                        browser_session.results_page,
                        *PAUSE_BETWEEN_KEYWORDS,
                        "before the next keyword",
                    )  # Move the mouse, scroll and press random letters instead of sitting still
                run_with_browser_restarts(  # Run the keyword, restarting the browser if it closes
                    browser_session,  # The session that owns the browser
                    f"keyword '{search_keyword}'",  # Description used in log messages
                    lambda current_keyword=search_keyword: collect_and_download_pdf_urls_for_keyword(
                        browser_session, database, current_keyword, run_statistics
                    ),  # The work to do for this keyword
                )  # End of the restart wrapper

            download_logger.info(
                "Retrying URLs that are still pending"
            )  # Record the retry
            run_with_browser_restarts(  # Retry pending URLs, restarting the browser if it closes
                browser_session,  # The session that owns the browser
                "the retry of pending URLs",  # Description used in log messages
                lambda: download_pdf_urls_in_temporary_tab(
                    browser_session,
                    database,
                    database.load_unprocessed_urls(),
                    run_statistics,
                ),  # Re-reads the pending URLs on each attempt
            )  # End of the restart wrapper
        finally:  # Always runs, even after an error
            internet_monitor.stop_monitoring()  # Stop the background internet check
            browser_session.stop_browser()  # Close the browser
    except AccessBlockedError as blocked_error:  # The website blocked us too many times in a row
        download_logger.error(
            "STOPPED EARLY: %s", blocked_error
        )  # Explain why the run ended
    except Exception:  # Anything unexpected that escaped the code above
        download_logger.exception(
            "Unexpected error, stopping"
        )  # Log the full error with its traceback
        raise  # Pass the error on so the script exits with an error code
    finally:  # Always runs at the very end
        log_run_statistics(run_statistics, "FINAL")  # Show the final totals
        database.close_database()  # Close the database
        download_logger.info("Database closed, finished")  # Record the final step


if __name__ == "__main__":  # Only run when this file is executed directly
    run_downloader()  # Start the job
