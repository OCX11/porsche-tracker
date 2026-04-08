"""Extract PCA Mart cookies from Chrome and save to data/pca_cookies.json"""
import json
from pathlib import Path

try:
    import browser_cookie3
    jar = browser_cookie3.chrome(domain_name=".pca.org")
    cookies = {c.name: c.value for c in jar}
    also = browser_cookie3.chrome(domain_name="mart.pca.org")
    cookies.update({c.name: c.value for c in also})

    out = Path(__file__).parent / "data" / "pca_cookies.json"
    out.write_text(json.dumps(cookies, indent=2))
    print(f"Saved {len(cookies)} cookies to {out}")
    for k in ["CFID", "CFTOKEN", "JSESSIONID", "authToken"]:
        if k in cookies:
            print(f"  {k} = {cookies[k][:20]}...")
except Exception as e:
    print(f"Error: {e}")
    import traceback; traceback.print_exc()
