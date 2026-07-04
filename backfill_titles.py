"""
backfill_titles.py — One-time cleanup script

Fixes existing garbled titles in the apps table (from before the
canonical-title fix) by fetching each app's own detail page and using
its clean <title> tag instead of the old catalog-scraped text.

Run once:
    python backfill_titles.py

Safe to re-run — apps whose title already looks clean will just get
re-confirmed with the same value.
"""

import os
import re
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def fetch_app_page(package_name, country="us"):
    url = f"https://play.google.com/store/apps/details?id={package_name}&gl={country}&hl=en"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


def extract_canonical_title(soup):
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def main():
    apps = supabase.table("apps").select("*").eq("status", "active").execute().data
    print(f"[info] Backfilling titles for {len(apps)} active apps...")

    fixed = 0
    failed = 0

    for i, app in enumerate(apps):
        soup = fetch_app_page(app["package_name"])
        canonical_title = extract_canonical_title(soup) if soup else None

        if canonical_title and canonical_title != app["title"]:
            supabase.table("apps").update({"title": canonical_title}).eq("id", app["id"]).execute()
            print(f"[fixed] {app['package_name']}: '{app['title']}' -> '{canonical_title}'")
            fixed += 1
        elif not canonical_title:
            print(f"[warn] Could not fetch canonical title for {app['package_name']}")
            failed += 1

        time.sleep(0.5)  # be gentle on Play Store

        if (i + 1) % 20 == 0:
            print(f"[info] Progress: {i + 1}/{len(apps)}")

    print(f"[info] Done. Fixed: {fixed}, Failed to fetch: {failed}, Unchanged: {len(apps) - fixed - failed}")


if __name__ == "__main__":
    main()