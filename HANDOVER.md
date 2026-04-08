# Vehicle Market Analyzer — Project Handover Summary
*Last updated: April 4, 2026*

---

## 1. Project Overview & Goals

A Porsche-focused market intelligence platform running autonomously on a Mac Mini M4. It scrapes inventory from 8+ active sources, tracks price history, classifies listings by tier, scores every listing against FMV using 5,600+ BaT sold comps, and sends iMessage deal alerts with link previews when relevant listings appear. The long-term goal is to become the most informed buyer in the air-cooled, water-cooled, and GT Porsche market — finding deals before other dealers and buying right every time.

### Business Context
Owner operates a small, focused performance car dealership. All purchases are evaluated as investments — short-term flips or long-term holds. Core price range: $70K–$150K. GT/collector cars have no ceiling. Every car bought must have a credible path to making money.

### Success Criteria
- Alert on any Tier 1 (GT/Collector) listing the moment it appears anywhere
- Alert on Tier 2 (Standard) listings only when 10%+ below verified market value
- Accurately estimate FMV for any target car within 5% using sold comp data
- Know competitor dealer inventory, pricing patterns, and turn times cold
- Weekly/monthly reports that predict segment price direction with documented accuracy

---

## 2. Target Vehicles

### Tier 1 — GT / Collector (alert at DEAL or WATCH)
- Porsche 911: GT3, GT3 RS, GT2, GT2 RS, R, Speedster, Sport Classic, Touring (996/997/991/992)
- Porsche 911: All air-cooled generations — 930, 964, 993 (pre-1998)
- Porsche Cayman: GT4, GT4 RS, Spyder, R (987/981/718)
- Porsche Boxster: Spyder (987/981/718)
- Any Turbo S variant
- 356, 914-6 (rare, flag immediately)

### Tier 2 — Standard (alert only at DEAL — 10%+ below FMV)
- Porsche 911: Base Carrera, S, 4S, GTS, Targa (996/997/991/992)
- Porsche Cayman: S, GTS (987/981/718)
- Porsche Boxster: Base, S, GTS (987/981/718)

### Never
- Cayenne, Panamera, Macan, Taycan — excluded at scrape level
- Salvage, rebuilt, flood, or frame damage — excluded
- Year range: 1986–2026
- Mileage: under 100,000 miles
- Price: under $5,000 (filtered at ingest — exempts auctions)

---

## 3. System Architecture

### Infrastructure
- **Machine:** Mac Mini M4, running 24/7, user: claw
- **Alert delivery:** iMessage via `notify_imessage.py` (AppleScript → Messages.app) — LIVE ✅
- **Alert format:** One iMessage per car. Link preview shows photo automatically. No separate image message.
- **New-listing alerts:** `notify_new_listings()` fires for every new listing the moment it hits the DB — no FMV threshold. Runs before deal-scoring alerts each cycle.
- **Scheduler:** launchd plists — survive reboots
- **Main scrape schedule:** Peak (7AM–11PM) every 12 min; off-peak every 60 min (`com.porschetracker.scrape`)
- **Distill poller:** runs every 60s polling Distill Web Monitor (`com.porschetracker.distill-poller`)

