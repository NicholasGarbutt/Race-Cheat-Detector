#!/usr/bin/env python3
"""
scrape_splits_playwright.py

Same job as scrape_splits.py (build a wide-format CSV of every
runner's split times) but drives a real Chromium browser via
Playwright instead of raw HTTP requests, because the site is
returning 403s to plain requests (likely a bot-detection layer).

SETUP (once)
------------
    pip install playwright beautifulsoup4
    playwright install chromium

RECOMMENDED WORKFLOW FOR AN UNATTENDED 5-HOUR RUN
---------------------------------------------------
1. Before you leave, run a small HEADED test so you can see the
   browser and manually clear any "checking your browser" / captcha
   page if one appears. This also saves the resulting session
   (cookies) to a local file so future runs don't need you again:

       python scrape_splits_playwright.py maindata.csv splits_output.csv --test 5 --headed

   Watch the browser window. If it shows a verification page, solve
   it - the script waits up to 90 seconds for that. Once it's past
   it and prints results for the 5 test runners, you're good.

2. Kick off the real run, headless (no visible window), reusing the
   saved session:

       python scrape_splits_playwright.py maindata.csv splits_output.csv --workers 3

   Leave it running. It writes to splits_output.csv incrementally
   and is resumable (see below), so it's safe to interrupt.

NOTES
-----
- Session cookies can expire after a while. If, partway through, you
  start seeing a lot of [RETRY LATER] / no-data results in a row,
  the session likely needs refreshing - re-run step 1 (headed) to
  get a fresh one, then resume step 2.
- Keep --workers modest (2-4). This is a real browser tab per
  in-flight request, so it's heavier than plain HTTP - going higher
  won't necessarily be faster and is more likely to get you blocked.
- Resuming: completed bibs are recorded in <output>.done.txt, same
  as the requests-based version. Re-running the same command skips
  them and continues.
"""

import argparse
import asyncio
import csv
import random
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

LINK_COLUMN = "tablescraper-selected-row href"
NAME_COLUMN = "tablescraper-selected-row 2"
TIME_COLUMN = "tablescraper-selected-row 3"
COUNTRY_COLUMN = "d-none"
AGE_GROUP_COLUMN = "d-none 3"

BIB_RE = re.compile(r"#(\d+)")

RESULTS_HOME_URL = "https://www.multisportaustralia.com.au/races/sydney-marathon-2026/events/1/results/individuals"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "verify you are human",
    "cf-chl",
)


def extract_bib(name_field):
    m = BIB_RE.search(name_field or "")
    return m.group(1) if m else None


def parse_splits_table(html):
    soup = BeautifulSoup(html, "html.parser")

    target_table = None
    header_cells = None
    header_row_index = None

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for idx, row in enumerate(rows):
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            lowered = [c.lower() for c in cells]
            if "location" in lowered and "split" in lowered:
                target_table = table
                header_cells = cells
                header_row_index = idx
                break
        if target_table is not None:
            break

    if target_table is None:
        return [], {}

    col_index = {name: i for i, name in enumerate(header_cells)}
    loc_i = col_index.get("Location")
    racetime_i = col_index.get("Race Time")

    locations = []
    splits = {}
    all_rows = target_table.find_all("tr")
    for row in all_rows[header_row_index + 1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) <= max(loc_i, racetime_i):
            continue
        location = cells[loc_i]
        race_time = cells[racetime_i]
        if not location:
            continue
        locations.append(location)
        splits[location] = race_time

    return locations, splits


def looks_like_challenge(html):
    lowered = html.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


async def wait_for_challenge_clear(page, max_wait_s):
    for i in range(max_wait_s):
        html = await page.content()
        if not looks_like_challenge(html):
            return True, html
        if i == 0:
            print(f"  Verification page detected - waiting up to {max_wait_s}s "
                  f"(solve it in the browser window if one is visible)...")
        await asyncio.sleep(1)
    return False, await page.content()


async def fetch_page(context, url, retries=3, nav_timeout_ms=30000):
    last_err = None
    saw_500 = False
    for attempt in range(retries):
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            status = resp.status if resp else None
            if status == 500:
                saw_500 = True
                last_err = "HTTP 500"
                await page.close()
            elif status and status != 200:
                last_err = f"HTTP {status}"
                await page.close()
            else:
                clear, html = await wait_for_challenge_clear(page, max_wait_s=5)
                await page.close()
                if clear:
                    return "ok", html, None
                last_err = "verification page did not clear"
        except Exception as e:
            last_err = str(e)
            try:
                await page.close()
            except Exception:
                pass
        await asyncio.sleep(1.5 * (attempt + 1) + random.random())

    if saw_500:
        return "no_data", None, last_err
    return "transient", None, last_err


def base_row(bib, row, status, locations):
    out_row = {
        "bib": bib,
        "name": row.get(NAME_COLUMN, "").strip(),
        "country": row.get(COUNTRY_COLUMN, "").strip(),
        "age_group": row.get(AGE_GROUP_COLUMN, "").strip(),
        "overall_time": row.get(TIME_COLUMN, "").strip(),
        "status": status,
    }
    for loc in locations:
        out_row[loc] = ""
    return out_row


