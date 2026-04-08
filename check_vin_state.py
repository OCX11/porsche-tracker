import sqlite3
conn = sqlite3.connect('data/inventory.db')
conn.row_factory = sqlite3.Row

r = conn.execute("SELECT COUNT(*) total, COUNT(vin) with_vin FROM listings WHERE status='active'").fetchone()
print(f"Active listings: {r['total']} total, {r['with_vin']} with VIN ({100*r['with_vin']//max(r['total'],1)}%)")

print()
rows = conn.execute("SELECT dealer, year, model, trim, vin FROM listings WHERE status='active' ORDER BY date_first_seen DESC LIMIT 12").fetchall()
for row in rows:
    vin = row['vin'] or 'NONE'
    trim = row['trim'] or '(no trim)'
    print(f"  {row['year']} {row['model']} [{trim}] | VIN: {vin}")

r2 = conn.execute("SELECT COUNT(*) total, COUNT(vin) with_vin FROM sold_comps").fetchone()
print(f"\nSold comps: {r2['total']} total, {r2['with_vin']} with VIN")
