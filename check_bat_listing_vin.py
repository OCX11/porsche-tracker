"""Check if VIN is accessible on a BaT individual listing page."""
import requests, re
from bs4 import BeautifulSoup

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"})

url = "https://bringatrailer.com/listing/1995-porsche-911-carrera-4-cabriolet-38/"
r = s.get(url, timeout=20)
soup = BeautifulSoup(r.text, "lxml")

# Check listing details list (specs section)
print("=== Searching for VIN/Chassis in listing page ===")

# BaT shows specs in a <ul class="listing-essentials"> or similar
for selector in ["ul.listing-essentials li", ".listing-essentials li", ".essentials li", "li"]:
    items = soup.select(selector)
    for item in items:
        txt = item.get_text(" ", strip=True)
        if any(k in txt.lower() for k in ["vin", "chassis", "serial"]):
            print(f"  [{selector}] {txt[:120]}")

# Also check for VIN in page JSON/script
vin_m = re.search(r'"vin"\s*:\s*"([^"]+)"', r.text)
chassis_m = re.search(r'[Cc]hassis[:\s#]+([A-Z0-9]{6,17})', r.text)
vin_pattern = re.search(r'\b(WP0[A-Z0-9]{14})\b', r.text)  # Porsche VINs start with WP0

print(f"\nVIN in JSON: {vin_m.group(1) if vin_m else 'not found'}")
print(f"Chassis text: {chassis_m.group(1) if chassis_m else 'not found'}")
print(f"WP0 VIN pattern: {vin_pattern.group(1) if vin_pattern else 'not found'}")

# Check excerpt/description for VIN mention
excerpt = soup.select_one(".post-excerpt, .listing-excerpt, .the-content")
if excerpt:
    txt = excerpt.get_text(" ", strip=True)
    if any(k in txt.lower() for k in ["vin", "chassis"]):
        print(f"\nIn description: {txt[:300]}")
