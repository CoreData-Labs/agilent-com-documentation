#!/usr/bin/env python3  # Run this file with Python 3
"""
Download all SDS / MSDS PDFs from Agilent's chemical-standards search page using
invisible_playwright (Playwright on an anti-detect Firefox).

Overview
--------
1. Starts the browser through invisible_playwright and opens its first tab.
2. Opens the Agilent home page and spends about 30 seconds moving the mouse and scrolling.
   Then opens 3 different pages from the home page, about 30 seconds on each, moving the
   mouse along curved paths, scrolling and pressing random letter keys like a person.
   The same mouse, scrolling and random-letter activity runs on every page it visits,
   including while searching and downloading. Letters are only pressed, never submitted,
   so no new search is started.
3. Only then searches keywords a-z and 0-9.
4. After every result page, downloads the PDFs found there right away, using a
   second tab that's opened only when there's something to download.
5. A SQLite database remembers every URL and file name, so nothing is downloaded twice.
6. If the browser crashes or closes, it's restarted and work continues where it stopped.
7. "Page may no longer exist" error pages are skipped (marked done, never retried).
8. Random pauses, exponential back-off on blocks, and a stop-after-N-blocks rule
   keep the traffic gentle and stop the run if the site is blocking us.
9. A background thread pings 1.1.1.1 / 8.8.8.8. If neither answers, the browser is
   closed (by the main thread at its next step), and only restarted (with scraping
   resumed) once the internet is back.
10. Only USA English files are downloaded (file names ending in _NAEnglish.pdf).
11. Only the results tab stays open permanently; the download tab exists only
    while downloading; any other tab or popup is closed immediately.
12. Everything is logged to the console.
"""

import logging  # Prints log lines to the console
import math  # Distance and curve calculations for human-like mouse movement
import random  # Picks random pause lengths
import re  # Regular expressions for link matching and text cleaning
import shutil  # Finds the "ping" program on the system PATH
import socket  # Opens a plain TCP connection when ping is unavailable
import sqlite3  # Built-in database used to remember what's already downloaded
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

from invisible_playwright._pw.sync_api import (  # invisible_playwright bundles its own Playwright, so its errors are only caught by this copy of the classes
    Dialog,
    Download,
    Page,
    Error as PlaywrightError,  # Base error class raised by Playwright
    TimeoutError as PlaywrightTimeoutError,  # Error raised when Playwright waits too long
)

# ============================================================== SETTINGS

SEARCH_URL_TEMPLATE = (  # Search page URL with a placeholder for the keyword
    "https://www.agilent.com/en/product/chemical-standards/search-results"  # First half of the URL
    "?tabname=related_part_number&keyword={search_keyword}"  # Second half, with the keyword placeholder
)  # End of the template
SEARCH_KEYWORDS = list(string.ascii_lowercase + string.digits)  # Keywords a-z then 0-9
HOME_PAGE_URL = "https://www.agilent.com/"  # Page visited first, like a normal visitor
HOME_PAGE_BROWSING_SECONDS = 30  # How long to move the mouse and scroll on the home page before opening other pages
OTHER_PAGES_TO_VISIT = 3  # How many different pages are opened from the home page
OTHER_PAGE_BROWSING_SECONDS = 30  # How long to stay on each of those pages
REQUIRED_FILE_NAME_ENDING = (
    "_NAEnglish.pdf"  # Only files whose name ends like this are downloaded
)

DOWNLOAD_FOLDER = Path(
    "PDFs"
).resolve()  # Absolute path of the folder where PDFs are saved
DATABASE_FILE_PATH = Path(
    "download_history.sqlite3"
).resolve()  # Absolute path of the SQLite file
MAXIMUM_FILE_NAME_STEM_LENGTH = 150  # Longest allowed file name, not counting ".pdf"

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
)  # Random time range (seconds) of mouse and keyboard activity after a search page opens
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

logger = logging.getLogger(
    "sds_downloader"
)  # Logger used by every function in this script


class AccessBlockedError(
    Exception
):  # Raised when the site keeps blocking us and we should stop
    """The website kept blocking us; the whole run should stop."""


class InternetOutageError(Exception):  # Raised when the internet is down mid-step
    """The internet is down; the current step should be retried once it's back."""


# ============================================================== LOGGING


def configure_console_logging() -> None:  # Send every log message to the console
    handler = logging.StreamHandler(
        sys.stdout
    )  # Create a handler that writes to standard output
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )  # Choose the line format
    logger.setLevel(
        logging.DEBUG
    )  # Show everything, including the most detailed messages
    logger.addHandler(handler)  # Attach the console handler to our logger
    logger.propagate = False  # Prevent every line from being printed twice


def attach_browser_event_logging(
    page: Page,
) -> None:  # Connect logging to a single browser tab
    """Wire up console/error/request/response/navigation/download/dialog logging for a tab."""
    page.on(
        "console", lambda m: logger.debug("BROWSER CONSOLE [%s] %s", m.type, m.text)
    )  # Log the website's own console output
    page.on(
        "pageerror", lambda e: logger.error("BROWSER PAGE ERROR %s", e)
    )  # Log JavaScript errors raised by the website
    page.on(
        "requestfailed",
        lambda r: logger.warning(
            "REQUEST FAILED %s %s -> %s", r.method, r.url, r.failure
        ),
    )  # Log failed network requests
    page.on(
        "response",
        lambda r: logger.warning("HTTP %s %s", r.status, r.url)
        if r.status >= 400
        else None,
    )  # Log HTTP error responses
    page.on(
        "framenavigated",
        lambda f: logger.info("NAVIGATED to %s", f.url)
        if f.parent_frame is None
        else None,
    )  # Log top-level page changes
    page.on(
        "download",
        lambda d: logger.info(
            "BROWSER DOWNLOAD STARTED url=%s suggested_name=%s",
            d.url,
            d.suggested_filename,
        ),
    )  # Log every download the browser starts

    def handle_dialog(
        dialog: Dialog,
    ) -> None:  # Deal with alert/confirm popups so they can't block us
        logger.warning(
            "BROWSER DIALOG [%s] %s", dialog.type, dialog.message
        )  # Log what the popup said
        dialog.dismiss()  # Close the popup

    page.on("dialog", handle_dialog)  # Attach the dialog handler to this tab


def log_run_statistics(stats: dict, label: str) -> None:  # Print the running totals
    logger.info(  # Write one summary line
        "%s: downloaded %d | skipped %d | page unavailable %d | failed %d",  # Message template
        label,
        stats["downloaded"],
        stats["skipped"],
        stats["unavailable"],
        stats["failed"],  # The values to fill in
    )  # End of the log call


# ============================================================== DATABASE
#
# Two tables:
#   collected_urls   -- every PDF-looking URL ever seen, and whether it's been handled
#   downloaded_files -- every file name actually saved, so duplicate names are caught too


