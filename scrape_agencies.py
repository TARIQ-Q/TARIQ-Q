#!/usr/bin/env python3
"""Scrape agency listings from https://my.gov.sa/ar/agencies using Playwright."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import pandas as pd
from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

BASE_URL = "https://my.gov.sa"
START_PATH = "/ar/agencies"
DETAIL_PATH_PATTERN = re.compile(r"^/ar/agencies/[^/?#]+/?$")
CARD_ANCHOR_SELECTOR = 'a[href^="/ar/agencies/"], a[href^="https://my.gov.sa/ar/agencies/"]'
CARD_ANCHOR_JS_SELECTOR = 'a[href*="/agencies/"]'

logger = logging.getLogger(__name__)


def normalize_text(value: Optional[str]) -> Optional[str]:
    """Collapse whitespace and strip surrounding spaces."""

    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized or None


async def wait_for_cards(page: Page, timeout: int) -> None:
    """Wait until at least one agency card anchor is present."""

    await page.wait_for_selector(CARD_ANCHOR_SELECTOR, timeout=timeout)


async def extract_cards(page: Page, seen_urls: Set[str]) -> List[Dict[str, Optional[str]]]:
    """Extract agency cards (name, detail URL, category) from the current page."""

    records: List[Dict[str, Optional[str]]] = []
    anchors = await page.locator(CARD_ANCHOR_SELECTOR).element_handles()

    for anchor in anchors:
        href = await anchor.get_attribute("href")
        if not href:
            continue

        absolute_url = urljoin(BASE_URL, href)
        parsed = urlparse(absolute_url)
        if not DETAIL_PATH_PATTERN.match(parsed.path):
            continue

        if absolute_url in seen_urls:
            continue

        info = await anchor.evaluate(
            """
            (node) => {
                const result = {
                    name: node.innerText ? node.innerText.trim().replace(/\s+/g, ' ') : ''
                };

                const card = node.closest('[class*="card"], [class*="Card"], article, li, div');
                if (card) {
                    const candidates = card.querySelectorAll('[class*="category"], [class*="Category"], [class*="sector"], [class*="Sector"], [class*="tag"], [class*="Tag"], .badge, .label');
                    for (const el of candidates) {
                        if (el === node) {
                            continue;
                        }
                        const text = el.innerText ? el.innerText.trim().replace(/\s+/g, ' ') : '';
                        if (text && text.length <= 120) {
                            result.category = text;
                            break;
                        }
                    }
                }

                return result;
            }
            """
        )

        name = normalize_text(info.get("name") if isinstance(info, dict) else None)
        if not name:
            continue

        category = normalize_text(info.get("category")) if isinstance(info, dict) else None

        records.append(
            {
                "name": name,
                "detail_url": absolute_url,
                "category": category if category else None,
            }
        )
        seen_urls.add(absolute_url)

    return records


async def is_locator_disabled(locator: Locator) -> bool:
    """Determine whether the locator (pagination control) is disabled."""

    try:
        return await locator.is_disabled()
    except Exception:
        pass

    try:
        return await locator.evaluate(
            """
            (el) => {
                if (!el) {
                    return true;
                }
                if (el.hasAttribute('disabled')) {
                    return true;
                }
                const ariaDisabled = el.getAttribute('aria-disabled');
                if (ariaDisabled && ariaDisabled.toLowerCase() === 'true') {
                    return true;
                }
                const classList = el.classList ? Array.from(el.classList) : [];
                return classList.some(cls => cls.toLowerCase().includes('disabled'));
            }
            """
        )
    except Exception:
        return False


async def find_next_button(page: Page) -> Optional[Locator]:
    """Locate the pagination control for the next page, if available."""

    candidates = [
        page.get_by_role("link", name=re.compile("التالي|Next", re.IGNORECASE)),
        page.get_by_role("button", name=re.compile("التالي|Next", re.IGNORECASE)),
        page.locator('a[rel="next"]'),
        page.locator('[aria-label="التالي"]'),
        page.locator('[aria-label="Next"]'),
    ]

    for locator in candidates:
        try:
            count = await locator.count()
        except Exception:
            continue

        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                if await is_locator_disabled(candidate):
                    continue
                return candidate
            except Exception:
                continue

    return None


async def wait_for_new_cards(page: Page, known_urls: Set[str], timeout_ms: int) -> None:
    """Wait until at least one new agency link appears or timeout occurs."""

    known_list = list(known_urls)
    try:
        await page.wait_for_function(
            """
            ({ selector, known, detailPattern }) => {
                const detailRegex = new RegExp(detailPattern);
                const anchors = Array.from(document.querySelectorAll(selector));
                return anchors.some(anchor => {
                    const href = anchor.getAttribute('href');
                    if (!href) {
                        return false;
                    }
                    let url;
                    try {
                        url = new URL(href, window.location.origin);
                    } catch (err) {
                        return false;
                    }
                    return detailRegex.test(url.pathname) && !known.includes(url.href);
                });
            }
            """,
            {
                "selector": CARD_ANCHOR_JS_SELECTOR,
                "known": known_list,
                "detailPattern": DETAIL_PATH_PATTERN.pattern,
            },
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        logger.debug("Timeout waiting for new cards; proceeding regardless.")


async def enrich_with_details(
    context,
    records: List[Dict[str, Optional[str]]],
    navigation_timeout: int,
    delay_ms: int,
) -> None:
    """Optionally visit agency detail pages to enrich contact information."""

    if not records:
        return

    detail_page = await context.new_page()
    detail_page.set_default_navigation_timeout(navigation_timeout)
    detail_page.set_default_timeout(navigation_timeout)

    contact_email_selector = 'a[href^="mailto:"]'
    contact_phone_selector = 'a[href^="tel:"]'

    for record in records:
        record.setdefault("contact_email", None)
        record.setdefault("contact_phone", None)

    for idx, record in enumerate(records, start=1):
        detail_url = record.get("detail_url")
        if not detail_url:
            continue

        try:
            await detail_page.goto(detail_url, wait_until="networkidle")
        except PlaywrightTimeoutError:
            logger.warning("Timed out loading detail page %s", detail_url)
            continue
        except Exception as exc:
            logger.warning("Error loading detail page %s: %s", detail_url, exc)
            continue

        email_locator = detail_page.locator(contact_email_selector)
        try:
            if await email_locator.count():
                email_href = await email_locator.first.get_attribute("href")
                record["contact_email"] = normalize_text(email_href.split(":", 1)[-1]) if email_href else None
        except Exception:
            logger.debug("Failed to extract email from %s", detail_url)

        phone_locator = detail_page.locator(contact_phone_selector)
        try:
            if await phone_locator.count():
                phone_href = await phone_locator.first.get_attribute("href")
                record["contact_phone"] = normalize_text(phone_href.split(":", 1)[-1]) if phone_href else None
        except Exception:
            logger.debug("Failed to extract phone from %s", detail_url)

        logger.debug("Enriched detail page %s (%d/%d)", detail_url, idx, len(records))

        if delay_ms > 0:
            await detail_page.wait_for_timeout(delay_ms)

    await detail_page.close()


async def run_scraper(args: argparse.Namespace) -> Dict[str, int]:
    """Execute the scraping workflow and return summary statistics."""

    summary = {"pages_crawled": 0, "cards_collected": 0}
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        context = await browser.new_context(locale="ar-SA")
        context.set_default_timeout(args.playwright_timeout)
        context.set_default_navigation_timeout(args.playwright_timeout)

        page = await context.new_page()
        await page.goto(urljoin(BASE_URL, START_PATH), wait_until="networkidle")
        await wait_for_cards(page, args.playwright_timeout)

        seen_urls: Set[str] = set()
        all_records: List[Dict[str, Optional[str]]] = []

        while True:
            summary["pages_crawled"] += 1
            page_records = await extract_cards(page, seen_urls)
            if page_records:
                all_records.extend(page_records)
                summary["cards_collected"] = len(all_records)
                logger.info(
                    "Page %d: captured %d new agencies (total %d)",
                    summary["pages_crawled"],
                    len(page_records),
                    len(all_records),
                )
            else:
                logger.warning(
                    "Page %d produced no new agencies (might be duplicate or selectors need review)",
                    summary["pages_crawled"],
                )

            if args.max_pages and summary["pages_crawled"] >= args.max_pages:
                logger.info("Reached max page limit (%d)", args.max_pages)
                break

            next_button = await find_next_button(page)
            if not next_button:
                logger.info("No further pagination controls detected; stopping.")
                break

            try:
                await next_button.scroll_into_view_if_needed()
            except Exception:
                logger.debug("Scrolling next button into view failed; continuing regardless.")

            try:
                await next_button.click()
            except Exception as exc:
                logger.warning("Failed to click pagination control: %s", exc)
                break

            try:
                await page.wait_for_load_state("networkidle")
            except PlaywrightTimeoutError:
                logger.debug("Pagination load did not reach network idle; continuing.")

            await wait_for_cards(page, args.playwright_timeout)
            await wait_for_new_cards(page, seen_urls, args.page_transition_timeout)

            if args.delay_between_pages > 0:
                await page.wait_for_timeout(int(args.delay_between_pages * 1000))

        if args.fetch_details:
            logger.info("Fetching optional detail page fields (email/phone)...")
            await enrich_with_details(
                context,
                all_records,
                args.playwright_timeout,
                int(args.delay_between_details * 1000),
            )

        await browser.close()

    if not all_records:
        logger.warning("No agency data collected; skipping export.")
        return summary

    df = pd.DataFrame(all_records)
    csv_path = output_dir / "agencies.csv"
    excel_path = output_dir / "agencies.xlsx"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_excel(excel_path, index=False)

    logger.info("Saved CSV to %s", csv_path)
    logger.info("Saved Excel to %s", excel_path)

    print(
        f"Crawled {summary['pages_crawled']} page(s); collected {summary['cards_collected']} unique agency cards."
    )

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape agency listings from my.gov.sa using Playwright",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to save agencies.csv and agencies.xlsx (default: current directory)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit on the number of pages to crawl.",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run the browser in headed mode for debugging.",
    )
    parser.set_defaults(headless=True)
    parser.add_argument(
        "--fetch-details",
        action="store_true",
        help="Visit each agency detail page to capture additional contact fields (email/phone).",
    )
    parser.add_argument(
        "--delay-between-pages",
        type=float,
        default=0.5,
        help="Delay (seconds) to wait after each page navigation.",
    )
    parser.add_argument(
        "--delay-between-details",
        type=float,
        default=0.5,
        help="Delay (seconds) between visiting detail pages when --fetch-details is enabled.",
    )
    parser.add_argument(
        "--playwright-timeout",
        type=int,
        default=15000,
        help="Default timeout in milliseconds for Playwright actions.",
    )
    parser.add_argument(
        "--page-transition-timeout",
        type=int,
        default=20000,
        help="Timeout in milliseconds to wait for new cards after clicking pagination.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging output.",
    )

    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    try:
        asyncio.run(run_scraper(args))
    except KeyboardInterrupt:
        logger.warning("Scraper interrupted by user.")


if __name__ == "__main__":
    main()

