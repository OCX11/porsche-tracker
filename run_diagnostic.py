"""
Diagnostic runner for a subset of scrapers.
Writes full results to logs/scraper_diagnostic.txt — nothing to stdout.
"""
import traceback
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

# Silence the urllib3 warning
import warnings
warnings.filterwarnings("ignore")

import scraper as sc

LOG = Path(__file__).parent / "logs" / "scraper_diagnostic.txt"
LOG.parent.mkdir(exist_ok=True)

TARGETS = [
    ("Bring a Trailer",       sc.scrape_bat),
    ("PCA Mart",              sc.scrape_pcamart),
    ("pcarmarket",            sc.scrape_pcarmarket),
    ("classic.com",           sc.scrape_classic),
    ("cars.com",              sc.scrape_carscom),
    ("AutoTrader",            sc.scrape_autotrader),
    ("eBay Motors",           sc.scrape_ebay),
    ("UDrive Automobiles",    sc.scrape_udriveautomobiles),
    ("Motorcars of the Main Line", sc.scrape_motorcarsofthemainline),
]

# Raw HTML probe: fetch the primary URL for each source directly
RAW_URLS = {
    "Bring a Trailer":       "https://bringatrailer.com/porsche/",
    "PCA Mart":              "https://mart.pca.org",
    "pcarmarket":            "https://www.pcarmarket.com/listings/?make=Porsche",
    "classic.com":           "https://www.classic.com/search/?make=Porsche&status=active",
    "cars.com":              "https://www.cars.com/shopping/results/?makes[]=porsche&page_size=5&page=1",
    "AutoTrader":            "https://www.autotrader.com/cars-for-sale/porsche",
    "eBay Motors":           "https://www.ebay.com/sch/i.html?_nkw=porsche&_sacat=6001&LH_BIN=1&_pgn=1",
    "UDrive Automobiles":    "https://www.udriveautomobiles.co/custom-12?make=Porsche",
    "Motorcars of the Main Line": "https://www.motorcarsofthemainline.com/all-inventory/?make=Porsche",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _raw_probe(name):
    url = RAW_URLS.get(name)
    if not url:
        return "  raw probe: no URL defined\n"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
        snippet = r.text[:500].replace("\n", " ").replace("\r", "")
        return (
            f"  raw GET status : {r.status_code}\n"
            f"  raw GET length : {len(r.text)} bytes\n"
            f"  raw GET snippet: {snippet}\n"
        )
    except Exception as e:
        return f"  raw GET error  : {e}\n"


def run():
    lines = []
    lines.append("=" * 70)
    lines.append(f"SCRAPER DIAGNOSTIC  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    for name, fn in TARGETS:
        lines.append("-" * 70)
        lines.append(f"SOURCE: {name}")
        lines.append("-" * 70)

        # 1. Raw HTTP probe (independent of scraper logic)
        lines.append(_raw_probe(name))

        # 2. Run the scraper
        t0 = time.time()
        result_count = 0
        valid_count = 0
        error_info = None
        sample = []

        try:
            raw = fn()
            result_count = len(raw)
            valid = [c for c in raw if sc._is_valid_listing(c)]
            valid_count = len(valid)
            sample = valid[:3]
        except Exception:
            error_info = traceback.format_exc()

        elapsed = time.time() - t0

        lines.append(f"  scraper elapsed : {elapsed:.1f}s")
        lines.append(f"  raw returned    : {result_count}")
        lines.append(f"  valid listings  : {valid_count}")

        if error_info:
            lines.append("  EXCEPTION:")
            for eline in error_info.strip().splitlines():
                lines.append(f"    {eline}")
        elif result_count == 0:
            lines.append("  STATUS: returned 0 results (no exception)")
        else:
            lines.append("  STATUS: OK")
            lines.append(f"  sample ({min(3,valid_count)} of {valid_count} valid):")
            for c in sample:
                lines.append(
                    f"    {c.get('year')} {c.get('make')} {c.get('model')} "
                    f"{c.get('trim') or ''!r} | "
                    f"${c.get('price') or 'N/A'} | "
                    f"{c.get('mileage') or '?'}mi | "
                    f"vin={c.get('vin')} | "
                    f"{(c.get('url') or '')[:70]}"
                )

        lines.append("")
        time.sleep(2)   # brief pause between scrapers

    lines.append("=" * 70)
    lines.append("DONE")
    lines.append("=" * 70)

    LOG.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    run()
    # Only message to terminal: where the file landed
    print(f"Diagnostic written to {LOG}")