class DownloadDatabase:  # Wraps the SQLite connection and every query this script needs
    def __init__(self, path: Path):  # Open (or create) the database file
        logger.info(
            "Opening database: %s", path
        )  # Tell the user which database file is used
        self.connection = sqlite3.connect(path)  # Connect to the database file on disk
        with self.connection:  # Commit automatically when this block finishes
            self.connection.execute(  # Create the URL table if it doesn't exist yet
                "CREATE TABLE IF NOT EXISTS collected_urls ("  # Table name
                "source_url TEXT PRIMARY KEY, is_processed INTEGER NOT NULL DEFAULT 0)"  # One row per URL, with a done flag
            )  # End of the first table definition
            self.connection.execute(  # Create the downloaded-files table if it doesn't exist yet
                "CREATE TABLE IF NOT EXISTS downloaded_files ("  # Table name
                "file_name TEXT PRIMARY KEY COLLATE NOCASE, source_url TEXT NOT NULL, "  # File name (case-insensitive) and its source
                "downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"  # When the file was saved
            )  # End of the second table definition

    def close(self) -> None:  # Close the database connection
        self.connection.close()  # Release the file handle

    def register_existing_pdf_files(
        self, folder: Path
    ) -> None:  # Remember PDFs already on disk
        """Record PDFs already on disk so they're never re-downloaded."""
        count = 0  # Counts how many existing files were newly registered
        with self.connection:  # Commit automatically when this block finishes
            for pdf_path in folder.glob(
                "*.pdf"
            ):  # Look at every PDF already in the folder
                cursor = self.connection.execute(  # Insert the file name unless it's already recorded
                    "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",  # SQL statement
                    (
                        pdf_path.name,
                        "found on disk before first run",
                    ),  # Values for the two placeholders
                )  # End of the insert
                count += cursor.rowcount  # Add 1 when a new row was really inserted
        logger.info(
            "Registered %d existing PDF file(s) from %s", count, folder
        )  # Report the result

    def ignore_non_usa_english_urls(
        self,
    ) -> None:  # Drop old pending URLs we no longer want
        """Drop old pending URLs that don't match the language filter (e.g. after a settings change)."""
        pending = self.load_unprocessed_urls()  # Every URL that is still pending
        ignored = [
            u for u in pending if not is_usa_english_pdf_url(u)
        ]  # Keep only the ones that fail the filter
        for url in ignored:  # Go through each URL that should be ignored
            self.mark_url_processed(url)  # Mark it as done so it's never downloaded
        logger.info(
            "Ignored %d pending URL(s) that do not end in %s",
            len(ignored),
            REQUIRED_FILE_NAME_ENDING,
        )  # Report

    def save_new_urls(self, urls: set) -> None:  # Store newly found URLs
        with self.connection:  # Commit automatically when this block finishes
            cursor = self.connection.executemany(  # Insert many URLs in one call
                "INSERT OR IGNORE INTO collected_urls (source_url) VALUES (?)",
                [(u,) for u in urls],  # Ignore URLs we already know
            )  # End of the insert
        logger.info(
            "Saved %d new URL(s) (%d were already known)",
            cursor.rowcount,
            len(urls) - cursor.rowcount,
        )  # Report the result

    def load_unprocessed_urls(self) -> list:  # Get every URL that still needs work
        rows = self.connection.execute(  # Ask the database for the pending URLs
            "SELECT source_url FROM collected_urls WHERE is_processed = 0 ORDER BY source_url"  # SQL statement
        )  # End of the query
        return [
            row[0] for row in rows.fetchall()
        ]  # Return just the URL text of each row

    def filter_unprocessed(
        self, candidate_urls: list
    ) -> list:  # Keep only the URLs from this list that are still pending
        placeholders = (
            ",".join("?" for _ in candidate_urls) or "''"
        )  # Build one "?" per URL for the SQL query
        rows = self.connection.execute(  # Ask the database which of these URLs are pending
            f"SELECT source_url FROM collected_urls WHERE is_processed = 0 AND source_url IN ({placeholders})",  # SQL statement
            candidate_urls,  # Values for the placeholders
        )  # End of the query
        pending = {
            row[0] for row in rows.fetchall()
        }  # Collect the URLs the database says are pending
        return [
            u for u in candidate_urls if u in pending
        ]  # Keep the candidates in their original order

    def mark_url_processed(self, source_url: str) -> None:  # Mark one URL as done
        with self.connection:  # Commit automatically when this block finishes
            self.connection.execute(
                "UPDATE collected_urls SET is_processed = 1 WHERE source_url = ?",
                (source_url,),
            )  # Flip the flag

    def is_file_name_downloaded(
        self, file_name: str
    ) -> bool:  # Check whether a file name was already saved
        row = self.connection.execute(
            "SELECT 1 FROM downloaded_files WHERE file_name = ?", (file_name,)
        ).fetchone()  # Look it up
        return row is not None  # True when a matching row exists

    def record_downloaded_file(
        self, file_name: str, source_url: str
    ) -> None:  # Save a record of a downloaded file
        with self.connection:  # Commit automatically when this block finishes
            self.connection.execute(  # Insert a row describing the saved file
                "INSERT OR IGNORE INTO downloaded_files (file_name, source_url) VALUES (?, ?)",  # SQL statement
                (file_name, source_url),  # Values for the two placeholders
            )  # End of the insert

    def is_url_already_downloaded_by_name(
        self, source_url: str
    ) -> bool:  # Cheap pre-check before downloading
        """Cheap pre-check: if the URL's own file name is already known/on disk, skip downloading it."""
        guessed = guess_file_name_from_url(
            source_url
        )  # Predict the file name from the URL itself
        if guessed is None:  # The URL doesn't reveal a trustworthy name
            return False  # We can't tell, so assume it still needs downloading
        safe_name = build_safe_file_name(guessed)  # Apply the naming rules to the guess
        return (
            self.is_file_name_downloaded(safe_name)
            or (DOWNLOAD_FOLDER / safe_name).exists()
        )  # Known in the DB or already on disk


# ============================================================== FILE NAMES


def guess_file_name_from_url(
    source_url: str,
) -> Optional[str]:  # Predict the file name before downloading
    last_segment = Path(
        unquote(urlparse(source_url).path)
    ).name  # Take the last part of the URL's path, decoded
    return (
        last_segment if last_segment.lower().endswith(".pdf") else None
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
    stem = (
        raw_file_name[:-4] if raw_file_name.lower().endswith(".pdf") else raw_file_name
    )  # Strip ".pdf" before cleaning
    stem = re.sub(
        r"[^a-z0-9_]+", "_", stem.lower()
    )  # Lowercase, then replace anything but a-z/0-9/_ with _
    stem = re.sub(r"_+", "_", stem)[:MAXIMUM_FILE_NAME_STEM_LENGTH].strip(
        "_"
    )  # Collapse underscores, cut to length, trim ends
    return (
        stem or "document"
    ) + ".pdf"  # Fall back to "document" if nothing is left, then re-add .pdf


# ============================================================== PACING AND BLOCK TRACKING


def register_access_block(
    stats: dict,
) -> None:  # Count one more "Access Denied" in a row
    stats["consecutive_blocks"] += 1  # Add one to the blocks-in-a-row counter
    logger.warning(
        "Consecutive access blocks: %d/%d",
        stats["consecutive_blocks"],
        MAX_CONSECUTIVE_BLOCKS,
    )  # Record the current count
    if (
        stats["consecutive_blocks"] >= MAX_CONSECUTIVE_BLOCKS
    ):  # Check whether the limit is reached
        raise AccessBlockedError(  # Stop the whole run
            f"The website blocked {MAX_CONSECUTIVE_BLOCKS} requests in a row. "  # First half of the message
            "Stopping so the block doesn't get worse. Try again later or contact Agilent."  # Advice
        )  # End of the error


def reset_access_blocks(
    stats: dict,
) -> None:  # A page opened fine, so the blocks-in-a-row count restarts
    stats["consecutive_blocks"] = 0  # Set the counter back to zero


# ============================================================== PAGE CHECKS & ACTIONS

PAGE_UNAVAILABLE_MESSAGE = "the page may be temporarily unavailable or may no longer exist"  # Text of the error page


def is_access_denied_page(
    page: Page,
) -> bool:  # Check whether the website blocked this visit
    try:  # Reading the page can fail while it's changing
        title, source = page.title(), page.content()  # Read the page title and source
    except PlaywrightError:  # The page couldn't be read
        return False  # Assume the page is fine
    lower_source = source.lower()  # Lowercase copy so the checks ignore capitals
    blocked = (  # Decide whether this is the "Access Denied" block page
        "access denied" in title.lower()  # The title says Access Denied
        or "errors.edgesuite.net"
        in lower_source  # The page links to the Akamai error site
        or "you don't have permission to access"
        in lower_source  # The usual block message
    )  # End of the check
    if blocked:  # The website blocked us
        ref = re.search(
            r"Reference #([0-9a-fA-F.]+)", source
        )  # Look for the reference number
        logger.warning(
            "ACCESS DENIED page detected (reference %s) at %s",
            ref.group(1) if ref else "unknown",
            page.url,
        )  # Record it
    return blocked  # Tell the caller whether we were blocked


def is_page_unavailable_page(
    page: Page,
) -> bool:  # Check for the "page may no longer exist" error page
    try:  # Reading the page can fail while it's changing
        text = re.sub(
            r"\s+", " ", page.content().lower()
        )  # Lowercase and squeeze all whitespace to one space
    except PlaywrightError:  # The page couldn't be read
        return False  # Assume the page is fine
    found = PAGE_UNAVAILABLE_MESSAGE in text  # Look for the known error message
    if found:  # The page is the error page
        logger.warning("PAGE UNAVAILABLE message found at %s", page.url)  # Record it
    return found  # Tell the caller what we found


def is_results_page_usable(
    page: Page, stats: dict
) -> bool:  # Check a result page before reading it
    if is_page_unavailable_page(
        page
    ):  # Check for the "page may no longer exist" message
        return False  # Skip this page
    if is_access_denied_page(page):  # Check for the "Access Denied" page
        register_access_block(stats)  # Count the block, which may stop the whole run
        return False  # Skip this page
    reset_access_blocks(
        stats
    )  # The page is fine, so the blocks-in-a-row count restarts
    return True  # The page can be read


def wait_for_network_to_settle(
    page: Page,
) -> None:  # Wait until the page stops loading data
    try:  # Waiting can time out on pages that never stop loading
        page.wait_for_load_state(
            "networkidle", timeout=20_000
        )  # Wait up to 20 seconds for quiet network
    except PlaywrightTimeoutError:  # The page kept loading data
        pass  # Continue anyway
    time.sleep(2)  # give JS a little extra time to draw the results


def dismiss_cookie_banner(page: Page) -> None:  # Close the cookie popup if it shows up
    for selector in (
        "#onetrust-accept-btn-handler",
        "button[id*='accept' i]",
    ):  # Known accept buttons
        try:  # Ignore any problem finding or clicking the button
            button = page.locator(selector).first  # Find the first matching button
            if button.is_visible():  # Only click it if it's visible
                button.click(timeout=3000)  # Accept the cookies
                time.sleep(0.5)  # Give the popup a moment to close
                return  # Stop after the first button that worked
        except PlaywrightError:  # The button was missing or not clickable
            continue  # Try the next selector


def scroll_to_bottom_of_page(
    page: Page, pause: float = 0.8
) -> None:  # Load lazy content by scrolling down
    previous_height = 0  # Remember the page height from the previous round
    for _ in range(15):  # Scroll at most 15 times
        page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )  # Jump to the page bottom
        time.sleep(pause)  # Wait for more content to load
        height = page.evaluate("document.body.scrollHeight")  # Measure the page again
        if height == previous_height:  # Check whether the page stopped growing
            break  # Everything is loaded, so stop scrolling
        previous_height = height  # Remember this height for the next comparison


