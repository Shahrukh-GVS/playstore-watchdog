"""
dashboard.py — Play Store Watchdog: Web Dashboard

Run locally in Codespaces with:
    streamlit run dashboard.py

Or deploy free via Streamlit Community Cloud (share.streamlit.io) pointing
at this file in your GitHub repo.

Three tabs:
  1. Trace       - paste a Play Store URL, runs the app-ads.txt match + auto-add logic
  2. Watchlist   - developers grouped, expandable to show their apps
  3. Recent Activity - change_log, most recent first, filterable by event type
"""

import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests
import streamlit as st
from bs4 import BeautifulSoup
from supabase import create_client

import auth

st.set_page_config(page_title="Play Store Watchdog", layout="wide")

SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", os.getenv("SUPABASE_ANON_KEY"))
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))  # service_role: user creation only
APPSTORESPY_API_KEY = st.secrets.get("APPSTORESPY_API_KEY", os.getenv("APPSTORESPY_API_KEY"))

if not SUPABASE_ANON_KEY:
    st.error(
        "Missing **SUPABASE_ANON_KEY**. Add it to `.streamlit/secrets.toml` "
        "(Supabase → Project Settings → API → `anon` `public` key)."
    )
    st.stop()

# Login gate — nothing below runs until a user is signed in.
if not auth.require_login(SUPABASE_URL, SUPABASE_ANON_KEY):
    st.stop()

# Every query below uses the signed-in user's client. Row Level Security means
# the database returns only their rows, and inserts stamp user_id automatically.
supabase = st.session_state.sb_client
auth.render_account_sidebar(SUPABASE_URL, SUPABASE_ANON_KEY)

PKT = timezone(timedelta(hours=5))


def format_pkt(iso_str):
    """Converts a stored UTC timestamp into a clean Pakistan-time display string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        pkt_dt = dt.astimezone(PKT)
        return pkt_dt.strftime("%B %d, %Y — %I:%M %p PKT")
    except Exception:
        return iso_str

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ---------------------------------------------------------------------------
# Shared tracing logic (same as trace.py, reused here for the dashboard button)
# ---------------------------------------------------------------------------

def extract_package_name(url):
    m = re.search(r"[?&]id=([a-zA-Z0-9._]+)", url)
    return m.group(1) if m else None


def fetch_app_page(package_name, country="us"):
    url = f"https://play.google.com/store/apps/details?id={package_name}&gl={country}&hl=en"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def extract_website(soup):
    for a in soup.find_all("a", href=True):
        label = (a.get("aria-label") or "").lower()
        text = (a.get_text() or "").strip().lower()
        href = a["href"]
        if ("website" in label or "website" in text) and href.startswith("http"):
            return unwrap_google_redirect(href)
    return None


def unwrap_google_redirect(url):
    """Play Store often wraps external links as https://www.google.com/url?q=REAL_URL&..."""
    if "google.com/url" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "q" in qs and qs["q"]:
            return qs["q"][0]
    return url


def extract_developer_info(soup):
    for a in soup.find_all("a", href=True):
        if "/store/apps/developer?id=" in a["href"] or "/store/apps/dev?id=" in a["href"]:
            dev_link = "https://play.google.com" + a["href"] if a["href"].startswith("/") else a["href"]
            dev_name = a.get_text(strip=True)
            return dev_name, dev_link
    return None, None


def fetch_url_content(url):
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


