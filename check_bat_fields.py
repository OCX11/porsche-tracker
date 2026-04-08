"""Show ALL fields returned by the BaT listings-filter API for a single item."""
import requests, re, json

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"})

r0 = s.get("https://bringatrailer.com/porsche/", timeout=20)
nonce = re.search(r'"restNonce"\s*:\s*"([^"]+)"', r0.text).group(1)

params = [
    ("base_filter[keyword_s]", "Porsche"),
    ("base_filter[items_type]", "make"),
    ("page", 1),
    ("per_page", 24),
    ("get_items", 1),
    ("get_stats", 0),
]
resp = s.get("https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter",
             params=params, headers={"X-WP-Nonce": nonce}, timeout=20)
data = resp.json()
item = data["items"][0]

print(f"ALL fields on first item ({item.get('title','')}):")
for k, v in sorted(item.items()):
    print(f"  {k:30s}: {repr(v)[:100]}")