PDF_OR_SDS_LINK_PATTERN = re.compile(
    r"\.pdf(\?|#|$)|msds|/sds|sds[_/\-.]|safety[-_ ]?data", re.IGNORECASE
)  # Matches links that look like PDFs/SDS
RAW_PDF_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE
)  # Matches raw .pdf URLs hidden in page source


def extract_pdf_urls_from_page(
    page: Page,
) -> set:  # Find every PDF-looking URL on the page
    found = set()  # Collects the URLs found on this page
    for href in page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map(e => e.href)"
    ):  # Read the address of every link on the page
        if href.startswith("http") and PDF_OR_SDS_LINK_PATTERN.search(
            href
        ):  # Looks like a PDF or SDS link
            found.add(href)  # Keep it
    for raw_url in RAW_PDF_URL_PATTERN.findall(
        page.content()
    ):  # Search the raw page source too
        found.add(
            raw_url.replace("\\u0026", "&").replace("&amp;", "&")
        )  # Keep the URL after decoding escaped ampersands
    usa_english = {
        u for u in found if is_usa_english_pdf_url(u)
    }  # Keep USA English links only
    logger.debug(
        "Found %d PDF link(s), %d after the USA English filter",
        len(found),
        len(usa_english),
    )  # Record the counts
    return usa_english  # Give only the USA English URLs back


NEXT_PAGE_BUTTON_SELECTORS = [  # Different ways the "next page" button might be found
    "button[aria-label*='next' i]",
    "a[aria-label*='next' i]",  # Elements whose label mentions "next"
    "li.next:not(.disabled) a",
    "a.next",
    "button.next",  # Elements with a "next" class
    "[class*='pagination'] [class*='next']",  # A "next" element inside pagination
    "xpath=//button[normalize-space()='Next' or normalize-space()='>']",  # Button with text Next or >
    "xpath=//a[normalize-space()='Next' or normalize-space()='>']",  # Link with text Next or >
]  # End of the selector list


