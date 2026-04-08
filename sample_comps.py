"""Pull a quality sample of sold_comps for review."""
import sqlite3
conn = sqlite3.connect('data/inventory.db')
conn.row_factory = sqlite3.Row

# Overall stats
r = conn.execute("SELECT COUNT(*) n, MIN(sold_date) earliest, MAX(sold_date) latest FROM sold_comps WHERE source='BaT'").fetchone()
print(f"BaT comps: {r['n']} total | {r['earliest']} → {r['latest']}\n")

# Breakdown by model
print("=== By model ===")
for row in conn.execute("SELECT model, COUNT(*) n, AVG(sold_price) avg_price, MIN(sold_price) min_p, MAX(sold_price) max_p FROM sold_comps WHERE source='BaT' AND sold_price > 0 GROUP BY model ORDER BY n DESC LIMIT 15").fetchall():
    print(f"  {row['model']:<12} {row['n']:4d} comps  avg=${row['avg_price']:>8,.0f}  range=${row['min_p']:>7,}–${row['max_p']:>9,}")

# Sample of 15 random comps showing all key fields
print("\n=== 15 random comps ===")
rows = conn.execute("""
    SELECT year, model, trim, mileage, sold_price, sold_date, transmission, title, listing_url
    FROM sold_comps WHERE source='BaT' AND sold_price > 0
    ORDER BY RANDOM() LIMIT 15
""").fetchall()
for r in rows:
    miles = f"{r['mileage']:,}mi" if r['mileage'] else "?mi"
    price = f"${r['sold_price']:,}"
    trans = r['transmission'] or '?'
    trim  = r['trim'] or '—'
    print(f"  {r['year']} {r['model']:<8} [{trim:<25}] {miles:>10}  {price:>10}  {trans:<9}  {r['sold_date']}  {r['listing_url'][-50:] if r['listing_url'] else ''}")

# Check for data quality issues
print("\n=== Data quality ===")
no_price   = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND (sold_price IS NULL OR sold_price=0)").fetchone()['n']
no_date    = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND sold_date IS NULL").fetchone()['n']
no_model   = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND (model IS NULL OR model='')").fetchone()['n']
no_year    = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND year IS NULL").fetchone()['n']
no_mileage = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND mileage IS NULL").fetchone()['n']
no_trans   = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT' AND transmission IS NULL").fetchone()['n']
total      = conn.execute("SELECT COUNT(*) n FROM sold_comps WHERE source='BaT'").fetchone()['n']
print(f"  Total:           {total}")
print(f"  No price (RNM):  {no_price}")
print(f"  No date:         {no_date}")
print(f"  No model:        {no_model}")
print(f"  No year:         {no_year}")
print(f"  No mileage:      {no_mileage}  ({100*no_mileage//max(total,1)}%)")
print(f"  No transmission: {no_trans}  ({100*no_trans//max(total,1)}%)")

# Sample of RNM records
print("\n=== Reserve not met samples ===")
for r in conn.execute("SELECT year, model, trim, mileage, sold_date, title FROM sold_comps WHERE source='BaT' AND (sold_price IS NULL OR sold_price=0) ORDER BY RANDOM() LIMIT 5").fetchall():
    print(f"  {r['year']} {r['model']} [{r['trim'] or '—'}] — {r['title'][:60]}")