def fetch_app_ads_txt(website):
    candidates = []

    if website.lower().endswith(".txt"):
        candidates.append(website)

    parsed = urllib.parse.urlparse(website if website.startswith("http") else "https://" + website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates.append(f"{root}/app-ads.txt")
    candidates.append(f"{root}/ads.txt")

    for url in candidates:
        content = fetch_url_content(url)
        if content and looks_like_ads_txt(content):
            return content

    return None


def parse_app_ads_lines(raw_text):
    lines = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            lines.append((parts[0], parts[1], parts[2]))
    return lines


def get_known_ids():
    """Returns {account_id: {"studio_id": ..., "studio_name": ...}} for matching."""
    result = supabase.table("ad_network_ids").select("account_id, studio_id").execute()
    studios = {s["id"]: s["name"] for s in supabase.table("studios").select("id, name").execute().data}
    return {
        row["account_id"]: {
            "studio_id": row.get("studio_id"),
            "studio_name": studios.get(row.get("studio_id"), "Unassigned"),
        }
        for row in result.data
    }


def find_match(ads_lines, known_ids):
    """Returns (domain, account_id, relationship, studio_id, studio_name) or None."""
    for domain, account_id, relationship in ads_lines:
        if account_id in known_ids and relationship.upper() == "DIRECT":
            info = known_ids[account_id]
            return (domain, account_id, relationship, info["studio_id"], info["studio_name"])
    return None


def normalize_title(raw_title, developer_name=None):
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


def ensure_gl_us(url):
    """Makes sure a Play Store URL always includes gl=us, appending it if missing."""
    if "gl=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}gl=us"


def fetch_developer_catalog(dev_link, developer_name=None):
    dev_link = ensure_gl_us(dev_link)
    resp = requests.get(dev_link, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    apps, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/store/apps/details\?id=([a-zA-Z0-9._]+)", a["href"])
        if not m:
            continue
        package_name = m.group(1)
        if package_name in seen:
            continue
        seen.add(package_name)
        title = a.get("aria-label") or a.get_text(strip=True) or package_name
        title = normalize_title(title, developer_name)
        img = a.find("img")
        icon_url = img["src"] if img and img.has_attr("src") else None
        apps.append({"package_name": package_name, "title": title, "icon_url": icon_url})
    return apps


def upsert_developer(dev_name, dev_link, studio_id=None):
    m = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    dev_id = m.group(1) if m else dev_link
    existing = supabase.table("developers").select("id").eq("dev_id", dev_id).execute()
    if existing.data:
        developer_id = existing.data[0]["id"]
        if studio_id:
            supabase.table("developers").update({"studio_id": studio_id}).eq("id", developer_id).execute()
        return developer_id
    result = supabase.table("developers").insert({
        "dev_id": dev_id, "name": dev_name, "developer_url": dev_link,
        "source": "trace", "studio_id": studio_id,
    }).execute()
    return result.data[0]["id"]


def extract_canonical_title(soup):
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def extract_install_info(soup):
    page_text = soup.get_text(" ", strip=True)
    if re.search(r"pre-?register", page_text, re.IGNORECASE):
        return True, None
    m = re.search(r"([\d.,]+[KMB]?\+)\s*Downloads", page_text, re.IGNORECASE)
    if m:
        return False, m.group(1)
    # No install count found at all -> treat as pre-registration
    return True, None


def insert_apps(developer_id, apps):
    inserted = 0
    for app in apps:
        existing = supabase.table("apps").select("id").eq("package_name", app["package_name"]).execute()
        if existing.data:
            continue

        detail_soup = fetch_app_page(app["package_name"])
        canonical_title = extract_canonical_title(detail_soup) if detail_soup else None
        final_title = canonical_title or app["title"]
        is_pre_reg, installs = extract_install_info(detail_soup) if detail_soup else (False, None)

        supabase.table("apps").insert({
            "package_name": app["package_name"], "developer_id": developer_id,
            "title": final_title, "icon_url": app["icon_url"], "status": "active",
            "is_pre_registration": is_pre_reg, "installs_bracket": installs,
        }).execute()
        inserted += 1
    return inserted


def discover_games(days_back, limit=100, country="US", pre_register_only=False):
    """
    Calls AppstoreSpy's filtered search for newly released/updated games,
    sorted by daily installs (matches: Google Play, Published, Game,
    Release date within `days_back` days, sorted highest daily installs).
    Returns the raw list of app dicts from their API.
    """
    if not APPSTORESPY_API_KEY:
        return None, "No AppstoreSpy API key configured."

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)

    filter_body = {
        "category_type": "GAME",
        "release_date": {"gte": start.isoformat(), "lte": today.isoformat()},
    }
    if pre_register_only:
        filter_body["pre_register"] = True
        filter_body["published"] = True
    else:
        filter_body["published"] = True

    body = {
        "limit": limit,
        "page": 1,
        "sort": "-downloads_daily",
        "fields": ["id", "name", "developer_id", "developer_name", "url", "icon", "downloads_daily"],
        "country": country,
        "filter": filter_body,
    }
    headers = {
        "accept": "application/json",
        "API-KEY": APPSTORESPY_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.appstorespy.com/v1/play/apps/query",
            json=body, headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            return None, f"API returned status {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        return data.get("data", []), None
    except Exception as e:
        return None, f"Request failed: {e}"


def discover_games_by_name(name, days_back, limit=100, country="US", pre_register_only=False):
    """Searches AppstoreSpy for games matching a name, within a release-date window."""
    if not APPSTORESPY_API_KEY:
        return None, "No AppstoreSpy API key configured."

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)

    filter_body = {
        "name": name,
        "category_type": "GAME",
        "release_date": {"gte": start.isoformat(), "lte": today.isoformat()},
    }
    if pre_register_only:
        filter_body["pre_register"] = True
        filter_body["published"] = True

    body = {
        "limit": limit,
        "page": 1,
        "sort": "-downloads_daily",
        "fields": ["id", "name", "developer_id", "developer_name", "url", "icon", "downloads_daily"],
        "country": country,
        "filter": filter_body,
    }
    headers = {
        "accept": "application/json",
        "API-KEY": APPSTORESPY_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.appstorespy.com/v1/play/apps/query",
            json=body, headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            return None, f"API returned status {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        return data.get("data", []), None
    except Exception as e:
        return None, f"Request failed: {e}"


def add_own_account(account_label, dev_url):
    """Adds one of the user's own developer accounts directly (no app-ads.txt
    matching needed, since ownership is already known). Pulls the full
    catalog and inserts it, tagged source='own_account'."""
    dev_url = dev_url.strip()
    if not dev_url.startswith("http"):
        return {"status": "error", "message": "Please paste a full developer page URL."}

    dev_id_match = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_url)
    if not dev_id_match:
        return {"status": "error", "message": "Could not find a developer ID in that URL."}
    dev_id = dev_id_match.group(1)

    existing = supabase.table("developers").select("id").eq("dev_id", dev_id).execute()
    if existing.data:
        developer_id = existing.data[0]["id"]
        supabase.table("developers").update({
            "name": account_label, "source": "own_account",
        }).eq("id", developer_id).execute()
    else:
        result = supabase.table("developers").insert({
            "dev_id": dev_id, "name": account_label,
            "developer_url": dev_url, "source": "own_account",
        }).execute()
        developer_id = result.data[0]["id"]

    catalog = fetch_developer_catalog(dev_url, developer_name=account_label)
    inserted = insert_apps(developer_id, catalog)

    return {
        "status": "added",
        "message": f"**{account_label}** added as your own account. {len(catalog)} apps found, {inserted} newly added.",
    }


def run_trace(url):
    package_name = extract_package_name(url)
    if not package_name:
        return {"status": "error", "message": "Could not extract package name from that URL."}

    soup = fetch_app_page(package_name)
    if not soup:
        return {"status": "error", "message": f"Could not load Play Store page for `{package_name}`."}

    website = extract_website(soup)
    if not website:
        return {"status": "warn", "message": f"`{package_name}` has no website listed. Skipped."}

    ads_txt_raw = fetch_app_ads_txt(website)
    if not ads_txt_raw:
        return {"status": "warn", "message": f"No app-ads.txt found at {website}."}

    ads_lines = parse_app_ads_lines(ads_txt_raw)
    known_ids = get_known_ids()
    match = find_match(ads_lines, known_ids)

    if not match:
        return {"status": "no_match", "message": f"No known ad IDs matched (DIRECT) for `{package_name}`."}

    domain, account_id, relationship, studio_id, studio_name = match
    dev_name, dev_link = extract_developer_info(soup)
    if not dev_link:
        return {"status": "error", "message": "Match found but could not extract developer page link."}

    dev_id_match = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    dev_id_str = dev_id_match.group(1) if dev_id_match else dev_link
    existing_dev_check = supabase.table("developers").select("id").eq("dev_id", dev_id_str).execute()
    already_existed = bool(existing_dev_check.data)

    developer_id = upsert_developer(dev_name, dev_link, studio_id=studio_id)
    catalog = fetch_developer_catalog(dev_link, developer_name=dev_name)
    inserted = insert_apps(developer_id, catalog)

    if already_existed:
        return {
            "status": "already_exists",
            "message": f"**{dev_name}** is already in your watchlist (Studio: **{studio_name}**). {len(catalog)} apps found, {inserted} newly added (if any new games were released).",
            "matched_id": account_id,
            "domain": domain,
            "studio_name": studio_name,
        }

    return {
        "status": "match",
        "message": f"Match confirmed — **{dev_name}** added under Studio **{studio_name}**. {len(catalog)} apps found, {inserted} newly added.",
        "matched_id": account_id,
        "domain": domain,
        "studio_name": studio_name,
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Play Store Watchdog")

tab1, tab_spy, tab_search, tab_mine, tab2, tab_short, tab3, tab4, tab_settings = st.tabs(
    ["🔍 Trace", "📈 AppStore Spy", "🔎 Search by Name", "🏢 My Accounts",
     "📋 Watchlist", "⭐ Shortlisted", "🕒 Recent Activity", "🆔 Manage Ad IDs",
     "⚙️ Settings"]
)

# --- Tab 1: Trace ---
with tab1:
    st.subheader("Trace new games")
    st.caption("Paste one URL, or multiple separated by commas.")
    url_input = st.text_area("Paste Play Store game URL(s)", height=100)

    if st.button("Run Trace", type="primary"):
        raw_urls = [u.strip() for u in url_input.split(",") if u.strip()]

        if not raw_urls:
            st.warning("Please paste at least one URL first.")
        else:
            st.write(f"Tracing {len(raw_urls)} URL(s)...")
            progress = st.progress(0)

            for i, single_url in enumerate(raw_urls):
                with st.spinner(f"Tracing {single_url}..."):
                    result = run_trace(single_url)

                label = single_url if len(single_url) < 60 else single_url[:57] + "..."

                if result["status"] == "match":
                    st.success(
                        f"**{label}**\n\n{result['message']}\n\n"
                        f"**Matched ID:** `{result['matched_id']}` on `{result['domain']}` (DIRECT)"
                    )
                elif result["status"] == "already_exists":
                    st.warning(
                        f"**{label}**\n\n{result['message']}\n\n"
                        f"**Matched ID:** `{result['matched_id']}` on `{result['domain']}` (DIRECT)"
                    )
                elif result["status"] == "no_match":
                    st.info(f"**{label}**\n\n{result['message']}")
                elif result["status"] == "warn":
                    st.warning(f"**{label}**\n\n{result['message']}")
                else:
                    st.error(f"**{label}**\n\n{result['message']}")

                progress.progress((i + 1) / len(raw_urls))

            st.success(f"Done — traced {len(raw_urls)} URL(s).")

# --- Tab: AppStore Spy ---
with tab_spy:
    st.subheader("Discover top games (via AppstoreSpy)")
    st.caption("Fetches the top 100 games by daily installs within the selected window. Review the list, then choose whether to trace them.")

    if "spy_results" not in st.session_state:
        st.session_state.spy_results = None
    if "spy_window" not in st.session_state:
        st.session_state.spy_window = None

    pre_reg_only = st.checkbox("Pre-register only", key="spy_pre_reg_checkbox")
    st.caption("Uses AppstoreSpy's 'pre_register' filter field (confirmed via their support team).")

    col1, col2, col3 = st.columns(3)
    fetch_7 = col1.button("📅 Top 100 (7 days)", use_container_width=True, type="primary")
    fetch_30 = col2.button("📅 Top 100 (30 days)", use_container_width=True, type="primary")
    fetch_90 = col3.button("📅 Top 100 (90 days)", use_container_width=True, type="primary")

    col4, col5, col6 = st.columns(3)
    fetch_7_1k = col4.button("📅 Top 1000 (7 days)", use_container_width=True)
    fetch_30_1k = col5.button("📅 Top 1000 (30 days)", use_container_width=True)
    fetch_90_1k = col6.button("📅 Top 1000 (90 days)", use_container_width=True)
    st.caption("⚠️ Top 1000 uses more API credits and tracing all results afterward will take significantly longer.")

    days_back, fetch_limit = None, 100
    if fetch_7:
        days_back, fetch_limit = 7, 100
    elif fetch_30:
        days_back, fetch_limit = 30, 100
    elif fetch_90:
        days_back, fetch_limit = 90, 100
    elif fetch_7_1k:
        days_back, fetch_limit = 7, 1000
    elif fetch_30_1k:
        days_back, fetch_limit = 30, 1000
    elif fetch_90_1k:
        days_back, fetch_limit = 90, 1000

    if days_back:
        with st.spinner(f"Fetching top {fetch_limit} games from the last {days_back} days..."):
            games, error = discover_games(days_back=days_back, limit=fetch_limit, pre_register_only=pre_reg_only)

        if error:
            st.error(error)
            st.session_state.spy_results = None
        elif not games:
            st.info("No games found matching that window.")
            st.session_state.spy_results = None
        else:
            st.session_state.spy_results = games
            st.session_state.spy_window = days_back

    if st.session_state.spy_results:
        games = st.session_state.spy_results
        st.markdown(f"### Results — top {len(games)} games, last {st.session_state.spy_window} days (sorted by daily installs)")

        table_data = [{
            "Icon": g.get("icon"),
            "Name": g.get("name"),
            "Developer": g.get("developer_name"),
            "Daily Installs": g.get("downloads_daily"),
            "Link": ensure_gl_us(g.get("url")) if g.get("url") else None,
        } for g in games]

        st.dataframe(
            table_data,
            column_config={
                "Icon": st.column_config.ImageColumn("Icon", width="small"),
                "Link": st.column_config.LinkColumn("Link", display_text="Open"),
            },
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("---")
        st.write(f"Ready to check all {len(games)} games for app-ads.txt matches against your known ad IDs?")

        if st.button("🔎 Trace these games", type="primary"):
            progress = st.progress(0)
            summary = {"match": 0, "already_exists": 0, "no_match": 0, "other": 0}
            match_details = []

            for i, game in enumerate(games):
                url = game.get("url")
                if not url:
                    summary["other"] += 1
                    progress.progress((i + 1) / len(games))
                    continue

                result = run_trace(url)
                if result["status"] == "match":
                    summary["match"] += 1
                    match_details.append(f"✅ {game.get('name', 'unknown')} — new developer added")
                elif result["status"] == "already_exists":
                    summary["already_exists"] += 1
                elif result["status"] == "no_match":
                    summary["no_match"] += 1
                else:
                    summary["other"] += 1

                progress.progress((i + 1) / len(games))

            st.success(
                f"**Tracing complete:**\n\n"
                f"- New matches added: **{summary['match']}**\n"
                f"- Already in watchlist: **{summary['already_exists']}**\n"
                f"- No match: **{summary['no_match']}**\n"
                f"- Skipped/errors: **{summary['other']}**"
            )
            if match_details:
                st.write("\n".join(match_details))

# --- Tab: Search by Name ---
with tab_search:
    st.subheader("Search games by name (via AppstoreSpy)")
    st.caption("Search for a specific game name on Google Play, filtered by release date window.")

    search_name = st.text_input("Game name", key="search_name_input", placeholder="e.g. Geometry Dash")
    days_option = st.radio("Release date window", ["7 days", "30 days", "90 days"], horizontal=True, key="search_days_option")
    days_map = {"7 days": 7, "30 days": 30, "90 days": 90}
    search_pre_reg_only = st.checkbox("Pre-register only", key="search_pre_reg_checkbox")

    if "search_results" not in st.session_state:
        st.session_state.search_results = None

    if st.button("🔎 Search", type="primary"):
        if not search_name.strip():
            st.warning("Please enter a game name.")
        else:
            with st.spinner(f"Searching for '{search_name}'..."):
                results, error = discover_games_by_name(
                    search_name.strip(), days_map[days_option], limit=100, pre_register_only=search_pre_reg_only
                )

            if error:
                st.error(error)
                st.session_state.search_results = None
            elif not results:
                st.info("No games found matching that search.")
                st.session_state.search_results = None
            else:
                st.session_state.search_results = results

    if st.session_state.search_results:
        results = st.session_state.search_results
        st.markdown(f"### Results — {len(results)} game(s) found")

        table_data = [{
            "Icon": g.get("icon"),
            "Name": g.get("name"),
            "Developer": g.get("developer_name"),
            "Daily Installs": g.get("downloads_daily"),
            "Link": ensure_gl_us(g.get("url")) if g.get("url") else None,
        } for g in results]

        st.dataframe(
            table_data,
            column_config={
                "Icon": st.column_config.ImageColumn("Icon", width="small"),
                "Link": st.column_config.LinkColumn("Link", display_text="Open"),
            },
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("---")
        st.write(f"Ready to check all {len(results)} games for app-ads.txt matches?")

        if st.button("🔎 Trace these games", type="primary", key="trace_search_results"):
            progress = st.progress(0)
            summary = {"match": 0, "already_exists": 0, "no_match": 0, "other": 0}
            match_details = []

            for i, game in enumerate(results):
                url = game.get("url")
                if not url:
                    summary["other"] += 1
                    progress.progress((i + 1) / len(results))
                    continue

                result = run_trace(url)
                if result["status"] == "match":
                    summary["match"] += 1
                    match_details.append(f"✅ {game.get('name', 'unknown')} — new developer added")
                elif result["status"] == "already_exists":
                    summary["already_exists"] += 1
                elif result["status"] == "no_match":
                    summary["no_match"] += 1
                else:
                    summary["other"] += 1

                progress.progress((i + 1) / len(results))

            st.success(
                f"**Tracing complete:**\n\n"
                f"- New matches added: **{summary['match']}**\n"
                f"- Already in watchlist: **{summary['already_exists']}**\n"
                f"- No match: **{summary['no_match']}**\n"
                f"- Skipped/errors: **{summary['other']}**"
            )
            if match_details:
                st.write("\n".join(match_details))

# --- Tab: My Accounts ---
with tab_mine:
    st.subheader("My Accounts")
    st.caption("Add your own Google Play developer accounts here — no app-ads.txt matching needed, since ownership is already known. These get checked every 30 minutes by a separate monitor, with alerts to Discord and Slack.")

    with st.form("add_own_account_form", clear_on_submit=True):
        account_label = st.text_input("Account name (your own label)")
        dev_url = st.text_input("Developer page URL", placeholder="https://play.google.com/store/apps/dev?id=... or /developer?id=...")
        submitted = st.form_submit_button("➕ Add my account", type="primary")

        if submitted:
            if not account_label.strip() or not dev_url.strip():
                st.warning("Both fields are required.")
            else:
                with st.spinner("Fetching account catalog..."):
                    result = add_own_account(account_label.strip(), dev_url.strip())
                if result["status"] == "added":
                    st.success(result["message"])
                else:
                    st.error(result["message"])

    st.markdown("---")
    st.subheader("Your registered accounts")

    own_developers = supabase.table("developers").select("*").eq("source", "own_account").order("first_seen", desc=True).execute().data
    all_apps = supabase.table("apps").select("*").execute().data

    if "confirm_delete_own" not in st.session_state:
        st.session_state.confirm_delete_own = None

    if not own_developers:
        st.info("No own accounts added yet. Use the form above.")

    for dev in own_developers:
        dev_apps = [a for a in all_apps if a["developer_id"] == dev["id"]]
        active_count = len([a for a in dev_apps if a["status"] == "active"])
        removed_count = len([a for a in dev_apps if a["status"] == "removed"])

        with st.expander(f"**{dev['name']}** — {active_count} active, {removed_count} removed ({len(dev_apps)} total)"):
            st.caption(f"Developer page: {dev['developer_url']}")
            st.caption(f"Last checked: {dev.get('last_checked', 'never')}")

            if dev_apps:
                table_data = [{
                    "Icon": a.get("icon_url"),
                    "Title": a["title"],
                    "Package": a["package_name"],
                    "Status": a["status"],
                } for a in dev_apps]
                st.dataframe(
                    table_data,
                    column_config={"Icon": st.column_config.ImageColumn("Icon", width="small")},
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.write("No apps recorded yet.")

            st.markdown("---")
            if st.session_state.confirm_delete_own == dev["id"]:
                st.warning(f"Remove **{dev['name']}** from monitoring? This cannot be undone.")
                dcol1, dcol2 = st.columns(2)
                if dcol1.button("Yes, delete", key=f"own_confirm_yes_{dev['id']}", type="primary"):
                    app_ids = [a["id"] for a in dev_apps]
                    if app_ids:
                        supabase.table("change_log").delete().in_("app_id", app_ids).execute()
                    supabase.table("change_log").delete().eq("developer_id", dev["id"]).execute()
                    supabase.table("apps").delete().eq("developer_id", dev["id"]).execute()
                    supabase.table("developers").delete().eq("id", dev["id"]).execute()
                    st.session_state.confirm_delete_own = None
                    st.rerun()
                if dcol2.button("Cancel", key=f"own_confirm_no_{dev['id']}"):
                    st.session_state.confirm_delete_own = None
                    st.rerun()
            else:
                if st.button("🗑️ Remove from monitoring", key=f"own_delete_{dev['id']}"):
                    st.session_state.confirm_delete_own = dev["id"]
                    st.rerun()

with tab2:
    st.subheader("Watched developers")

    col_refresh, col_recheck = st.columns(2)
    if col_refresh.button("Refresh"):
        st.rerun()

    recheck_clicked = col_recheck.button("🔁 Recheck all accounts against current Ad IDs", type="primary")

    if "confirm_delete_dev" not in st.session_state:
        st.session_state.confirm_delete_dev = None

    developers = supabase.table("developers").select("*").neq("source", "own_account").order("first_seen", desc=True).execute().data
    apps_all = supabase.table("apps").select("*").execute().data

    watchlist_studios = {s["id"]: s["name"] for s in supabase.table("studios").select("id, name").execute().data}

    fcol1, fcol2 = st.columns(2)
    title_search = fcol1.text_input("🔍 Search by game title", key="watchlist_title_search", placeholder="Type to filter games by name...")
    studio_filter = fcol2.selectbox(
        "🏷️ Filter by studio",
        ["All studios"] + sorted(watchlist_studios.values()) + ["Unassigned"],
        key="watchlist_studio_filter",
    )

    if recheck_clicked:
        known_ids = get_known_ids()
        no_longer_matching = []
        no_longer_matching_ids = []
        still_matching = []
        progress = st.progress(0)
        status_text = st.empty()

        for i, dev in enumerate(developers):
            status_text.write(f"Checking {dev['name']}...")
            dev_apps_list = [a for a in apps_all if a["developer_id"] == dev["id"] and a["status"] == "active"]
            found = False

            for app in dev_apps_list:
                soup = fetch_app_page(app["package_name"])
                if not soup:
                    continue
                website = extract_website(soup)
                if not website:
                    continue
                ads_txt = fetch_app_ads_txt(website)
                if not ads_txt:
                    continue
                lines = parse_app_ads_lines(ads_txt)
                if find_match(lines, known_ids):
                    found = True
                    break

            if found:
                still_matching.append(dev["name"])
            else:
                no_longer_matching.append(dev["name"])
                no_longer_matching_ids.append(dev["id"])

            progress.progress((i + 1) / len(developers))

        status_text.empty()
        st.session_state.flagged_dev_ids = no_longer_matching_ids
        st.session_state.flagged_dev_names = no_longer_matching

        if no_longer_matching:
            st.warning(
                f"⚠️ **{len(no_longer_matching)} account(s) no longer match any current Ad ID** "
                f"(likely because you removed the ID they were originally matched on):\n\n"
                + "\n".join(f"- {n}" for n in no_longer_matching)
            )
        else:
            st.success(f"✅ All {len(still_matching)} account(s) still match at least one current Ad ID.")

    if st.session_state.get("flagged_dev_ids"):
        st.markdown("---")
        st.write(f"**{len(st.session_state.flagged_dev_ids)} flagged account(s) ready to remove:**")
        for n in st.session_state.flagged_dev_names:
            st.write(f"- {n}")

        if st.session_state.get("confirm_bulk_delete"):
            st.warning("Are you sure? This will permanently delete all flagged accounts and their apps/history.")
            bcol1, bcol2 = st.columns(2)
            if bcol1.button("Yes, delete all flagged accounts", type="primary"):
                for dev_id in st.session_state.flagged_dev_ids:
                    app_ids = [a["id"] for a in apps_all if a["developer_id"] == dev_id]
                    if app_ids:
                        supabase.table("change_log").delete().in_("app_id", app_ids).execute()
                    supabase.table("change_log").delete().eq("developer_id", dev_id).execute()
                    supabase.table("apps").delete().eq("developer_id", dev_id).execute()
                    supabase.table("developers").delete().eq("id", dev_id).execute()
                st.success(f"Deleted {len(st.session_state.flagged_dev_ids)} account(s).")
                st.session_state.flagged_dev_ids = None
                st.session_state.flagged_dev_names = None
                st.session_state.confirm_bulk_delete = False
                st.rerun()
            if bcol2.button("Cancel"):
                st.session_state.confirm_bulk_delete = False
                st.rerun()
        else:
            if st.button("🗑️ Delete all flagged accounts", type="primary"):
                st.session_state.confirm_bulk_delete = True
                st.rerun()

    if not developers:
        st.info("No developers tracked yet. Use the Trace tab to add one.")

    any_match_shown = False

    for dev in developers:
        dev_studio_label = watchlist_studios.get(dev.get("studio_id"), "Unassigned")
        if studio_filter != "All studios" and dev_studio_label != studio_filter:
            continue

        dev_apps = [a for a in apps_all if a["developer_id"] == dev["id"]]

        if title_search.strip():
            dev_apps = [a for a in dev_apps if title_search.strip().lower() in (a.get("title") or "").lower()]
            if not dev_apps:
                continue  # hide developers with no matching games while searching

        any_match_shown = True
        active_count = len([a for a in dev_apps if a["status"] == "active"])
        removed_count = len([a for a in dev_apps if a["status"] == "removed"])
        studio_label = watchlist_studios.get(dev.get("studio_id"), "Unassigned")

        with st.expander(f"**{dev['name']}** [{studio_label}] — {active_count} active, {removed_count} removed ({len(dev_apps)} total)"):
            st.caption(f"Developer page: {dev['developer_url']}")
            st.caption(f"Studio: {studio_label} | Source: {dev.get('source', 'unknown')} | Last checked: {dev.get('last_checked', 'never')}")

            if dev_apps:
                table_data = [{
                    "Shortlisted": bool(a.get("shortlisted", False)),
                    "Icon": a.get("icon_url"),
                    "Title": a["title"],
                    "Package": a["package_name"],
                    "Status": a["status"],
                    "Installs": a.get("installs_bracket") or "-",
                    "Pre-registration": "Yes" if a.get("is_pre_registration") else "No",
                } for a in dev_apps]

                edited = st.data_editor(
                    table_data,
                    column_config={
                        "Shortlisted": st.column_config.CheckboxColumn("⭐"),
                        "Icon": st.column_config.ImageColumn("Icon", width="small"),
                    },
                    disabled=["Icon", "Title", "Package", "Status", "Installs", "Pre-registration"],
                    use_container_width=True,
                    hide_index=True,
                    key=f"editor_{dev['id']}",
                )

                for i, row in enumerate(edited):
                    original_app = dev_apps[i]
                    if row["Shortlisted"] != bool(original_app.get("shortlisted", False)):
                        supabase.table("apps").update(
                            {"shortlisted": row["Shortlisted"]}
                        ).eq("id", original_app["id"]).execute()
            else:
                st.write("No apps recorded yet.")

            st.markdown("---")

            if st.session_state.confirm_delete_dev == dev["id"]:
                st.warning(f"Remove **{dev['name']}** and all {len(dev_apps)} of its apps from your watchlist? This cannot be undone.")
                col_yes, col_no = st.columns(2)
                if col_yes.button("Yes, delete permanently", key=f"confirm_yes_{dev['id']}", type="primary"):
                    app_ids = [a["id"] for a in dev_apps]
                    if app_ids:
                        supabase.table("change_log").delete().in_("app_id", app_ids).execute()
                    supabase.table("change_log").delete().eq("developer_id", dev["id"]).execute()
                    supabase.table("apps").delete().eq("developer_id", dev["id"]).execute()
                    supabase.table("developers").delete().eq("id", dev["id"]).execute()
                    st.session_state.confirm_delete_dev = None
                    st.success(f"Removed {dev['name']}.")
                    st.rerun()
                if col_no.button("Cancel", key=f"confirm_no_{dev['id']}"):
                    st.session_state.confirm_delete_dev = None
                    st.rerun()
            else:
                if st.button("🗑️ Delete this account", key=f"delete_{dev['id']}"):
                    st.session_state.confirm_delete_dev = dev["id"]
                    st.rerun()

    if title_search.strip() and not any_match_shown:
        st.info(f"No games found matching '{title_search.strip()}'.")

# --- Tab: Shortlisted ---
with tab_short:
    st.subheader("Shortlisted games")
    st.caption("Games you've checked off in the Watchlist tab. Uncheck here to remove from this list (they stay in Watchlist either way).")

    shortlisted_apps = supabase.table("apps").select("*").eq("shortlisted", True).execute().data

    if not shortlisted_apps:
        st.info("No games shortlisted yet. Go to the Watchlist tab and check the ⭐ box next to any game.")
    else:
        developers_lookup = {d["id"]: d["name"] for d in supabase.table("developers").select("id, name").execute().data}

        short_table = [{
            "Shortlisted": True,
            "Icon": a.get("icon_url"),
            "Title": a["title"],
            "Developer": developers_lookup.get(a["developer_id"], "unknown"),
            "Package": a["package_name"],
            "Status": a["status"],
        } for a in shortlisted_apps]

        edited_short = st.data_editor(
            short_table,
            column_config={
                "Shortlisted": st.column_config.CheckboxColumn("⭐"),
                "Icon": st.column_config.ImageColumn("Icon", width="small"),
            },
            disabled=["Icon", "Title", "Developer", "Package", "Status"],
            use_container_width=True,
            hide_index=True,
            key="shortlist_editor",
        )

        for i, row in enumerate(edited_short):
            if not row["Shortlisted"]:
                supabase.table("apps").update({"shortlisted": False}).eq("id", shortlisted_apps[i]["id"]).execute()
                st.rerun()

# --- Tab 3: Recent Activity ---
with tab3:
    st.subheader("Recent Activity")

    event_filter = st.selectbox(
        "Filter by event type",
        ["All", "new_upload", "transferred_in", "transferred", "removed", "listing_changed"],
    )

    query = supabase.table("change_log").select("*").order("detected_at", desc=True).limit(500)
    if event_filter != "All":
        query = query.eq("event_type", event_filter)
    changes = query.execute().data

    if not changes:
        st.info("No activity recorded yet.")
    else:
        # Group consecutive entries by run_id (falls back to per-row if run_id missing, e.g. older data)
        runs = []
        current_run_id = object()
        current_group = None
        for c in changes:
            rid = c.get("run_id") or f"unknown_{c['id']}"
            if rid != current_run_id:
                current_run_id = rid
                current_group = {"run_id": rid, "time": c["detected_at"], "items": []}
                runs.append(current_group)
            current_group["items"].append(c)

        event_icons = {
            "new_upload": "🆕", "transferred_in": "📥", "transferred": "🔀",
            "removed": "🗑️", "listing_changed": "🎨",
        }

        for run in runs:
            display_time = format_pkt(run['time'])
            with st.expander(f"Check run — {display_time}  ({len(run['items'])} change(s))", expanded=False):
                by_dev = {}
                for c in run["items"]:
                    dev_name = c.get("developer_name") or "Unknown developer"
                    by_dev.setdefault(dev_name, []).append(c)

                for dev_name, dev_items in by_dev.items():
                    st.markdown(
                        f"<div style='background-color:#1e2530; padding:10px 14px; "
                        f"border-radius:6px; margin-top:14px; margin-bottom:10px;'>"
                        f"<span style='font-size:22px; font-weight:700;'>🏢 {dev_name}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                    for c in dev_items:
                        package_name = c.get("package_name") or "-"
                        app_title = c.get("app_title") or "unknown"
                        link = f"https://play.google.com/store/apps/details?id={package_name}&gl=us" if package_name != "-" else None
                        icon = event_icons.get(c["event_type"], "•")
                        event_type = c["event_type"]

                        if link:
                            st.markdown(f"{icon} **[{app_title}]({link})** — {event_type}")
                        else:
                            st.markdown(f"{icon} **{app_title}** — {event_type}")
                        st.caption(f"Package: `{package_name}`")

                        old_val = c.get("old_value") or {}
                        new_val = c.get("new_value") or {}

                        if event_type == "listing_changed":
                            lines = []
                            if "title" in new_val:
                                lines.append(f"Title: **{old_val.get('title')}** → **{new_val.get('title')}**")
                            if "icon_url" in new_val:
                                lines.append("Icon changed (see images below)")
                            st.write("\n".join(lines) if lines else "No visible field changes recorded.")
                            if "icon_url" in new_val:
                                ic1, ic2 = st.columns(2)
                                ic1.image(old_val.get("icon_url"), caption="Before", width=80)
                                ic2.image(new_val.get("icon_url"), caption="After", width=80)
                        elif event_type == "transferred":
                            st.write(f"From **{old_val.get('developer')}** → **{new_val.get('developer')}**")
                        elif event_type == "new_upload":
                            st.write("New pre-registration listing appeared.")
                        elif event_type == "transferred_in":
                            st.write("Appeared with existing installs (moved from elsewhere, origin unknown).")
                        elif event_type == "removed":
                            st.write("No longer available under this developer / any watched account.")

                        st.markdown("---")

# --- Tab 4: Manage Ad IDs ---
with tab4:
    st.subheader("Studios")
    st.caption("Group your tracked ad network IDs by studio. Each traced account gets tagged with the studio whose ID matched.")

    studios = supabase.table("studios").select("*").order("name").execute().data
    studio_by_id = {s["id"]: s["name"] for s in studios}

    with st.expander("➕ Add a new studio"):
        with st.form("add_studio_form", clear_on_submit=True):
            new_studio_name = st.text_input("Studio name")
            new_studio_notes = st.text_input("Notes (optional)")
            if st.form_submit_button("Create studio", type="primary"):
                if not new_studio_name.strip():
                    st.warning("Studio name is required.")
                else:
                    existing_studio = supabase.table("studios").select("id").eq("name", new_studio_name.strip()).execute()
                    if existing_studio.data:
                        st.warning(f"A studio named '{new_studio_name.strip()}' already exists.")
                    else:
                        supabase.table("studios").insert({
                            "name": new_studio_name.strip(),
                            "notes": new_studio_notes.strip() or None,
                        }).execute()
                        st.success(f"Studio '{new_studio_name.strip()}' created.")
                        st.rerun()

    if not studios:
        st.info("No studios yet. Create one above before adding ad IDs.")
    else:
        st.markdown("---")
        st.subheader("Add a new ad network ID")
        st.caption("IDs must be globally unique — if an ID already exists under any studio, it won't be added again.")

        with st.form("add_id_form", clear_on_submit=True):
            studio_choice = st.selectbox(
                "Studio", [s["name"] for s in studios], key="add_id_studio_select"
            )
            col1, col2 = st.columns(2)
            with col1:
                network_input = st.text_input("Network domain (e.g. google.com, applovin.com)")
            with col2:
                account_id_input = st.text_input("Account ID (e.g. pub-1234567890123456)")
            label_input = st.text_input("Label (optional note)")

            if st.form_submit_button("Add ID", type="primary"):
                if not network_input.strip() or not account_id_input.strip():
                    st.warning("Both network and account ID are required.")
                else:
                    account_id_clean = account_id_input.strip()
                    existing = supabase.table("ad_network_ids").select(
                        "id, network, studio_id"
                    ).eq("account_id", account_id_clean).execute()

                    if existing.data:
                        owner_studio_id = existing.data[0].get("studio_id")
                        owner_studio = studio_by_id.get(owner_studio_id, "Unassigned")
                        existing_network = existing.data[0].get("network")
                        st.error(
                            f"❌ **Already exists** — `{account_id_clean}` is already tracked "
                            f"under studio **{owner_studio}** (network: `{existing_network}`). "
                            f"IDs must be unique across all studios."
                        )
                    else:
                        target_studio_id = next(s["id"] for s in studios if s["name"] == studio_choice)
                        try:
                            supabase.table("ad_network_ids").insert({
                                "network": network_input.strip(),
                                "account_id": account_id_clean,
                                "label": label_input.strip() or None,
                                "studio_id": target_studio_id,
                            }).execute()
                            st.success(f"Added `{account_id_clean}` under studio **{studio_choice}**.")
                        except Exception as e:
                            st.error(f"Failed to add ID: {e}")

        st.markdown("---")
        st.subheader("Tracked ad network IDs by studio")

        all_ids = supabase.table("ad_network_ids").select("*").order("id", desc=True).execute().data
        all_developers = supabase.table("developers").select("id, name, studio_id").execute().data

        if "editing_id" not in st.session_state:
            st.session_state.editing_id = None

        for studio in studios:
            studio_ids = [r for r in all_ids if r.get("studio_id") == studio["id"]]
            studio_devs = [d for d in all_developers if d.get("studio_id") == studio["id"]]

            with st.expander(
                f"**{studio['name']}** — {len(studio_ids)} ad ID(s), {len(studio_devs)} tracked account(s)"
            ):
                if studio.get("notes"):
                    st.caption(studio["notes"])

                if not studio_ids:
                    st.write("No ad IDs assigned to this studio yet.")
                else:
                    for row in studio_ids:
                        if st.session_state.editing_id == row["id"]:
                            with st.form(f"edit_form_{row['id']}"):
                                ecol1, ecol2 = st.columns(2)
                                with ecol1:
                                    new_network = st.text_input("Network", value=row["network"])
                                with ecol2:
                                    new_account_id = st.text_input("Account ID", value=row["account_id"])
                                new_label = st.text_input("Label", value=row.get("label") or "")
                                new_studio_for_id = st.selectbox(
                                    "Studio", [s["name"] for s in studios],
                                    index=[s["name"] for s in studios].index(studio["name"]),
                                )

                                save_col, cancel_col = st.columns(2)
                                save_clicked = save_col.form_submit_button("Save", type="primary")
                                cancel_clicked = cancel_col.form_submit_button("Cancel")

                                if save_clicked:
                                    if not new_network.strip() or not new_account_id.strip():
                                        st.warning("Network and Account ID cannot be empty.")
                                    else:
                                        clash = supabase.table("ad_network_ids").select("id, studio_id").eq(
                                            "account_id", new_account_id.strip()
                                        ).neq("id", row["id"]).execute()
                                        if clash.data:
                                            clash_studio = studio_by_id.get(clash.data[0].get("studio_id"), "Unassigned")
                                            st.error(f"`{new_account_id.strip()}` already exists under studio **{clash_studio}**.")
                                        else:
                                            target_sid = next(s["id"] for s in studios if s["name"] == new_studio_for_id)
                                            supabase.table("ad_network_ids").update({
                                                "network": new_network.strip(),
                                                "account_id": new_account_id.strip(),
                                                "label": new_label.strip() or None,
                                                "studio_id": target_sid,
                                            }).eq("id", row["id"]).execute()
                                            st.session_state.editing_id = None
                                            st.rerun()

                                if cancel_clicked:
                                    st.session_state.editing_id = None
                                    st.rerun()
                        else:
                            c1, c2, c3, c4, c5 = st.columns([2, 3, 2, 1, 1])
                            c1.write(row["network"])
                            c2.code(row["account_id"])
                            c3.write(row.get("label") or "-")
                            if c4.button("Edit", key=f"edit_{row['id']}"):
                                st.session_state.editing_id = row["id"]
                                st.rerun()
                            if c5.button("Delete", key=f"del_{row['id']}"):
                                supabase.table("ad_network_ids").delete().eq("id", row["id"]).execute()
                                st.rerun()

                if studio_devs:
                    st.markdown("**Tracked accounts under this studio:**")
                    for d in studio_devs:
                        st.write(f"- {d['name']}")

        unassigned_ids = [r for r in all_ids if not r.get("studio_id")]
        if unassigned_ids:
            with st.expander(f"⚠️ Unassigned IDs ({len(unassigned_ids)})"):
                st.caption("These IDs aren't linked to any studio. Use Edit to assign them.")
                for row in unassigned_ids:
                    uc1, uc2, uc3 = st.columns([2, 3, 1])
                    uc1.write(row["network"])
                    uc2.code(row["account_id"])
                    if uc3.button("Edit", key=f"unassigned_edit_{row['id']}"):
                        st.session_state.editing_id = row["id"]
                        st.rerun()

# --- Tab: Settings ---
with tab_settings:
    user = st.session_state.sb_user
    profile = auth.current_profile() or {}

    st.subheader("Notifications")
    st.caption(
        "Your Slack webhook is stored against your account, so alerts for your "
        "watchlist and your own accounts go to your channel. Nothing here is "
        "hard-coded any more."
    )

    try:
        settings_rows = supabase.table("user_settings").select("*") \
            .eq("user_id", user["id"]).execute().data
        settings = settings_rows[0] if settings_rows else {}
    except Exception as e:
        settings = {}
        st.error(f"Could not load your settings: {e}")

    with st.form("slack_settings_form"):
        st.markdown("**Watchlist channel** — competitor tracking, checked every 2 hours")
        webhook = st.text_input(
            "Slack webhook URL (watchlist)",
            value=settings.get("slack_webhook_url") or "",
            placeholder="https://hooks.slack.com/services/...",
            help="Slack → your app → Incoming Webhooks → Add New Webhook to Workspace",
        )

        st.markdown("**My Accounts channel** — your own accounts, checked every 30 minutes")
        webhook_own = st.text_input(
            "Slack webhook URL (my accounts)",
            value=settings.get("slack_webhook_url_own") or "",
            placeholder="Leave blank to reuse the watchlist channel",
            help="Keeping this separate stops urgent removals on your own accounts "
                 "getting buried in routine competitor noise.",
        )

        enabled = st.checkbox(
            "Send me notifications", value=bool(settings.get("slack_enabled", True))
        )

        st.markdown("**Alert me about**")
        c1, c2 = st.columns(2)
        n_new = c1.checkbox("New uploads", value=bool(settings.get("notify_new_upload", True)))
        n_removed = c1.checkbox("Removals", value=bool(settings.get("notify_removed", True)))
        n_transfer = c2.checkbox("Transfers", value=bool(settings.get("notify_transferred", True)))
        n_listing = c2.checkbox("Listing changes", value=bool(settings.get("notify_listing_changed", True)))
        n_quiet = st.checkbox(
            "Also tell me when a check found nothing",
            value=bool(settings.get("notify_nothing_new", True)),
            help="Turn this off if the 'nothing new' pings every 30 minutes get noisy.",
        )

        if st.form_submit_button("Save settings", type="primary"):
            clean = webhook.strip()
            clean_own = webhook_own.strip()
            bad = [u for u in (clean, clean_own)
                   if u and not u.startswith("https://hooks.slack.com/")]
            if bad:
                st.error("That doesn't look like a Slack webhook URL — it should start with https://hooks.slack.com/")
            else:
                try:
                    supabase.table("user_settings").upsert({
                        "user_id": user["id"],
                        "slack_webhook_url": clean or None,
                        "slack_webhook_url_own": clean_own or None,
                        "slack_enabled": enabled,
                        "notify_new_upload": n_new,
                        "notify_removed": n_removed,
                        "notify_transferred": n_transfer,
                        "notify_listing_changed": n_listing,
                        "notify_nothing_new": n_quiet,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }, on_conflict="user_id").execute()
                    st.success("Settings saved.")
                except Exception as e:
                    st.error(f"Could not save: {e}")

    def _slack_test(url, label, key):
        if st.button(f"Send test to {label}", key=key):
            try:
                r = requests.post(
                    url,
                    json={"text": f":wave: Test from Play Store Watchdog — "
                                  f"your *{label}* webhook works."},
                    timeout=15,
                )
                if r.status_code < 300:
                    st.success(f"Sent — check your {label} channel.")
                else:
                    st.error(f"Slack rejected it (HTTP {r.status_code}): {r.text[:200]}")
            except Exception as e:
                st.error(f"Could not reach Slack: {e}")

    tcol1, tcol2 = st.columns(2)
    with tcol1:
        if settings.get("slack_webhook_url"):
            _slack_test(settings["slack_webhook_url"], "watchlist", "test_wl")
    with tcol2:
        if settings.get("slack_webhook_url_own"):
            _slack_test(settings["slack_webhook_url_own"], "my accounts", "test_own")
        elif settings.get("slack_webhook_url"):
            st.caption("My Accounts alerts will use the watchlist channel.")

    st.divider()
    st.subheader("Change your password")

    with st.form("change_password_form", clear_on_submit=True):
        new_pw = st.text_input("New password", type="password")
        confirm_pw = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password"):
            ok, msg = auth.change_password(new_pw, confirm_pw)
            (st.success if ok else st.error)(msg)

    if auth.is_admin():
        st.divider()
        st.subheader("User management")
        st.caption("Create accounts for other people. They sign in with the username "
                   "and password you set, then change their own password here.")

        with st.form("create_user_form", clear_on_submit=True):
            nu_name = st.text_input("Username", placeholder="e.g. ali.hassan")
            nu_pass = st.text_input("Initial password", type="password")
            nu_admin = st.checkbox("Make this user an administrator")
            if st.form_submit_button("Create user", type="primary"):
                ok, msg = auth.create_user(SUPABASE_URL, SUPABASE_KEY,
                                           nu_name, nu_pass, nu_admin)
                (st.success if ok else st.error)(msg)

        existing = auth.list_users(SUPABASE_URL, SUPABASE_KEY)
        if existing:
            st.dataframe(
                [{"Username": u.get("username"),
                  "Admin": "Yes" if u.get("is_admin") else "No",
                  "Created": (u.get("created_at") or "")[:10]} for u in existing],
                use_container_width=True, hide_index=True,
            )