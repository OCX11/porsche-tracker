# Session Summary — 2026-03-29

## What We Were Working On
Stabilizing the data pipeline, fixing thumbnails, migrating sources from scrapers/Apify to Distill,
and simplifying the overall architecture for reliability and low maintenance.

---

## Architecture — Current State

### Scraper (`scraper.py` / launchd every 12 min)
| Source | Status | Notes |
|---|---|---|
| Bring a Trailer | ✅ Active | Playwright, auction |
| PCA Mart | ✅ Active | Cookie-authenticated, thumbnails working |
| pcarmarket | ✅ Active | Auction |

### Distill → `distill_poller.py` (HTML mode, 60s poll)
| Source | Status | Notes |
|---|---|---|
| eBay Motors | ✅ Active | Private sellers only (`For Sale By=Private Seller`), HTML parser, thumbnails |
| Rennlist | ✅ Active | HTML mode, individual listing URLs + thumbnails |
| Cars.com | ✅ Active | HTML parser, price history tracking |
| Built for Backroads | ✅ Active | Private sellers, HTML parser, thumbnails |
| AutoTrader | ⚠️ Paused | IP blocked from too many manual triggers today. Text parser working, re-enable tomorrow. |

### Disabled / Parked
| Source | Reason |
|---|---|
| Holt Motorsports | 403/timeout — re-enable after priorities stabilized |
| Grand Prix Motors | Playwright timing (eBay covers their inventory) |
| classic.com scraper | API key pending from insight@classic.com |
| classic.com Distill | Disabled — dedup collision with BaT |
| Ryan Friedman, Velocity, Road Scholars, Gaudin, UDrive, MFML | Low volume dealers — parked |
| Apify (AutoTrader + Cars.com) | Monthly free tier exhausted, replaced by Distill |

---

## Active Listing Counts (end of day)
| Dealer | Active |
|---|---|
| AutoTrader | 88 |
| PCA Mart | 85 |
| Bring a Trailer | 52 |
| eBay Motors | 43 |
| Rennlist | 22 |
| Cars.com | 20 |
| pcarmarket | 7 |
| Built for Backroads | 1 (monitor paused, will repopulate) |

---

## Major Changes Made Today

### Infrastructure
- Migrated eBay from Browse API scraper → Distill (private sellers only)
- Migrated Cars.com from Apify → Distill
- Migrated AutoTrader from Apify → Distill (text mode working, HTML blocked)
- Disabled Apify dependency entirely for active listings
- Distill poller source map updated: eBay, AutoTrader, classic.com added/modified
- All Distill monitors switched to HTML mode (except AutoTrader text fallback)

### Dashboard
- Fixed sort order — now uses `created_at` (real timestamp) not `date_first_seen` (midnight-stamped)
- Generation filter now has correct fixed order: G-Series → 964 → 993 → 996 → 997.1 → 997.2 → 991.1 → 991.2 → 992 → Boxster/Cayman
- Health pills updated to reflect current sources
- Source badges cleaned up

### Thumbnails
- Rennlist: HTML mode in Distill captures `<img>` tags → images now populating for new listings
- eBay: HTML parser extracts individual listing URLs + images from `div.su-card-container`
- Built for Backroads: HTML parser groups `<a>` tags by href, extracts images
- PCA Mart: confirmed working, all 85 listings have thumbnails
- `_resolve_img()` in `new_dashboard.py` now caches ALL external URLs server-side (not just PCA)
- Dashboard push runtime ~8 seconds (images cached by MD5 hash, skips already-cached)

### Data Quality
- Upsert dedup key tightened: URL-first match, then (dealer, year, model, price) fallback
- `created_at` preserved on re-insert — reply bumps no longer float old Rennlist listings to top
- Rennlist snapshot expiry: listings not in latest Distill trigger get marked sold
- eBay added to `FULL_SNAPSHOT_DEALERS` — stale listings expire on each snapshot
- Distill poller dealer name unified: `autotrader.com` → `AutoTrader`
- Built for Backroads category corrected: DEALER → RETAIL (private sellers)
- 187 stale Rennlist listings expired (30+ days old)
- 399 old eBay listings (mixed private/dealer) expired on migration
- 41 parked dealer listings expired
- 33 Holt/Grand Prix stale listings expired

### Parsers Added (distill_poller.py)
- AutoTrader: splits on "Newly Listed"/"Sponsored", custom price regex for concatenated format
- eBay: BeautifulSoup on `div.su-card-container`, extracts title/price/url/image
- Built for Backroads: groups `<a href="/listing/...">` by URL, combines text + extracts images
- Rennlist: HTML branch using `.shelf-item` selector, injects `LISTING_URL:` and `IMAGE_URL:` sentinels

### Scripts Added
- `enrich_rennlist.py`: enriches Rennlist listings missing image_url or with search-page URLs
  Added to `run_daily.sh` after `enrich_listings.py`

---

## Known Issues / To Fix

### Immediate
- [ ] AutoTrader IP cooldown — re-enable Distill monitor tomorrow, confirm text parser resumes
- [ ] AutoTrader thumbnails — text mode has no image URLs. Options: (a) AutoTrader JSON API scraper, (b) accept no thumbs
- [ ] 10 Rennlist listings still have search-page URL as `listing_url` — `enrich_rennlist.py` should fix these on next run
- [ ] Built for Backroads only 1 listing — monitor was paused, trigger manual Distill check to repopulate

### Short Term
- [ ] Cars & Bids active listings — sold comps actor exists, active listings is a small delta. High value for Tier 1/GT cars
- [ ] Add `generation` column to `listings` table — computed at insert time from `_gen()`, enables proper DB-level filtering
- [ ] AutoTrader parser: add to `FULL_SNAPSHOT_DEALERS` once HTML mode confirmed working
- [ ] DataImpulse bandwidth — monitor usage, should drop significantly now that Distill handles eBay/AT/Cars.com

### Phase 3 (FMV Engine)
- [ ] Build FMV calculation engine using sold comps (5,600+ BaT comps enriched with VIN/generation/mileage/options)
- [ ] Add deal score to every listing (X% above/below FMV)
- [ ] Wire deal scores into dashboard
- [ ] Comps by Generation panel on dashboard (avg/min/max price, manual %, per generation)

### Phase 4 (Günther Activation)
- [ ] Re-enable `notify_gunther.py` once FMV engine is live
- [ ] Switch Günther to Claude Sonnet (was scheduled for April 1)
- [ ] End-to-end test: new listing → deal score → Günther evaluates → Telegram alert

### Parked (revisit later)
- [ ] Holt Motorsports — 403 fix
- [ ] classic.com API — awaiting key from insight@classic.com
- [ ] Cars.com private seller only filter (currently mixed)
- [ ] AutoTrader HTML thumbnails via JSON API

---

## Key Learnings
- AutoTrader aggressively rate-limits repeated manual Distill triggers — do not trigger more than 2-3x in a session
- eBay Browse API cannot filter by seller type — must use website URL with `For Sale By=Private Seller`
- Distill `dataAttr: text` strips all HTML including hrefs and img src — must use HTML mode for URLs/images
- Built for Backroads splits listing data across multiple `<a>` tags with the same href — must group by URL before parsing
- `created_at` in SQLite DEFAULT only fires on INSERT, not UPDATE — safe to use as "when first seen" timestamp
- GitHub Pages CDN can lag 5-10 min after push — use `?nocache=1` param to force fresh fetch for testing