def click_next_page_button(
    page: Page,
) -> bool:  # Go to the next result page if possible
    for (
        selector
    ) in NEXT_PAGE_BUTTON_SELECTORS:  # Try each known way of finding the button
        locator = page.locator(selector)  # Find every element matching this selector
        try:  # Counting can fail if the page changes at the wrong moment
            count = locator.count()  # Count the matches
        except PlaywrightError:  # The selector couldn't be used
            continue  # Try the next selector
        for i in range(count):  # Check each candidate by position
            candidate = locator.nth(i)  # Pick the candidate
            try:  # The element may go stale or be covered
                if (
                    not candidate.is_visible() or not candidate.is_enabled()
                ):  # Hidden or disabled
                    continue  # Skip this candidate
                if candidate.get_attribute("aria-disabled") == "true" or "disabled" in (
                    candidate.get_attribute("class") or ""
                ):  # Other disabled markers
                    continue  # Skip this candidate
                human_click_element(
                    page, candidate
                )  # Move the pointer to the button like a person and click it
                logger.info(
                    "Clicked next page button (%s)", selector
                )  # Record the click
                return True  # Report that we moved to another page
            except PlaywrightError:  # The click didn't work
                continue  # Try the next candidate
    logger.info(
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
# Mouse movement, scrolling and random letter keys happen on every page the script visits:
# the home page, the three other pages, the search pages, and while downloading.
# Random letters are only pressed, never submitted: Enter and other special keys are never used,
# and any focused text box is un-focused first, so no new search is ever started.

# ---------------------------------------------------------------- 1. LINKS AND URLS

EXCLUDED_BROWSING_LINK_PATTERN = re.compile(  # Pattern for links a casual visitor wouldn't click
    r"log-?in|log-?out|sign-?in|sign-?out|register|cart|checkout|account"  # Login, logout, sign-in, registration, shopping and account pages
    r"|\.pdf|msds|/sds|mailto:|tel:|javascript:",  # Documents, e-mail, phone and script links
    re.IGNORECASE,  # Ignore capital letters
)  # End of the pattern
LINK_CANDIDATES_SCRIPT = (  # JavaScript that describes every link on the page, in page order
    "() => Array.from(document.querySelectorAll('a[href]')).map((a, index) => {"  # Go through every link and remember its position
    "const box = a.getBoundingClientRect();"  # Size and position of the link on screen
    "const style = getComputedStyle(a);"  # How the link is displayed
    "return {"  # Start the description of this link
    "index: index,"  # Position of the link among all links
    "href: a.href,"  # Full address of the link
    "visible: box.width > 20 && box.height > 10 && style.visibility !== 'hidden' && style.display !== 'none',"  # Is it big enough and shown
    "new_tab: a.target === '_blank',"  # Does it open a new tab
    "download: a.hasAttribute('download'),"  # Is it a download link
    "};"  # End of the description of this link
    "})"  # End of the loop over all links
)  # End of the script
UNFOCUS_TEXT_BOXES_SCRIPT = (  # JavaScript that takes the keyboard focus away from any text box
    "() => { const e = document.activeElement;"  # The element that currently has the focus
    " if (e && e !== document.body && (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) || e.isContentEditable)) e.blur(); }"  # If it is a text box, take the focus away
)  # End of the script


def without_fragment(url: str) -> str:  # Drop the "#section" part of a URL
    return url.split("#")[0]  # Everything before the first "#"


def pick_random_internal_link(
    page: Page, visited: set
) -> Optional[tuple]:  # Choose a random visible Agilent link we haven't used
    candidates = []  # Collects (position on the page, address) pairs
    for item in page.evaluate(LINK_CANDIDATES_SCRIPT):  # Look at every link on the page
        href = item["href"]  # The full address of the link
        parsed = urlparse(href)  # Split the address into parts
        hostname = parsed.hostname or ""  # The website name, empty if missing
        if (
            not item["visible"] or item["new_tab"] or item["download"]
        ):  # Hidden, popup or download link
            continue  # A normal visitor wouldn't click it here
        if parsed.scheme not in ("http", "https"):  # Not a normal web address
            continue  # Skip it
        if not hostname.endswith("agilent.com"):  # Not an Agilent page
            continue  # Stay on Agilent's own website
        if EXCLUDED_BROWSING_LINK_PATTERN.search(href):  # Login, cart, PDFs and similar
            continue  # Skip it
        if without_fragment(href) in visited:  # Already visited, or the page we're on
            continue  # We want different pages
        candidates.append((item["index"], href))  # This link is a good candidate
    return random.choice(candidates) if candidates else None  # One at random, if any


# ---------------------------------------------------------------- 2. HUMAN-LIKE INPUT

mouse_position = {
    "x": 200.0,
    "y": 200.0,
}  # Where the pointer is now (Playwright can't tell us, so we remember it)


def get_viewport_size(page: Page) -> tuple:  # Width and height of the visible page area
    size = page.viewport_size or {
        "width": 1280,
        "height": 720,
    }  # Fall back to a common size
    return size["width"], size["height"]  # Give both numbers back


def human_mouse_move(
    page: Page, target_x: float, target_y: float
) -> None:  # Move the pointer along a curved, uneven path
    width, height = get_viewport_size(page)  # Size of the page area
    target_x = min(
        max(target_x, 1), width - 1
    )  # Keep the target inside the page horizontally
    target_y = min(
        max(target_y, 1), height - 1
    )  # Keep the target inside the page vertically
    start_x, start_y = (
        mouse_position["x"],
        mouse_position["y"],
    )  # Where the pointer is now
    distance = math.hypot(
        target_x - start_x, target_y - start_y
    )  # Straight-line distance to travel
    if distance < 2:  # Already there
        return  # Nothing to do
    steps = max(12, min(60, int(distance / 10)))  # Longer moves use more small steps
    bend = distance * random.uniform(0.1, 0.3)  # How much the path curves
    control_x = (start_x + target_x) / 2 + random.choice(
        (-1, 1)
    ) * bend * random.random()  # Curve point, pulled sideways
    control_y = (start_y + target_y) / 2 + random.choice(
        (-1, 1)
    ) * bend * random.random()  # Curve point, pulled up or down
    for step in range(1, steps + 1):  # Walk along the curve
        t = step / steps  # How far along the path we are (0 to 1)
        eased = (
            t * t * (3 - 2 * t)
        )  # Start slowly, speed up in the middle, slow down at the end
        x = (
            (1 - eased) ** 2 * start_x
            + 2 * (1 - eased) * eased * control_x
            + eased**2 * target_x
        )  # Point on the curve, horizontal
        y = (
            (1 - eased) ** 2 * start_y
            + 2 * (1 - eased) * eased * control_y
            + eased**2 * target_y
        )  # Point on the curve, vertical
        if step < steps:  # Every step except the last one
            x += random.uniform(-1.0, 1.0)  # Tiny hand shake, horizontal
            y += random.uniform(-1.0, 1.0)  # Tiny hand shake, vertical
        page.mouse.move(x, y)  # Move the pointer to this point
        time.sleep(random.uniform(0.004, 0.014))  # Tiny pause between steps
    mouse_position["x"], mouse_position["y"] = (
        target_x,
        target_y,
    )  # Remember where the pointer ended up


def human_click_element(
    page: Page, element
) -> None:  # Move the pointer to an element like a person and click it
    element.scroll_into_view_if_needed(timeout=5000)  # Bring the element into view
    box = element.bounding_box()  # Position and size of the element on screen
    if box is None:  # The element has no size right now
        element.click(timeout=5000)  # Fall back to a normal click
        return  # Done
    click_x = box["x"] + box["width"] * random.uniform(
        0.3, 0.7
    )  # A spot inside the element, horizontal
    click_y = box["y"] + box["height"] * random.uniform(
        0.3, 0.7
    )  # A spot inside the element, vertical
    human_mouse_move(page, click_x, click_y)  # Move the pointer there like a person
    time.sleep(random.uniform(0.2, 0.6))  # Short hesitation before clicking
    page.mouse.click(
        mouse_position["x"], mouse_position["y"], delay=random.randint(50, 150)
    )  # Click, holding the button briefly


def human_scroll(
    page: Page, pixels: int
) -> None:  # Scroll with the mouse wheel in small, uneven notches
    direction = 1 if pixels > 0 else -1  # Down is positive, up is negative
    remaining = abs(pixels)  # How much is still left to scroll
    while remaining > 0:  # Keep scrolling until we've covered the distance
        notch = min(remaining, random.randint(40, 120))  # Size of this wheel notch
        page.mouse.wheel(0, direction * notch)  # Turn the wheel a little
        remaining -= notch  # Count it off
        time.sleep(random.uniform(0.02, 0.09))  # Short pause between notches
        if random.random() < 0.08:  # Now and then
            time.sleep(random.uniform(0.3, 0.9))  # Stop to read for a moment


def human_type_random_letters(
    page: Page,
) -> None:  # Press a few random letter keys with human-like timing
    page.evaluate(
        UNFOCUS_TEXT_BOXES_SCRIPT
    )  # No text box may have the focus, so the letters can't change a search box
    for _ in range(random.randint(2, 6)):  # A few key presses
        letter = random.choice(
            string.ascii_lowercase
        )  # Pick a random letter a-z (never Enter or other special keys)
        page.keyboard.press(
            letter, delay=random.randint(40, 140)
        )  # Press and release it, holding the key briefly
        time.sleep(random.uniform(0.08, 0.35))  # Typing rhythm between keys
        if random.random() < 0.15:  # Sometimes
            time.sleep(random.uniform(0.4, 1.0))  # Hesitate like someone thinking


def act_like_a_person_on_page(
    page: Page, seconds: float
) -> None:  # Move the mouse, scroll and press random letters for a while
    deadline = time.time() + seconds  # Moment when we stop
    width, height = get_viewport_size(page)  # Size of the page area
    while time.time() < deadline:  # Keep going until the time is up
        try:  # Browser actions can fail while the page is changing
            roll = random.random()  # Pick what to do next
            if roll < 0.2:  # Sometimes
                human_type_random_letters(page)  # Press a few random letters
            elif roll < 0.55:  # Mostly read the page
                human_scroll(page, random.randint(200, 650))  # Scroll down
            elif roll < 0.9:  # Often move the mouse somewhere else
                human_mouse_move(
                    page,
                    random.uniform(40, width - 40),
                    random.uniform(40, height - 40),
                )  # Move to a random spot
            else:  # Occasionally look back up
                human_scroll(page, -random.randint(100, 300))  # Scroll up a little
        except PlaywrightError as error:  # The action didn't work
            if is_browser_closed_error(error):  # The browser itself is gone
                raise  # Let the caller deal with it
        remaining = deadline - time.time()  # Time left
        if remaining > 0:  # Still time to spend
            time.sleep(
                min(remaining, random.uniform(0.5, 2.0))
            )  # Human-like pause between actions


def pause_while_acting_like_a_person(
    page: Page, low: float, high: float, reason: str
) -> None:  # Wait a random time, acting like a person instead of sitting still
    seconds = random.uniform(low, high)  # Pick a random length inside the range
    logger.debug(
        "Pausing %.1f seconds with mouse and keyboard activity (%s)", seconds, reason
    )  # Record the pause and why it happens
    try:  # The browser can fail while we act
        act_like_a_person_on_page(
            page, seconds
        )  # Move the mouse, scroll and press random letters
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as error:  # The browser had a problem
        logger.debug(
            "Activity during the pause failed: %s", str(error).splitlines()[0]
        )  # Record it
        time.sleep(seconds)  # Just wait instead, the next step deals with the browser


# ---------------------------------------------------------------- 3. THE BROWSING ROUTINE


def open_random_link(
    page: Page, visited: set
) -> Optional[str]:  # Click a random link and return the address of the page it opens
    for _ in range(5):  # Try a few different links if one doesn't work
        candidate = pick_random_internal_link(page, visited)  # Choose a link
        if candidate is None:  # No suitable link on this page
            return None  # Give up
        index, href = candidate  # Position and address of the link
        visited.add(without_fragment(href))  # Never choose this one again
        url_before = without_fragment(page.url)  # Where we are now
        try:  # Clicking can fail
            link = page.locator("a[href]").nth(index)  # Find the link by its position
            human_click_element(
                page, link
            )  # Move the pointer to it like a person and click it
            page.wait_for_url(  # Wait until we really moved to another page
                lambda url: without_fragment(url)
                != url_before,  # The address must be different
                wait_until="domcontentloaded",  # The page must have started loading
                timeout=8000,  # Give up after 8 seconds
            )  # End of the wait
        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ) as error:  # The click didn't lead anywhere
            if is_browser_closed_error(error):  # The browser itself is gone
                raise  # Stop browsing
            logger.debug("Browsing: link %s did not open a page", href)  # Record it
            continue  # Try another link
        return page.url  # We are on a new page
    return None  # None of the links worked


def return_to_home_page(
    page: Page, home_url: str
) -> None:  # Go back to the home page like a person would
    try:  # Going back can fail
        page.go_back(
            wait_until="domcontentloaded", timeout=15_000
        )  # Use the back button
    except (PlaywrightError, PlaywrightTimeoutError) as error:  # Back didn't work
        if is_browser_closed_error(error):  # The browser itself is gone
            raise  # Stop browsing
    if without_fragment(page.url) != without_fragment(home_url):  # Not on the home page
        page.goto(
            HOME_PAGE_URL, wait_until="domcontentloaded"
        )  # Open it directly instead
    time.sleep(random.uniform(1.0, 2.0))  # Let the home page draw itself


def browse_like_a_normal_user(
    page: Page,
) -> None:  # Move around the home page, then open a few different pages from it
    try:  # Any browser problem just ends the browsing early
        home_url = page.url  # The page we start from and keep returning to
        visited = {
            without_fragment(home_url),
            without_fragment(HOME_PAGE_URL),
        }  # Pages already seen
        logger.info(  # Record the first step
            "Browsing: mouse, scrolling and random keys on the home page for about %d seconds",  # Message
            HOME_PAGE_BROWSING_SECONDS,  # Value for %d
        )  # End of the log call
        act_like_a_person_on_page(
            page, HOME_PAGE_BROWSING_SECONDS
        )  # Home page: mouse, scrolling and random letters

        for visit_number in range(
            1, OTHER_PAGES_TO_VISIT + 1
        ):  # Open the other pages one by one
            visit_started = time.time()  # When this visit began
            opened_url = open_random_link(
                page, visited
            )  # Click a random link on the home page
            if opened_url is None:  # No link worked
                logger.warning(
                    "Browsing: could not open another page, stopping early"
                )  # Record it
                break  # Stop browsing
            logger.info(  # Record the visit
                "Browsing: page %d/%d opened (about %d seconds on it): %s",  # Message
                visit_number,  # Value for the first %d
                OTHER_PAGES_TO_VISIT,  # Value for the second %d
                OTHER_PAGE_BROWSING_SECONDS,  # Value for the third %d
                opened_url,  # Value for %s
            )  # End of the log call
            time_left = max(
                2.0, OTHER_PAGE_BROWSING_SECONDS - (time.time() - visit_started)
            )  # Rest of this visit's time
            act_like_a_person_on_page(
                page, time_left
            )  # Other pages: mouse, scrolling and random letters
            if visit_number < OTHER_PAGES_TO_VISIT:  # Another visit follows
                return_to_home_page(page, home_url)  # Go back to the home page first
    except (
        PlaywrightError,
        PlaywrightTimeoutError,
    ) as error:  # Something went wrong in the browser
        logger.warning(
            "Browsing like a normal visitor ended early: %s", str(error).splitlines()[0]
        )  # Record it, scraping goes on anyway
        return  # Leave the function
    logger.info("Finished browsing like a normal visitor")  # Record the end


