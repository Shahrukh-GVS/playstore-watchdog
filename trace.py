"""
trace.py — Play Store Watchdog: Tracing Tool

Run this interactively:
    python trace.py

It will prompt you to paste a Play Store game URL. It then:
  1. Extracts the package name
  2. Finds the app's website
  3. Fetches <website>/app-ads.txt
  4. Checks each line against your known ad_network_ids (exact ID match + relationship == DIRECT)
  5. If matched -> pulls the developer's full catalog and adds developer + all apps to Supabase
  6. Sends a Discord notification with the result either way
"""

import os
import re
import sys
import urllib.parse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def send_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[warn] No Discord webhook configured, skipping notification.")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"[warn] Failed to send Discord notification: {e}")


def extract_package_name(url: str) -> str | None:
    match = re.search(r"[?&]id=([a-zA-Z0-9._]+)", url)
    return match.group(1) if match else None


def fetch_app_page(package_name: str):
    url = f"https://play.google.com/store/apps/details?id={package_name}&gl=us&hl=en"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def unwrap_google_redirect(url: str) -> str:
    """Play Store often wraps external links as https://www.google.com/url?q=REAL_URL&..."""
    if "google.com/url" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "q" in qs and qs["q"]:
            return qs["q"][0]
    return url


def extract_website(soup: BeautifulSoup) -> str | None:
    # Play Store "Website" link text varies (sometimes just "Website", sometimes "Visit website")
    for a in soup.find_all("a", href=True):
        label = (a.get("aria-label") or "").lower()
        text = (a.get_text() or "").strip().lower()
        href = a["href"]
        if ("website" in label or "website" in text) and href.startswith("http"):
            return unwrap_google_redirect(href)
    return None


def extract_developer_info(soup: BeautifulSoup):
    dev_link = None
    dev_name = None
    for a in soup.find_all("a", href=True):
        if "/store/apps/developer?id=" in a["href"] or "/store/apps/dev?id=" in a["href"]:
            dev_link = "https://play.google.com" + a["href"] if a["href"].startswith("/") else a["href"]
            dev_name = a.get_text(strip=True) or dev_name
            break
    return dev_name, dev_link


def fetch_url_content(url):
    """Try curl_cffi first (bypasses TLS fingerprint blocking), fall back to requests."""
    try:
        from curl_cffi import requests as cf_requests
        resp = cf_requests.get(url, headers=HEADERS, timeout=15, impersonate="chrome124")
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def looks_like_ads_txt(text):
    """Basic sanity check that the content is actually an ads.txt-style file."""
    if not text:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[2].upper() in ("DIRECT", "RESELLER"):
            return True
    return False


def fetch_app_ads_txt(website: str) -> str | None:
    candidates = []

    # If the extracted "website" itself already points at a .txt file, try it as-is first
    if website.lower().endswith(".txt"):
        candidates.append(website)

    parsed = urllib.parse.urlparse(website if website.startswith("http") else "https://" + website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates.append(f"{root}/app-ads.txt")
    candidates.append(f"{root}/ads.txt")

    for url in candidates:
        content = fetch_url_content(url)
        if content and looks_like_ads_txt(content):
            print(f"[info] Found valid ads.txt-style content at {url}")
            return content

    return None


def parse_app_ads_lines(raw_text: str):
    lines = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            domain, account_id, relationship = parts[0], parts[1], parts[2]
            lines.append((domain, account_id, relationship))
    return lines


def get_known_ids():
    result = supabase.table("ad_network_ids").select("account_id").execute()
    return {row["account_id"] for row in result.data}


def find_match(ads_lines, known_ids):
    for domain, account_id, relationship in ads_lines:
        if account_id in known_ids and relationship.upper() == "DIRECT":
            return (domain, account_id, relationship)
    return None


def normalize_title(raw_title: str, developer_name: str = None) -> str:
    if not raw_title:
        return raw_title
    text = raw_title.strip()
    text = re.sub(r'^(icon image|app icon)\s*', '', text, flags=re.IGNORECASE).strip()
    if "," in text:
        text = text.split(",")[0].strip()
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r'\d+(?:\.\d+)?\s*(?:stars?|★)\s*$', '', text, flags=re.IGNORECASE).strip()
    m = re.match(r'^(.+?)\s+\1$', text, flags=re.IGNORECASE)
    if m:
        text = m.group(1).strip()
    if developer_name:
        dev_norm = re.sub(r'\s+', '', developer_name).lower()
        if dev_norm:
            matched = 0
            cut_index = len(text)
            i = len(text) - 1
            while i >= 0 and matched < len(dev_norm):
                ch = text[i]
                if ch != " ":
                    if ch.lower() != dev_norm[len(dev_norm) - 1 - matched]:
                        matched = -1
                        break
                    matched += 1
                cut_index = i
                i -= 1
            if matched == len(dev_norm):
                text = text[:cut_index].strip()
    return text.strip()