### Key Files
```
~/porsche-tracker/
├── scraper.py              # BaT, PCA Mart, pcarmarket scrapers + DEALERS list
├── scraper_autotrader.py   # AutoTrader Playwright scraper (mobile site) — LIVE ✅
├── scraper_carscom.py      # Cars.com Playwright scraper — LIVE ✅
├── scraper_ebay.py         # eBay Browse API scraper — LIVE ✅
├── scraper_rennlist.py     # Rennlist Playwright scraper — LIVE ✅ (April 4)
├── distill_poller.py       # Polls Distill Web Monitor → inserts BfB only (all others migrated)
├── db.py                   # Database layer, tier classification, upsert logic
├── main.py                 # Entry point — scrape + snapshot + dashboards + reports + alerts
├── fmv.py                  # FMV engine — weighted median, deal scoring, generation bucketing
├── notify_imessage.py      # iMessage deal alerts — LIVE (NOTIFICATIONS_ENABLED=True)
├── new_dashboard.py        # Primary dashboard → docs/index.html (GitHub Pages)
├── comp_scraper.py         # Ongoing sold comp scraping (BaT, Cars & Bids)
├── backfill_comps.py       # BaT historical backfill (paginated, resumable)
├── enrich_bat_vins.py      # Visits BaT listing pages: VIN, mileage, transmission, engine, color
├── enrich_ebay_mileage.py  # On-demand eBay mileage/VIN enricher (Browse API per-item)
├── decode_vin_generation.py # Decodes VINs → generation column on sold_comps
├── report.py               # Market report generator
├── daily_report.py         # Daily sold/unsold summary
├── weekly_report.py        # Weekly price movement by segment
├── monthly_report.py       # Monthly trends + predictions
└── data/
    ├── inventory.db              # SQLite — listings, price_history, sold_comps, snapshots
    ├── imessage_config.json      # {"recipient": "6108361111"} — CONFIGURED ✅
    ├── seen_alerts_imessage.json # Dedup store for iMessage alerts
    ├── proxy_config.json         # DataImpulse proxy (gw.dataimpulse.com:823)
    ├── autotrader_state.json     # {"bootstrapped": true} — AT backfill complete
    └── dealer_weights.json       # Per-source FMV weighting
```

---

## 4. Active Data Sources

| Source | Method | Status | Thumbnails |
|---|---|---|---|
| AutoTrader | `scraper_autotrader.py` Playwright (mobile site) | ✅ LIVE | Via link preview |
| Bring a Trailer | `scraper.py` Playwright | ✅ LIVE | ✅ Yes |
| eBay Motors | `scraper_ebay.py` (eBay Browse API) | ✅ LIVE | ✅ Yes |
| PCA Mart | `scraper.py` (cookie-auth) | ✅ LIVE | ❌ Local paths |
| cars.com | `scraper_carscom.py` (curl_cffi + data-vehicle-details JSON) | ✅ LIVE | ✅ Yes |
| Rennlist | `scraper_rennlist.py` Playwright — migrated off Distill April 4 | ✅ LIVE | ✅ Yes |
| pcarmarket | `scraper.py` | ✅ LIVE | ✅ Yes |
| Built for Backroads | Distill Desktop (HTML mode) — last remaining Distill source | ✅ LIVE | ✅ Yes |

### Distill Status (April 4)
Distill Web Monitor is now only used for **Built for Backroads**. All other sources have been migrated to local Playwright/API scrapers. Distill subscription can be cancelled once BfB is migrated (low priority — BfB is low volume, 6 listings).

Distill `_SOURCE_MAP` skip flags:
- `autotrader.com` → `skip=True` (owned by `scraper_autotrader.py`)
- `cars.com` → `skip=True` (owned by `scraper_carscom.py`)
- `ebay.com` → `skip=True` (owned by `scraper_ebay.py`)
- `rennlist.com` → `skip=True` (owned by `scraper_rennlist.py`)
- `builtforbackroads.com` → `skip=False` (still Distill)

### Rennlist Scraper Notes
- Playwright headless Chromium + DataImpulse proxy
- URL: `rennlist.com/forums/market/vehicles?countryid=5&sortby=dateline_desc&intent[2]=2&status[0]=0&type[0]=1` (USA only, for-sale, active, vehicles, newest first)
- Page 1 only — no pagination needed for polling cadence
- No state file — fetches and returns every run
- Stale listing expiry handled by `main.py` → `mark_sold()` (same as all other scrapers)
- `.shelf-item` CSS selector, `a[href*='/forums/market/']` for URL, `img[src*='ibsrv.net']` for image
- 20 listings on first smoke test, 100% image coverage ✅

### Proxy Policy — All Scrapers
**Rule: proxy is mandatory, never a fallback.** If DataImpulse is down, the scraper returns `[]` and skips that cycle. The Mac Mini's bare IP is never exposed to AutoTrader or Cars.com.