def warm_up_session_on_home_page(
    page: Page,
) -> bool:  # Open the home page and browse around a little first
    logger.info(
        "Warm-up: opening the home page %s", HOME_PAGE_URL
    )  # Record the first step
    try:  # Opening the page can fail
        page.goto(HOME_PAGE_URL, wait_until="domcontentloaded")  # Open the home page
    except PlaywrightError as e:  # The page couldn't be opened
        logger.error("Warm-up: could not open the home page: %s", e)  # Record the error
        return False  # The warm-up failed
    wait_for_network_to_settle(page)  # Let the home page finish loading
    dismiss_cookie_banner(page)  # Accept the cookie popup like a normal visitor
    if is_access_denied_page(page):  # Check whether even the home page is blocked
        logger.error(
            "Warm-up: the home page itself shows Access Denied"
        )  # Record the problem
        return False  # The warm-up failed
    browse_like_a_normal_user(
        page
    )  # Move around the home page and open a few different pages, like a real visitor
    return True  # The warm-up worked


PAGE_STATUS_OPENED = "opened"  # The page opened normally
PAGE_STATUS_UNAVAILABLE = (
    "unavailable"  # The page shows the "page may no longer exist" message
)
PAGE_STATUS_BLOCKED = "blocked"  # The page kept showing "Access Denied"
PAGE_STATUS_FAILED = "failed"  # The page couldn't be opened at all


def open_page_politely(
    page: Page, url: str, stats: dict
) -> str:  # Open a page, backing off if blocked
    """Open a page, backing off and retrying through the home page if blocked."""
    for attempt in range(
        1, MAX_ACCESS_DENIED_ATTEMPTS + 1
    ):  # Try a limited number of times
        logger.info(
            "Opening %s (attempt %d/%d)", url, attempt, MAX_ACCESS_DENIED_ATTEMPTS
        )  # Record the attempt
        try:  # Opening the page can fail
            page.goto(url, wait_until="domcontentloaded")  # Open the page
        except PlaywrightError as e:  # The page couldn't be opened
            logger.error("Could not open %s: %s", url, e)  # Record the error
            return PAGE_STATUS_FAILED  # Give up on this page
        wait_for_network_to_settle(page)  # Wait for the page to finish loading
        if is_page_unavailable_page(
            page
        ):  # Check for the "page may no longer exist" message
            return PAGE_STATUS_UNAVAILABLE  # Skip this page, retrying won't help
        if not is_access_denied_page(page):  # Check whether the website let us in
            reset_access_blocks(stats)  # The blocks-in-a-row count restarts
            return PAGE_STATUS_OPENED  # The page opened normally
        register_access_block(stats)  # Count the block, which may stop the whole run
        if (
            attempt < MAX_ACCESS_DENIED_ATTEMPTS
        ):  # Check whether another attempt is allowed
            backoff = ACCESS_DENIED_BACKOFF_SECONDS * (
                2 ** (attempt - 1)
            ) + random.uniform(
                0, 5
            )  # Longer each time
            logger.info(
                "Backing off for %.0f seconds, then revisiting the home page", backoff
            )  # Explain
            time.sleep(backoff)  # Wait before trying again
            warm_up_session_on_home_page(
                page
            )  # Visit the home page again to refresh the session
    logger.error(
        "Still blocked after %d attempt(s): %s", MAX_ACCESS_DENIED_ATTEMPTS, url
    )  # Record failure
    return PAGE_STATUS_BLOCKED  # The page couldn't be opened


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
    page: Page, source_url: str
) -> tuple:  # Ask the browser to download one URL
    """Navigate to a URL and wait for the browser to report a download, or classify what happened instead."""
    received = []  # Collects the Download objects as the browser reports them

    def collect_download(
        download: Download,
    ) -> None:  # Handler must be a normal function, a built-in like list.append can't be registered
        received.append(download)  # Remember the download

    page.on("download", collect_download)  # Start listening for downloads on this tab
    try:  # Make sure the listener is always removed again
        try:  # Opening a download link can raise an error, which is normal
            response = page.goto(
                source_url, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS
            )  # Open the link
        except PlaywrightError as e:  # The browser reported an error for the navigation
            if "Download is starting" in str(e) or "NS_BINDING_ABORTED" in str(
                e
            ):  # Expected
                response = None  # expected: navigation turned straight into a download
            else:  # Some other, real error
                raise  # Let the calling code handle it

        if (
            response is not None
            and "text/html" in response.headers.get("content-type", "").lower()
        ):  # The server returned a web page
            try:  # Waiting for the page can time out
                page.wait_for_load_state(
                    "domcontentloaded", timeout=15_000
                )  # Let the page finish loading
            except PlaywrightTimeoutError:  # The page took too long
                pass  # Continue with whatever loaded
            if is_page_unavailable_page(
                page
            ):  # Check for the "page may no longer exist" message
                return None, OUTCOME_PAGE_UNAVAILABLE  # No PDF exists at this address
            if is_access_denied_page(page):  # Check for the "Access Denied" page
                return None, OUTCOME_ACCESS_DENIED  # The website blocked this request
            return None, OUTCOME_NOT_A_DOWNLOAD  # Some other web page, not a file

        deadline = (
            time.time() + DOWNLOAD_TIMEOUT_MS / 1000
        )  # Moment when we give up waiting
        while (
            not received and time.time() < deadline
        ):  # Wait for the browser to report the download
            page.wait_for_timeout(
                200
            )  # Wait a moment while Playwright processes browser events
        return (
            (received[0], OUTCOME_DOWNLOAD_STARTED)
            if received
            else (None, OUTCOME_TIMEOUT)
        )  # Hand back what we got
    finally:  # Always runs, even after an error
        page.remove_listener(
            "download", collect_download
        )  # Stop listening for downloads