def fetch_developer_catalog(dev_link: str, developer_name: str = None):
    """Returns list of dicts: package_name, title, icon_url, installs_bracket-ish, is_pre_registration"""
    resp = requests.get(dev_link, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    apps = []
    seen_packages = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/store/apps/details\?id=([a-zA-Z0-9._]+)", href)
        if not m:
            continue
        package_name = m.group(1)
        if package_name in seen_packages:
            continue
        seen_packages.add(package_name)

        title = a.get("aria-label") or a.get_text(strip=True) or package_name
        title = normalize_title(title, developer_name)
        img = a.find("img")
        icon_url = img["src"] if img and img.has_attr("src") else None

        apps.append({
            "package_name": package_name,
            "title": title,
            "icon_url": icon_url,
        })

    return apps


def upsert_developer(dev_name: str, dev_link: str) -> int:
    dev_id_match = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    dev_id = dev_id_match.group(1) if dev_id_match else dev_link

    existing = supabase.table("developers").select("id").eq("dev_id", dev_id).execute()
    if existing.data:
        return existing.data[0]["id"]

    result = supabase.table("developers").insert({
        "dev_id": dev_id,
        "name": dev_name,
        "developer_url": dev_link,
        "source": "trace",
    }).execute()
    return result.data[0]["id"]


def extract_canonical_title(soup) -> str | None:
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def extract_install_info(soup):
    """Fast fallback check using plain HTML (misses JS-rendered pre-register buttons)."""
    page_text = soup.get_text(" ", strip=True)
    if re.search(r"pre-?register", page_text, re.IGNORECASE):
        return True, None
    m = re.search(r"([\d.,]+[KMB]?\+)\s*Downloads", page_text, re.IGNORECASE)
    if m:
        return False, m.group(1)
    return False, None


def check_install_info_browser(package_name, country="us"):
    """Accurate check using a real headless browser (catches JS-rendered
    'Pre-register' buttons that plain HTML fetching misses). Returns
    (is_pre_registration, installs_bracket) or None if unavailable/failed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    url = f"https://play.google.com/store/apps/details?id={package_name}&gl={country}&hl=en"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=20000, wait_until="networkidle")
            page_text = page.inner_text("body")
            browser.close()

        if re.search(r"pre-?register", page_text, re.IGNORECASE):
            return True, None
        m = re.search(r"([\d.,]+[KMB]?\+)\s*Downloads", page_text, re.IGNORECASE)
        if m:
            return False, m.group(1)
        return False, None
    except Exception as e:
        print(f"[warn] Playwright check failed for {package_name}: {e}")
        return None


def get_install_info(package_name, soup=None, country="us"):
    """Tries the accurate browser-based check first; falls back to plain HTML."""
    result = check_install_info_browser(package_name, country)
    if result is not None:
        return result
    if soup is not None:
        return extract_install_info(soup)
    return False, None


def insert_apps(developer_id: int, apps: list):
    inserted_count = 0
    for app in apps:
        existing = supabase.table("apps").select("id").eq("package_name", app["package_name"]).execute()
        if existing.data:
            continue  # already tracked

        # Fetch the app's own detail page for a canonical title, so it matches
        # exactly what the monitor will later compare against (avoids a false
        # "listing changed" flood on the very next check cycle).
        detail_soup = fetch_app_page(app["package_name"])
        canonical_title = extract_canonical_title(detail_soup) if detail_soup else None
        final_title = canonical_title or app["title"]
        is_pre_reg, installs = get_install_info(app["package_name"], soup=detail_soup)

        supabase.table("apps").insert({
            "package_name": app["package_name"],
            "developer_id": developer_id,
            "title": final_title,
            "icon_url": app["icon_url"],
            "status": "active",
            "is_pre_registration": is_pre_reg,
            "installs_bracket": installs,
        }).execute()
        inserted_count += 1
    return inserted_count


def main():
    url = input("Paste the Play Store game URL: ").strip()

    package_name = extract_package_name(url)
    if not package_name:
        msg = f"❌ **Trace failed** — could not extract package name from URL:\n{url}"
        print(msg)
        send_discord(msg)
        return

    print(f"[info] Package name: {package_name}")
    soup = fetch_app_page(package_name)
    if not soup:
        msg = f"❌ **Trace failed** — could not load Play Store page for `{package_name}`"
        print(msg)
        send_discord(msg)
        return

    website = extract_website(soup)
    if not website:
        msg = f"⚠️ **Trace incomplete** — `{package_name}` has no website listed. Skipping."
        print(msg)
        send_discord(msg)
        return

    print(f"[info] Website: {website}")
    ads_txt_raw = fetch_app_ads_txt(website)
    if not ads_txt_raw:
        msg = f"⚠️ **Trace incomplete** — no app-ads.txt found at `{website}` for `{package_name}`."
        print(msg)
        send_discord(msg)
        return

    ads_lines = parse_app_ads_lines(ads_txt_raw)
    known_ids = get_known_ids()
    match = find_match(ads_lines, known_ids)

    if not match:
        msg = (f"🔍 **Trace complete — no match**\n"
               f"Package: `{package_name}`\nWebsite: {website}\n"
               f"No known ad IDs matched with DIRECT relationship.")
        print(msg)
        send_discord(msg)
        return

    domain, account_id, relationship = match
    print(f"[info] MATCH FOUND: {domain} / {account_id} / {relationship}")

    dev_name, dev_link = extract_developer_info(soup)
    if not dev_link:
        msg = f"⚠️ Match found for `{package_name}` but could not extract developer page link."
        print(msg)
        send_discord(msg)
        return

    print(f"[info] Developer: {dev_name} — {dev_link}")
    developer_id = upsert_developer(dev_name, dev_link)

    catalog = fetch_developer_catalog(dev_link, developer_name=dev_name)
    inserted = insert_apps(developer_id, catalog)

    msg = (f"✅ **Match confirmed — developer added to watchlist**\n"
           f"Developer: **{dev_name}**\n"
           f"Matched ID: `{account_id}` ({domain}, DIRECT)\n"
           f"Total apps found: {len(catalog)} | Newly added: {inserted}\n"
           f"Developer page: {dev_link}")
    print(msg)
    send_discord(msg)


if __name__ == "__main__":
    main()