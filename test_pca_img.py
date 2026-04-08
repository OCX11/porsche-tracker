"""Quick test: fetch one PCA Mart image and show what we get."""
import sys
from pathlib import Path

# Get a real image URL from the DB
sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn

conn = get_conn()
rows = conn.execute("""
    SELECT image_url, listing_url FROM listings
    WHERE dealer='PCA Mart' AND image_url IS NOT NULL
    LIMIT 5
""").fetchall()
conn.close()

if not rows:
    print("No PCA Mart listings in DB")
    sys.exit(1)

print("DB image_url values:")
for r in rows:
    print(" ", r[0])

# Try to reconstruct the original remote URL from the hash — we can't.
# Instead, re-scrape one listing page via requests to get the image URL.
print()

# Try fetching the image directly with requests
import requests
img_url = rows[0][0]
listing_url = rows[0][1]

print(f"Testing image URL: {img_url}")
print(f"Listing URL: {listing_url}")
print()

if img_url.startswith("/static/img_cache/"):
    print("Image URL is a cached path — need to scrape listing page to get original URL")
    print()
    print("Fetching listing page to find real image URL...")
    try:
        r = requests.get(listing_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=15, proxies={})
        print(f"  Listing page status: {r.status_code}")
        print(f"  Content-Type: {r.headers.get('Content-Type','?')}")
        # Look for image URLs in the page
        import re
        imgs = re.findall(r'https://mart\.pca\.org/includes/images/ads/[^\s"\'<>]+', r.text)
        imgs += re.findall(r'/includes/images/ads/[^\s"\'<>]+', r.text)
        print(f"  Found image URLs: {imgs[:5]}")
        if imgs:
            test_url = imgs[0] if imgs[0].startswith("http") else "https://mart.pca.org" + imgs[0]
            print(f"\nTesting direct fetch of: {test_url}")
            r2 = requests.get(test_url,
                             headers={"Referer": "https://mart.pca.org/",
                                      "User-Agent": "Mozilla/5.0"},
                             timeout=15, proxies={})
            print(f"  Status: {r2.status_code}")
            print(f"  Content-Type: {r2.headers.get('Content-Type','?')}")
            print(f"  Content length: {len(r2.content)} bytes")
            print(f"  First bytes: {r2.content[:20]}")
    except Exception as e:
        print(f"  Error: {e}")
else:
    print("Testing direct fetch with requests...")
    try:
        r = requests.get(img_url,
                        headers={"Referer": "https://mart.pca.org/",
                                 "User-Agent": "Mozilla/5.0"},
                        timeout=15, proxies={})
        print(f"  Status: {r.status_code}")
        print(f"  Content-Type: {r.headers.get('Content-Type','?')}")
        print(f"  Content length: {len(r.content)} bytes")
        print(f"  First bytes: {r.content[:20]}")
    except Exception as e:
        print(f"  Error: {e}")

print()
print("Now testing via Playwright (with session cookies)...")
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print("  Loading mart.pca.org to get session cookies...")
        page.goto("https://mart.pca.org", timeout=30000)
        print(f"  Page title: {page.title()}")

        # Try fetching an image via context.request
        listing_r = page.goto(listing_url, timeout=30000)
        print(f"  Listing page title: {page.title()}")

        # Find image on page
        import re
        imgs = re.findall(r'https://mart\.pca\.org/includes/images/ads/[^\s"\'<>]+', page.content())
        imgs += ["https://mart.pca.org" + x for x in re.findall(r'/includes/images/ads/[^\s"\'<>]+', page.content())]
        print(f"  Images found on page: {imgs[:3]}")

        if imgs:
            test_url = imgs[0]
            print(f"\n  Fetching via context.request: {test_url}")
            resp = page.context.request.get(test_url, headers={"Referer": "https://mart.pca.org/"})
            body = resp.body()
            ct = resp.headers.get("content-type", "?")
            print(f"  Status: {resp.status}")
            print(f"  Content-Type: {ct}")
            print(f"  Body length: {len(body)} bytes")
            print(f"  First bytes: {body[:20]}")

        browser.close()
except Exception as e:
    print(f"  Playwright error: {e}")
    import traceback; traceback.print_exc()