- `scraper_autotrader.py` — proxy-only enforced ✅
- `scraper_carscom.py` — proxy-only enforced ✅
- `scraper_rennlist.py` — proxy loaded from proxy_config.json ✅
- `scraper.py` (BaT, PCA Mart, pcarmarket) — proxy used, fallback-to-direct still exists (lower risk sites)

### Proxy Credentials
DataImpulse rotating residential proxy (`gw.dataimpulse.com:823`).
- **Username:** 7dffcde9c33e2eab45cb
- **Password:** 068a3aeba25658b5 (updated April 1, 2026)
- **Balance:** $50 topped up April 1, 2026 (~16 months runway at current usage)
- Credits never expire.

### AutoTrader Scraper Notes
- Uses `m.autotrader.com` mobile site — returns `__NEXT_DATA__` JSON, no JS rendering needed
- Desktop `www.autotrader.com` gets blocked by Akamai — always use mobile URL
- Bootstrap complete (`autotrader_state.json` = `{"bootstrapped": true}`)
- Subsequent runs: 1 page × 25 records only
- `sellerTypes=p%2Cd` — both private and dealer listings included
- `seller_type` field populated in DB

---

## 5. Database — Current State

### Tables
- **listings** — ~350+ active. Key fields: dealer, year, make, model, trim, mileage, price, vin, listing_url, image_url, date_first_seen, status, source_category, tier, feed_type, seller_type, generation
- **price_history** — timestamped price changes per listing
- **sold_comps** — 5,666+ records. The FMV truth layer.
- **bat_reserve_not_met** — 1,784 records. BaT auctions ending without meeting reserve.
- **snapshots** — raw daily snapshots per dealer
- **hagerty_valuations** — 22 rows (Good condition only — Excellent locked without session token)

### Sold Comps Field Fill Rates
| Field | Fill Rate |
|---|---|
| sold_date | 95% |
| transmission | 99% |
| mileage | 97% |
| generation | 81% |
| drivetrain | 94% |
| sold_price | 76% (NULL = reserve not met) |

---

## 6. FMV Engine — LIVE ✅

`fmv.py` fully wired. Called by dashboards and `notify_imessage.py` on every scrape cycle.

- Primary source: BaT sold comps (weight 1.0)
- Groups by: generation + trim family
- Recency decay: full weight ≤6 months, decays to 0.3 at 24 months
- Outputs: weighted median, price_low/high, RNM floor, confidence (HIGH/MEDIUM/LOW/NONE), comp count

---

## 7. Alert System — LIVE ✅

**File:** `notify_imessage.py`
**Recipient:** 6108361111
**Format:** One iMessage per car. URL link preview automatically shows photo.

### New-listing alerts (`notify_new_listings`)
Fires for **every** new listing the moment it first hits the DB — no FMV required.
Called from `main.py` immediately after `run_snapshot()`, before deal-scoring.

```
🆕 NEW: 2019 Porsche 911 GT3 RS
💰 $289,000
🛣️  8,400 mi
📍 Bring a Trailer [AUCTION]  [GT/Collector]
🔗 https://bringatrailer.com/listing/...
```

Dedup key: `"new:{listing_url}"` in `seen_alerts_imessage.json` — a listing never triggers this alert more than once across scrape cycles.

### Deal/Watch alerts (`main`)
Fires only when a listing scores as DEAL or WATCH against FMV.

```
🔥 DEAL: 2022 Porsche 911 GT3
💰 $239,900  -15% vs FMV ($282,000)
🛣️  12,000 mi
📍 AutoTrader [RETAIL]  [GT/Collector]
🔗 https://www.autotrader.com/cars-for-sale/vehicle/...
```

**Thresholds:**
- Tier 1 GT/Collector: alert on DEAL (10%+ below) OR WATCH (5-10% below)
- Tier 2 Standard: alert only on DEAL (10%+ below)
- Dedup: won't re-alert same listing unless price drops or flag improves WATCH→DEAL
- Confidence gating: skips NONE confidence

### Alert order each cycle
1. `notify_new_listings()` — every new car, no FMV required
2. `notify_imessage.main()` — deal/watch scoring on all active listings