def download_pdf_urls(
    page: Page,
    db: DownloadDatabase,
    urls: list,
    stats: dict,
    internet: "InternetMonitor",
    activity_page: Page,  # The results tab, where the mouse and keyboard activity happens
) -> None:  # Download a batch of URLs
    """Download each URL in turn on the given (temporary) tab."""
    made_a_request = (
        False  # Tracks whether we already contacted the website in this batch
    )
    for i, source_url in enumerate(urls, start=1):  # Handle each URL one at a time
        logger.info(
            "(%d/%d) Processing %s", i, len(urls), source_url
        )  # Record the progress
        internet.raise_if_down()  # Stop here and wait if the internet is down

        if db.is_url_already_downloaded_by_name(
            source_url
        ):  # Skip when the predicted file already exists
            logger.info(
                "  SKIP: the file for this URL already exists"
            )  # Explain the skip
            db.mark_url_processed(source_url)  # Remember this URL is finished
            stats["skipped"] += 1  # Count the skip
            continue  # Move on to the next URL

        if made_a_request:  # Only pause between two real requests
            activity_page.bring_to_front()  # Show the results tab, a hidden tab would look odd to the website
            pause_while_acting_like_a_person(
                activity_page, *PAUSE_BETWEEN_DOWNLOADS, "between downloads"
            )  # Move the mouse, scroll and press random letters instead of sitting still
        made_a_request = True  # From now on we've contacted the website

        try:  # Starting a download can raise browser errors
            download, outcome = start_download(
                page, source_url
            )  # Let the browser download it
        except PlaywrightError as e:  # A real browser error happened
            if is_browser_closed_error(
                e
            ):  # Check whether the browser itself has closed
                raise  # Let the restart logic deal with it, this URL stays pending
            if (
                internet.confirm_down()
            ):  # Check whether the internet being down caused the error
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
            logger.error(
                "  FAILED to open %s: %s", source_url, str(e).splitlines()[0]
            )  # Record it
            stats["failed"] += 1  # Count the failure
            continue  # Move on to the next URL

        if (
            outcome == OUTCOME_PAGE_UNAVAILABLE
        ):  # The link led to the "page may no longer exist" message
            logger.warning(
                "  SKIP: page unavailable, no PDF at %s", source_url
            )  # Explain the skip
            db.mark_url_processed(source_url)  # Never try this URL again
            stats["unavailable"] += 1  # Count it as unavailable
            reset_access_blocks(
                stats
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if outcome == OUTCOME_ACCESS_DENIED:  # The website blocked this request
            logger.error(
                "  FAILED: access denied for %s", source_url
            )  # Record the failure
            stats["failed"] += 1  # Count the failure
            register_access_block(
                stats
            )  # Count the block, which may stop the whole run
            continue  # Move on to the next URL
        if (
            outcome == OUTCOME_NOT_A_DOWNLOAD
        ):  # The link is an ordinary web page, not a PDF
            logger.warning(
                "  SKIP: the link is a web page, not a PDF: %s", source_url
            )  # Explain the skip
            db.mark_url_processed(source_url)  # Never try this URL again
            stats["skipped"] += 1  # Count it as skipped
            reset_access_blocks(
                stats
            )  # The website answered normally, so the blocks-in-a-row count restarts
            continue  # Move on to the next URL
        if download is None:  # The browser never produced a download
            if (
                internet.confirm_down()
            ):  # Check whether the internet being down is the reason
                raise InternetOutageError(
                    "The internet went down while downloading"
                )  # Wait for it, this URL stays pending
            logger.error(
                "  FAILED: no download (%s) for %s", outcome, source_url
            )  # Record the failure
            stats["failed"] += 1  # Count the failure
            continue  # Move on to the next URL
        reset_access_blocks(
            stats
        )  # A download started, so the blocks-in-a-row count restarts

        _finish_download(
            download, source_url, db, stats
        )  # Validate, name and save the finished download

    log_run_statistics(stats, "Totals so far")  # Show the totals after this batch


def _finish_download(
    download: Download, source_url: str, db: DownloadDatabase, stats: dict
) -> None:  # Validate and save a completed download
    """Validate, name, de-duplicate and save a download that the browser already started."""
    suggested_name = (
        download.suggested_filename
    )  # The file name the browser would have used
    failure = download.failure()  # Wait for the download to end and get any error
    if failure:  # The browser reported that the download failed
        logger.error(
            "  FAILED: browser download error '%s' for %s", failure, source_url
        )  # Log
        stats["failed"] += 1  # Count the failure
        return  # Stop here, nothing more to do

    with open(download.path(), "rb") as f:  # Open the downloaded file for reading
        if f.read(4) != b"%PDF":  # Real PDF files always start with %PDF
            logger.error(
                "  FAILED: downloaded file is not a PDF: %s", source_url
            )  # Record the problem
            download.delete()  # Throw the file away
            stats["failed"] += 1  # Count the failure
            return  # Stop here, nothing more to do

    safe_name = build_safe_file_name(
        suggested_name
    )  # Apply the naming rules to the real name
    safe_path = DOWNLOAD_FOLDER / safe_name  # Full path where the PDF will be saved
    if (
        db.is_file_name_downloaded(safe_name) or safe_path.exists()
    ):  # Decide whether this file name is already saved
        logger.info(
            "  DUPLICATE file name, not saved: %s", safe_name
        )  # Explain what happens
        download.delete()  # Discard the extra copy
        db.mark_url_processed(source_url)  # Remember this URL is finished
        stats["skipped"] += 1  # Count it as skipped
        return  # Stop here, nothing more to do

    download.save_as(safe_path)  # Save the PDF into the PDFs folder
    db.record_downloaded_file(safe_name, source_url)  # Save the file in the database
    db.mark_url_processed(source_url)  # Remember this URL is finished
    stats["downloaded"] += 1  # Count the success
    logger.info(
        "  SAVED: %s (%d bytes)", safe_path, safe_path.stat().st_size
    )  # Record the result


def download_pdf_urls_in_temporary_tab(
    session: "BrowserSession", db: DownloadDatabase, urls: list, stats: dict
) -> None:  # Download, opening a tab only if needed
    """Pre-filter already-downloaded URLs, then open the download tab only if something's left to do."""
    needs_download = []  # Collects the URLs whose files are really missing
    for url in urls:  # Check each URL before opening any tab
        if db.is_url_already_downloaded_by_name(url):  # The file already exists
            logger.info(
                "  SKIP: the file for %s already exists", url
            )  # Explain the skip
            db.mark_url_processed(url)  # Remember this URL is finished
            stats["skipped"] += 1  # Count the skip
        else:  # The file is missing
            needs_download.append(url)  # Keep the URL for downloading
    if not needs_download:  # Check whether anything is left to download
        logger.info(
            "Nothing to download in this batch, so no second tab is opened"
        )  # Record the decision
        return  # Leave without opening a tab
    session.internet_monitor.raise_if_down()  # Don't open a tab while the internet is down
    with session.temporary_download_tab() as download_page:  # Open the second tab and close it afterwards
        download_pdf_urls(
            download_page,
            db,
            needs_download,
            stats,
            session.internet_monitor,
            session.results_page,
        )  # Download the missing files


# ============================================================== SCRAPING


def collect_and_download_pdf_urls_for_keyword(
    session: "BrowserSession", db: DownloadDatabase, keyword: str, stats: dict
) -> None:  # Scrape one keyword
    page = session.results_page  # The tab used for browsing the search results
    session.internet_monitor.raise_if_down()  # Wait for the internet before opening the search page
    url = SEARCH_URL_TEMPLATE.format(search_keyword=keyword)  # Build the search address
    logger.info("Opening search page for keyword '%s': %s", keyword, url)  # Record it

    status = open_page_politely(
        page, url, stats
    )  # Open the page, backing off if blocked
    if (
        status == PAGE_STATUS_FAILED and session.internet_monitor.confirm_down()
    ):  # No internet?
        raise InternetOutageError(
            "The internet went down while opening the search page"
        )  # Wait for it, then retry
    if status != PAGE_STATUS_OPENED:  # Check whether the page opened normally
        logger.warning(
            "[%s] skipping this keyword (page status: %s)", keyword, status
        )  # Explain the skip
        return  # Nothing was collected, so go on to the next keyword
    dismiss_cookie_banner(page)  # Close the cookie popup if present
    pause_while_acting_like_a_person(
        page, *ACTIVITY_AFTER_OPENING_SEARCH_PAGE, "after opening the search page"
    )  # Move the mouse, scroll and press random letters before reading the results

    all_urls = set()  # Collects the URLs from every result page of this keyword
    for page_number in range(
        1, MAXIMUM_RESULT_PAGES_PER_KEYWORD + 1
    ):  # Walk through the result pages
        session.close_unneeded_tabs()  # Close any tab other than the results and download tabs
        session.internet_monitor.raise_if_down()  # Stop here and wait if the internet is down
        page.bring_to_front()  # Keep the results tab in front
        if page_number > 1:  # A next result page has just opened
            pause_while_acting_like_a_person(
                page, *ACTIVITY_AFTER_OPENING_SEARCH_PAGE, "after opening a result page"
            )  # Move the mouse, scroll and press random letters before reading the results
        scroll_to_bottom_of_page(page)  # Make sure all results on this page are loaded

        urls_here = extract_pdf_urls_from_page(page)  # Read the URLs from this page
        new_urls = urls_here - all_urls  # Work out which ones are new
        all_urls |= urls_here  # Add this page's URLs to the keyword total
        logger.info(
            "[%s] page %d: %d link(s), %d new, %d total",
            keyword,
            page_number,
            len(urls_here),
            len(new_urls),
            len(all_urls),
        )  # Show progress

        if new_urls:  # Only touch the database when there's something new
            db.save_new_urls(new_urls)  # Store the new URLs in the database
            to_download = db.filter_unprocessed(
                sorted(new_urls)
            )  # Work out which of them are still pending
            logger.info(
                "[%s] page %d: downloading %d PDF(s) right away",
                keyword,
                page_number,
                len(to_download),
            )  # Say
            download_pdf_urls_in_temporary_tab(
                session, db, to_download, stats
            )  # Download them now

        if page_number > 1 and not new_urls:  # Check whether paging gave us nothing new
            logger.info(
                "[%s] no new links on this page, stopping", keyword
            )  # Explain why we stop
            break  # Stop, we're probably seeing the same page again

        pause_while_acting_like_a_person(
            page, *PAUSE_BETWEEN_RESULT_PAGES, "before the next result page"
        )  # Move the mouse, scroll and press random letters instead of sitting still
        page.bring_to_front()  # Return to the results tab before looking for the next page button
        if not click_next_page_button(page):  # Try to go to the next page
            break  # There's no next page, so this keyword is finished
        wait_for_network_to_settle(page)  # Wait for the next page of results to load
        if not is_results_page_usable(
            page, stats
        ):  # Check for the error page or "Access Denied"
            logger.warning(
                "[%s] the next result page is not usable, moving on", keyword
            )  # Explain the skip
            break  # Skip the rest of this keyword and go to the next one

    logger.info(
        "[%s] finished with %d PDF link(s)", keyword, len(all_urls)
    )  # Keyword summary


# ============================================================== INTERNET MONITOR


def is_host_responding(host: str) -> bool:  # Check whether one address answers us
    if shutil.which("ping"):  # Check whether the ping program is installed
        if sys.platform.startswith("win"):  # Windows uses different ping options
            cmd = [
                "ping",
                "-n",
                "1",
                "-w",
                str(PING_TIMEOUT_SECONDS * 1000),
                host,
            ]  # One ping, timeout in ms
        elif sys.platform.startswith(
            "linux"
        ):  # Linux uses -W for the timeout in seconds
            cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), host]  # One ping
        else:  # macOS and other systems
            cmd = [
                "ping",
                "-c",
                "1",
                host,
            ]  # One ping, time limit enforced by subprocess.run below
        try:  # Running ping can fail or take too long
            if (
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PING_TIMEOUT_SECONDS + 2,
                ).returncode
                == 0
            ):  # Exit code 0 means it answered
                return True  # The host responded
        except (
            subprocess.TimeoutExpired,
            OSError,
        ):  # Ping took too long or couldn't run
            pass  # Fall through to the TCP fallback
    try:  # Some networks block ping, so also try a plain connection
        with socket.create_connection(
            (host, TCP_FALLBACK_PORT), timeout=TCP_FALLBACK_TIMEOUT_SECONDS
        ):  # Connect
            return True  # The connection worked, so the host responded
    except OSError:  # The connection didn't work
        return False  # The host did not respond