def load_done_bibs(done_path):
    try:
        with open(done_path, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


async def main_async(args):
    done_path = args.output_csv + ".done.txt"
    done_bibs = load_done_bibs(done_path)

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if args.test:
        rows = rows[: args.test]

    jobs = []
    for row in rows:
        bib = extract_bib(row.get(NAME_COLUMN, ""))
        link = row.get(LINK_COLUMN, "").strip()
        if not bib or not link or bib in done_bibs:
            continue
        jobs.append((bib, row))

    print(f"{len(rows)} rows read, {len(jobs)} still to scrape "
          f"({len(done_bibs)} already done).")
    if not jobs:
        print("Nothing to do.")
        return

    state_path = Path(args.state_file)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context_kwargs = {"user_agent": USER_AGENT}
        if state_path.exists():
            context_kwargs["storage_state"] = str(state_path)
            print(f"Reusing saved session from {state_path}")
        context = await browser.new_context(**context_kwargs)

        # Warm-up / challenge-clearing visit to the results home page.
        page = await context.new_page()
        await page.goto(RESULTS_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        cleared, _ = await wait_for_challenge_clear(page, max_wait_s=90 if args.headed else 20)
        await page.close()
        await context.storage_state(path=str(state_path))

        if not cleared:
            print("Could not confirm the verification page cleared. "
                  "If you're running headless, try again with --headed so "
                  "you can solve it manually once, then re-run headless.")
            if not args.headed:
                await browser.close()
                sys.exit(1)

        # Probe checkpoint columns from the first job that succeeds.
        locations = []
        probe_idx = 0
        while probe_idx < len(jobs):
            bib, row = jobs[probe_idx]
            print(f"Probing checkpoint columns using bib #{bib} ...")
            status, html, err = await fetch_page(context, row[LINK_COLUMN])
            if status == "ok":
                locations, _ = parse_splits_table(html)
                if locations:
                    break
                print(f"  bib #{bib}: page loaded but no splits table found, trying next")
            else:
                print(f"  bib #{bib}: {status} ({err}), trying next")
            probe_idx += 1

        if not locations:
            print("Could not find a splits table on any of the probed runners' "
                  "pages. The page structure may differ from expected - open "
                  "one runner's page in a normal browser, view source, and "
                  "compare against parse_splits_table().")
            await browser.close()
            sys.exit(1)

        print(f"Checkpoints found: {locations}")
        fieldnames = ["bib", "name", "country", "age_group", "overall_time", "status"] + locations

        write_header = True
        try:
            with open(args.output_csv, "r", newline="", encoding="utf-8") as f:
                if f.readline():
                    write_header = False
        except FileNotFoundError:
            pass

        out_f = open(args.output_csv, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            out_f.flush()

        sem = asyncio.Semaphore(args.workers)
        completed = 0
        errors = 0
        no_data = 0
        start = time.time()

        async def worker(job):
            nonlocal completed, errors, no_data
            bib, row = job
            async with sem:
                await asyncio.sleep(args.delay + random.random() * args.delay)
                status, html, err = await fetch_page(context, row[LINK_COLUMN])

                if status == "transient":
                    errors += 1
                    print(f"  [RETRY LATER] bib #{bib}: {err}")
                    return

                if status == "no_data":
                    out_row = base_row(bib, row, "DID_NOT_START", locations)
                elif status == "ok":
                    locs, splits = parse_splits_table(html)
                    if not locs:
                        out_row = base_row(bib, row, "NO_SPLITS_FOUND", locations)
                    else:
                        out_row = base_row(bib, row, "OK", locations)
                        for loc in locations:
                            out_row[loc] = splits.get(loc, "")
                else:
                    errors += 1
                    return

                writer.writerow(out_row)
                out_f.flush()
                with open(done_path, "a") as f:
                    f.write(bib + "\n")
                completed += 1
                if out_row["status"] != "OK":
                    no_data += 1
                    print(f"  [{out_row['status']}] bib #{bib}")
                if completed % 25 == 0:
                    elapsed = time.time() - start
                    rate = completed / elapsed if elapsed else 0
                    remaining = len(jobs) - completed - errors
                    eta_min = (remaining / rate / 60) if rate else float("inf")
                    print(f"  {completed}/{len(jobs)} recorded ({no_data} no-data), "
                          f"{errors} to retry, ~{rate:.2f}/s, ETA {eta_min:.0f} min")
                    # Periodically refresh the saved session in case it
                    # rotates cookies during a long run.
                    await context.storage_state(path=str(state_path))

        await asyncio.gather(*(worker(job) for job in jobs))

        out_f.close()
        await context.storage_state(path=str(state_path))
        await browser.close()

        print(f"Finished. {completed} recorded ({no_data} marked as "
              f"DID_NOT_START/NO_SPLITS_FOUND), {errors} left to retry. "
              f"Output: {args.output_csv}")
        if errors:
            print("Re-run the same command to retry transient failures.")


def main():
    ap = argparse.ArgumentParser(description="Scrape split times using a real Playwright-controlled browser.")
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    ap.add_argument("--workers", type=int, default=3, help="Concurrent browser tabs (default 3, keep modest)")
    ap.add_argument("--delay", type=float, default=0.5, help="Extra delay before each navigation (default 0.5s)")
    ap.add_argument("--test", type=int, default=0, help="Only process the first N runners")
    ap.add_argument("--headed", action="store_true", help="Show the browser window (use for the first run to clear any challenge manually)")
    ap.add_argument("--state-file", default="cf_state.json", help="Where to save/reuse the browser session (default cf_state.json)")
    args = ap.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()