---

## 8. Open Items & Next Steps

### Immediate
1. **Low-price DEAL false positives** — add ⚠️ caveat in alert for LOW confidence + price <$20k
2. **AutoTrader intermittent zeros** — proxy dead-detection can mark proxy dead on startup if DataImpulse is briefly down, causing direct-IP exposure → Akamai block. Harden startup detection.

### Short Term
3. **Cars & Bids active listings** — sold comps exist. Active listings scraper is small additional work. High value for Tier 1/GT.
4. **PCA Mart image URLs** — stored as local `/static/img_cache/` paths. Need public URLs for iMessage previews.
5. **Built for Backroads → Playwright scraper** — last remaining Distill source. Low priority (6 listings) but would allow cancelling Distill subscription entirely.
6. **classic.com API** — follow up with insight@classic.com

### Phase 4 — Predictive Analysis (needs 30+ days of data)
7. Weekly/monthly reports gain real predictive value once data accumulates
8. Macro signal integration (rates, auction season, market indices)

---

## 9. Known Issues / Watch List

| Issue | Severity | Status |
|---|---|---|
| AutoTrader intermittent zeros | Medium | Proxy startup detection can fail — harden in next AT session |
| Low-price DEAL false positives | Low | Salvage/flood at $1k–$20k scoring as DEAL. Fix: ⚠️ caveat for LOW conf + price <$20k |
| PCA Mart images local-only | Low | `/static/img_cache/` paths not accessible externally |
| Hagerty Excellent prices locked | Low | Requires session token — Good condition only for now |
| Distill subscription | Low | Only BfB remains on Distill — can cancel once BfB scraper built |

---

## 10. Session Log

### March 26, 2026
- BaT backfill: ~124 initial comps
- eBay Browse API activated (~293 listings/run)
- DataImpulse proxy activated (replaced Webshare)

### March 27, 2026
- BaT backfill complete: 5,652 comps
- DB schema extended: generation, engine, drivetrain, options columns
- `enrich_bat_vins.py` ran overnight
- `decode_vin_generation.py` built and tested

### March 28–29, 2026
- Migrated eBay, Cars.com, AutoTrader from API/Apify → Distill Web Monitor
- Dashboard overhauled: sort by created_at, generation filter, source badges
- Rennlist, Built for Backroads added via Distill
- AutoTrader IP-blocked from over-triggering; recovered March 31

### March 30, 2026
- FMV pipeline wiring audit complete
- Generation fill rate: 11% → 81%
- Drivetrain fill rate: 8% → 94%
- iMessage alerts activated — 98 alerts sent on first live run

### April 1, 2026
- DataImpulse proxy password rotated + topped up $50
- `scraper_autotrader.py` built and fully wired (mobile site, bootstrap complete)
- `notify_imessage.py` updated: link preview handles photo, no separate image message
- Hagerty valuations: 22 rows scraped (Good condition only)

### April 2, 2026
- `scraper_carscom.py` built: curl_cffi + data-vehicle-details JSON, 61 listings, 100% images
- `scraper_ebay.py` built: eBay Browse API, OAuth2, bootstrap-then-monitor, 81 listings
- `enrich_ebay_mileage.py` built: on-demand eBay mileage/VIN enricher via per-item API
- Proxy policy hardened: no naked-IP fallback on AutoTrader or Cars.com

### April 4, 2026
- **`scraper_rennlist.py` built and wired — Rennlist fully migrated off Distill:**
  - Playwright headless Chromium + DataImpulse proxy
  - Pre-filtered URL: USA only, for-sale, active, vehicles, newest first
  - Page 1 only — no pagination, no state file
  - `.shelf-item` parser ported from `distill_poller.py`
  - Smoke test: 20 listings, 100% image coverage ✅
  - `distill_poller.py` rennlist.com entry → `skip=True`
  - Stale listing expiry handled by existing `mark_sold()` in `main.py`
- **Distill now only serves Built for Backroads** (6 listings) — all other sources on local scrapers