def check_internet_connection() -> bool:  # The internet works when at least one address answers
    return any(
        is_host_responding(h) for h in INTERNET_CHECK_HOSTS
    )  # One answer is enough


class InternetMonitor:  # Checks the internet in a background thread and tells the session when it goes down
    """Watches the internet in a background thread and flags the browser for restarting on outage."""

    def __init__(self):  # Prepare the shared state
        self.available = threading.Event()  # Set while the internet works
        self.stop_requested = threading.Event()  # Set when the monitor should stop
        self.lock = (
            threading.Lock()
        )  # Prevents two threads from changing the state at the same time
        self.outage_function = (
            None  # Function that is called when the internet goes down
        )
        self.outage_occurred = (
            False  # True when the browser must be restarted because of an outage
        )
        self.thread = None  # The background thread

    def start(
        self, outage_function
    ) -> None:  # Do a first check and start the background thread
        self.outage_function = outage_function  # Remember what to call on an outage
        if check_internet_connection():  # First check, done right now
            self.available.set()  # The internet works
            logger.info("Internet check: the internet is working")  # Record it
        else:  # No answer from either address
            logger.warning(
                "Internet check: no answer yet, waiting for the internet"
            )  # Say
        self.thread = threading.Thread(
            target=self._loop, name="internet-monitor", daemon=True
        )  # Create the monitor thread
        self.thread.start()  # Start checking in the background

    def stop(self) -> None:  # Stop the background thread
        self.stop_requested.set()  # Ask the thread to finish
        if self.thread:  # Check whether it was started
            self.thread.join(timeout=15)  # Wait a little for it to finish

    def _loop(self) -> None:  # The work done by the background thread
        while not self.stop_requested.is_set():  # Keep going until we're told to stop
            self.update_state(
                check_internet_connection()
            )  # Check the internet and react to any change
            self.stop_requested.wait(
                INTERNET_CHECK_INTERVAL_SECONDS
            )  # Sleep, but wake up early when stopping

    def update_state(
        self, internet_works: bool
    ) -> None:  # React when the internet goes down or comes back
        went_down = False  # Becomes True when the internet has just gone down
        with self.lock:  # Only one thread may change the state at a time
            was_available = self.available.is_set()  # What we believed until now
            if internet_works and not was_available:  # The internet came back
                self.available.set()  # Record that it works again
                logger.warning("INTERNET IS BACK")  # Tell the user
            elif not internet_works and was_available:  # The internet just went down
                self.available.clear()  # Record that it doesn't work
                self.outage_occurred = (
                    True  # Remember that the browser must be restarted afterwards
                )
                went_down = True  # The outage function has to be called
                logger.error("INTERNET IS DOWN")  # Tell the user
        if (
            went_down and self.outage_function
        ):  # Call the outage function outside the lock
            try:  # Calling it can fail
                self.outage_function()  # Handle the outage
            except Exception:  # Anything unexpected
                logger.exception(
                    "Could not handle the internet outage"
                )  # Log the details

    def confirm_down(self) -> bool:  # Check right now whether the internet is down
        works = check_internet_connection()  # Do a live check
        self.update_state(
            works
        )  # Update the shared state and handle the outage if needed
        return not works  # True when the internet is down

    def raise_if_down(self) -> None:  # Cheap check used before every request
        if not self.available.is_set():  # The monitor says the internet is down
            raise InternetOutageError(
                "The internet is down"
            )  # Make the restart logic wait for it

    def consume_outage_flag(
        self,
    ) -> bool:  # Tell whether an outage happened since the last restart
        with self.lock:  # Only one thread may change the state at a time
            flag, self.outage_occurred = (
                self.outage_occurred,
                False,
            )  # Read the flag and reset it
            return flag  # Give the old value back

    def wait_until_available(self) -> None:  # Block until the internet works
        waited = 0  # Counts how long we've waited
        while not self.available.wait(
            timeout=INTERNET_CHECK_INTERVAL_SECONDS
        ):  # Wait for the "works" signal
            waited += INTERNET_CHECK_INTERVAL_SECONDS  # Add the time we just waited
            logger.info(
                "Still waiting for the internet (%.0f seconds so far)", waited
            )  # Show progress
        if waited:  # Check whether we really had to wait
            logger.info(
                "The internet is back after about %.0f seconds", waited
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
    error: Exception,
) -> bool:  # Check whether an error means the browser is gone
    text = str(
        error
    ).lower()  # Lowercase the error text so the comparison ignores capitals
    return any(
        m in text for m in BROWSER_CLOSED_MESSAGES
    )  # True if any known phrase matches


class BrowserSession:  # Owns the browser and both tabs; can restart everything if the browser closes or crashes
    """Owns the browser and both tabs; can restart everything if the browser closes or crashes."""

    def __init__(
        self,
        internet_monitor: InternetMonitor,
    ):  # Setup
        self.internet_monitor = internet_monitor  # Tells us whether the internet works
        self.download_page: Optional[
            Page
        ] = None  # The temporary download tab, only set while it's open
        self.is_opening_download_tab = (
            False  # True for a moment while we open the download tab ourselves
        )
        self.invisible_playwright: Optional[
            InvisiblePlaywright
        ] = None  # The invisible_playwright launcher, once started
        self.connected_browser = None  # The Playwright Browser object
        self.context = None  # The browser context that owns both tabs
        self.results_page: Optional[
            Page
        ] = None  # Tab used for browsing the search results
        self.restart_count = 0  # How many times the browser had to be restarted

    def start(self) -> None:  # Start the browser and open the results tab
        logger.info("Starting the browser through invisible_playwright")  # Record it
        self.invisible_playwright = InvisiblePlaywright(
            extra_prefs={"pdfjs.disabled": True}
        )  # Prepare the launcher; without this Firefox shows PDFs in its viewer instead of downloading them
        self.connected_browser = (
            self.invisible_playwright.__enter__()
        )  # Launch the browser
        self.context = (
            self.connected_browser.new_context()
        )  # The context both tabs live in
        self.results_page = self.context.new_page()  # Open the first tab

        self.results_page.set_default_timeout(
            NAVIGATION_TIMEOUT_MS
        )  # Default time limit for page actions
        attach_browser_event_logging(
            self.results_page
        )  # Start logging everything the results tab does
        self.context.on(
            "page", self._close_unexpected_new_tab
        )  # Close any popup or extra tab the moment it opens
        self.results_page.bring_to_front()  # Show the results tab

    @staticmethod
    def _close_quietly(
        tab: Page,
    ) -> None:  # Close a tab, ignoring any error if it's already gone
        try:  # Closing can fail if the tab is already gone
            tab.close()  # Close the tab
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
        logger.info(
            "Closing an unexpected new tab: %s", new_tab.url
        )  # Record which tab is closed
        self._close_quietly(new_tab)  # Close it

    def close_unneeded_tabs(
        self,
    ) -> None:  # Close every tab except the results tab and the download tab
        for tab in list(self.context.pages):  # Look at every open tab
            if tab not in (
                self.results_page,
                self.download_page,
            ):  # A tab we don't need
                logger.info(
                    "Closing an unneeded tab: %s", tab.url
                )  # Record which tab is closed
                self._close_quietly(tab)  # Close it

    def handle_internet_outage(
        self,
    ) -> None:  # Called by the monitor thread when the internet goes down
        # Playwright's sync API must only be used from the thread that started it,
        # so the monitor thread cannot close the browser itself. The main thread
        # sees the outage at its next step (raise_if_down) and restart() closes it.
        logger.error(
            "The internet is down: the browser will be closed and restarted by the main thread"
        )  # Record it

    @contextmanager  # Turns the method below into something that can be used in a "with" block
    def temporary_download_tab(
        self,
    ) -> Iterator[Page]:  # Open a second tab only while downloads need it
        logger.info("Opening a temporary second tab for downloading")  # Record it
        self.is_opening_download_tab = (
            True  # Tell the popup guard that this new tab is wanted
        )
        try:  # Make sure the flag is always reset
            with self.context.expect_page(
                timeout=NAVIGATION_TIMEOUT_MS
            ) as new_tab_info:  # Wait for the new tab to appear
                self.results_page.evaluate(
                    "() => { window.open('about:blank', '_blank'); }"
                )  # Firefox opens this as a tab in the same window (context.new_page() opens a new window)
            download_page = new_tab_info.value  # The second tab
            self.results_page.bring_to_front()  # Keep showing the results tab, downloads work from the background tab
        finally:  # Always runs
            self.is_opening_download_tab = (
                False  # The popup guard may close unexpected tabs again
            )

        self.download_page = (
            download_page  # Remember it so the guards leave it alone while it's open
        )
        download_page.set_default_timeout(
            NAVIGATION_TIMEOUT_MS
        )  # Default time limit for it
        attach_browser_event_logging(
            download_page
        )  # Start logging everything the download tab does
        try:  # Make sure the tab is always closed again
            yield download_page  # Hand the tab to the code inside the "with" block
        finally:  # Always runs, even after an error
            self.download_page = None  # The download tab is going away
            logger.info("Closing the temporary download tab")  # Record it
            try:  # Closing can fail if the browser itself is gone
                download_page.close()  # Close the second tab
                self.results_page.bring_to_front()  # Show the results tab again
            except PlaywrightError:  # The tab or browser was already gone
                pass  # Nothing more to do

    def stop(self) -> None:  # Close the browser
        if self.invisible_playwright is not None:  # Check whether a browser was started
            try:  # The browser may already be dead
                self.invisible_playwright.__exit__(
                    None, None, None
                )  # Close the browser
            except Exception as e:  # Anything raised by a browser that is already gone
                logger.debug(
                    "Ignoring error while closing the browser: %s", e
                )  # Record it
        self.invisible_playwright = None  # Forget the old launcher
        self.connected_browser = None  # Forget the old browser
        self.context = None  # Forget the old context
        self.results_page = None  # Forget the old tab
        self.download_page = None  # Forget the old download tab

    def restart(
        self,
    ) -> None:  # Start a fresh browser after the old one closed or crashed
        if (
            self.internet_monitor.consume_outage_flag()
        ):  # Check whether this restart is only because the internet was down
            logger.info(
                "This restart is due to an internet outage, so it doesn't count towards the limit"
            )  # Say so
        else:  # The restart has another reason, such as a crash
            self.restart_count += 1  # Count this restart
            if (
                self.restart_count > MAX_BROWSER_RESTARTS
            ):  # Check whether we've restarted too often
                raise RuntimeError(
                    f"The browser closed more than {MAX_BROWSER_RESTARTS} times, giving up"
                )  # Stop the run
        logger.error(
            "Browser problem detected. Restarting (%d/%d)",
            self.restart_count,
            MAX_BROWSER_RESTARTS,
        )  # Record it
        self.stop()  # Close whatever is left of the old browser
        self.internet_monitor.wait_until_available()  # Don't start the browser until the internet works
        time.sleep(3)  # Give the system a moment to free resources
        self.start()  # Start a fresh browser with a new results tab
        warm_up_session_on_home_page(
            self.results_page
        )  # Visit the home page again to refresh the session


def run_with_browser_restarts(
    session: BrowserSession, description: str, step
) -> None:  # Run a step, restarting the browser if needed
    """Run a step, transparently restarting the browser (and retrying) on crash or internet outage."""
    while True:  # Keep trying until the step finishes or we give up
        try:  # The step may fail if the browser closes
            step()  # Run the step
            return  # The step finished, so we're done
        except InternetOutageError as e:  # The internet went down
            logger.warning("Internet outage during %s: %s", description, e)  # Record it
            session.restart()  # Wait for the internet, then start a fresh browser
        except PlaywrightError as e:  # A browser error happened
            if is_browser_closed_error(
                e
            ):  # Check whether the browser or its tab closed
                logger.error(
                    "Browser closed during %s: %s", description, str(e).splitlines()[0]
                )  # Log
                session.restart()  # Start a new browser, which raises an error if restarts ran out
            elif (
                session.internet_monitor.confirm_down()
            ):  # Check whether the internet being down is the cause
                logger.warning(
                    "The internet is down, so the error during %s is expected",
                    description,
                )  # Say so
                session.restart()  # Wait for the internet, then start a fresh browser
            else:  # Some other, real browser error
                logger.exception(
                    "Browser error during %s, skipping this step", description
                )  # Log the full error
                return  # Go on with the next step instead of crashing the whole run


# ============================================================== MAIN PROGRAM


def run_downloader() -> None:  # Run the whole job from start to finish
    configure_console_logging()  # Turn on console logging first so everything is shown
    logger.info("Download folder: %s", DOWNLOAD_FOLDER)  # Record where PDFs are saved
    logger.info(
        "Keywords: %s", " ".join(SEARCH_KEYWORDS)
    )  # Record which keywords will be searched

    DOWNLOAD_FOLDER.mkdir(exist_ok=True)  # Make sure the PDFs folder exists
    db = DownloadDatabase(DATABASE_FILE_PATH)  # Open the SQLite database
    db.register_existing_pdf_files(
        DOWNLOAD_FOLDER
    )  # Remember PDFs that are already in the folder
    db.ignore_non_usa_english_urls()  # Skip old pending URLs that aren't USA English

    stats = {
        "downloaded": 0,
        "skipped": 0,
        "unavailable": 0,
        "failed": 0,
        "consecutive_blocks": 0,
    }  # Running totals for the whole run

    try:  # Make sure the database is closed even if something fails
        internet = InternetMonitor()  # Prepare the background internet check
        session = BrowserSession(internet)  # Build the browser session
        try:  # Make sure the monitor and the browser are always stopped at the end
            internet.start(
                session.handle_internet_outage
            )  # Check the internet in the background
            internet.wait_until_available()  # Don't start the browser while the internet is down
            session.start()  # Start the browser with a single results tab
            if not warm_up_session_on_home_page(
                session.results_page
            ):  # Visit the home page and browse around first
                logger.warning(
                    "Warm-up did not finish cleanly, continuing anyway"
                )  # Record the problem

            for i, keyword in enumerate(
                SEARCH_KEYWORDS, start=1
            ):  # Go through a-z and 0-9
                if i > 1:  # Only pause between two keywords
                    pause_while_acting_like_a_person(
                        session.results_page,
                        *PAUSE_BETWEEN_KEYWORDS,
                        "before the next keyword",
                    )  # Move the mouse, scroll and press random letters instead of sitting still
                run_with_browser_restarts(  # Run the keyword, restarting the browser if it closes
                    session,
                    f"keyword '{keyword}'",  # The session and a description for log messages
                    lambda kw=keyword: collect_and_download_pdf_urls_for_keyword(
                        session, db, kw, stats
                    ),  # The work to do for this keyword
                )  # End of the restart wrapper

            logger.info("Retrying URLs that are still pending")  # Record the retry
            run_with_browser_restarts(  # Retry pending URLs, restarting the browser if it closes
                session,
                "the retry of pending URLs",  # The session and a description for log messages
                lambda: download_pdf_urls_in_temporary_tab(
                    session, db, db.load_unprocessed_urls(), stats
                ),  # The retry work, re-reading pending URLs each attempt
            )  # End of the restart wrapper
        finally:  # Always runs, even after an error
            internet.stop()  # Stop the background internet check
            session.stop()  # Close the browser
    except AccessBlockedError as e:  # The website blocked us too many times in a row
        logger.error("STOPPED EARLY: %s", e)  # Explain why the run ended
    except Exception:  # Anything unexpected that escaped the code above
        logger.exception(
            "Unexpected error, stopping"
        )  # Log the full error with its traceback
        raise  # Pass the error on so the script exits with an error code
    finally:  # Always runs at the very end
        log_run_statistics(stats, "FINAL")  # Show the final totals
        db.close()  # Close the database
        logger.info("Database closed, finished")  # Record the final step


if __name__ == "__main__":  # Only run when this file is executed directly
    run_downloader()  # Start the job
