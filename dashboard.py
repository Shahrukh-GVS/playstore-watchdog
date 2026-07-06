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

st.set_page_config(page_title="Play Store Watchdog", layout="wide")

SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))
DISCORD_WEBHOOK_URL = st.secrets.get("DISCORD_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL"))
APPSTORESPY_API_KEY = st.secrets.get("APPSTORESPY_API_KEY", os.getenv("APPSTORESPY_API_KEY"))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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
    result = supabase.table("ad_network_ids").select("account_id").execute()
    return {row["account_id"] for row in result.data}


def find_match(ads_lines, known_ids):
    for domain, account_id, relationship in ads_lines:
        if account_id in known_ids and relationship.upper() == "DIRECT":
            return (domain, account_id, relationship)
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


def fetch_developer_catalog(dev_link, developer_name=None):
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


def upsert_developer(dev_name, dev_link):
    m = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    dev_id = m.group(1) if m else dev_link
    existing = supabase.table("developers").select("id").eq("dev_id", dev_id).execute()
    if existing.data:
        return existing.data[0]["id"]
    result = supabase.table("developers").insert({
        "dev_id": dev_id, "name": dev_name, "developer_url": dev_link, "source": "trace",
    }).execute()
    return result.data[0]["id"]


def extract_canonical_title(soup):
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def insert_apps(developer_id, apps):
    inserted = 0
    for app in apps:
        existing = supabase.table("apps").select("id").eq("package_name", app["package_name"]).execute()
        if existing.data:
            continue

        detail_soup = fetch_app_page(app["package_name"])
        canonical_title = extract_canonical_title(detail_soup) if detail_soup else None
        final_title = canonical_title or app["title"]

        supabase.table("apps").insert({
            "package_name": app["package_name"], "developer_id": developer_id,
            "title": final_title, "icon_url": app["icon_url"], "status": "active",
        }).execute()
        inserted += 1
    return inserted


def discover_games(days_back, limit=100, country="US"):
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

    body = {
        "limit": limit,
        "page": 1,
        "sort": "-downloads_daily",
        "fields": ["id", "name", "developer_id", "developer_name", "url", "icon", "downloads_daily"],
        "country": country,
        "filter": {
            "published": True,
            "category_type": "GAME",
            "release_date": {"gte": start.isoformat(), "lte": today.isoformat()},
        },
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
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception:
        pass


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

    domain, account_id, relationship = match
    dev_name, dev_link = extract_developer_info(soup)
    if not dev_link:
        return {"status": "error", "message": "Match found but could not extract developer page link."}

    dev_id_match = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    dev_id_str = dev_id_match.group(1) if dev_id_match else dev_link
    existing_dev_check = supabase.table("developers").select("id").eq("dev_id", dev_id_str).execute()
    already_existed = bool(existing_dev_check.data)

    developer_id = upsert_developer(dev_name, dev_link)
    catalog = fetch_developer_catalog(dev_link, developer_name=dev_name)
    inserted = insert_apps(developer_id, catalog)

    if already_existed:
        return {
            "status": "already_exists",
            "message": f"**{dev_name}** is already in your watchlist. {len(catalog)} apps found, {inserted} newly added (if any new games were released).",
            "matched_id": account_id,
            "domain": domain,
        }

    return {
        "status": "match",
        "message": f"Match confirmed — **{dev_name}** added. {len(catalog)} apps found, {inserted} newly added.",
        "matched_id": account_id,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Play Store Watchdog")

tab1, tab_spy, tab2, tab3, tab4 = st.tabs(
    ["🔍 Trace", "📈 AppStore Spy", "📋 Watchlist", "🕒 Recent Activity", "🆔 Manage Ad IDs"]
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

    col1, col2, col3 = st.columns(3)
    fetch_7 = col1.button("📅 Top 100 (7 days)", use_container_width=True, type="primary")
    fetch_30 = col2.button("📅 Top 100 (30 days)", use_container_width=True, type="primary")
    fetch_90 = col3.button("📅 Top 100 (90 days)", use_container_width=True, type="primary")

    if fetch_7 or fetch_30 or fetch_90:
        days_back = 7 if fetch_7 else (30 if fetch_30 else 90)
        with st.spinner(f"Fetching top 100 games from the last {days_back} days..."):
            games, error = discover_games(days_back=days_back, limit=100)

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
            "Link": g.get("url"),
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
with tab2:
    st.subheader("Watched developers")

    col_refresh, col_recheck = st.columns(2)
    if col_refresh.button("Refresh"):
        st.rerun()

    recheck_clicked = col_recheck.button("🔁 Recheck all accounts against current Ad IDs", type="primary")

    if "confirm_delete_dev" not in st.session_state:
        st.session_state.confirm_delete_dev = None

    developers = supabase.table("developers").select("*").order("first_seen", desc=True).execute().data
    apps_all = supabase.table("apps").select("*").execute().data

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

    for dev in developers:
        dev_apps = [a for a in apps_all if a["developer_id"] == dev["id"]]
        active_count = len([a for a in dev_apps if a["status"] == "active"])
        removed_count = len([a for a in dev_apps if a["status"] == "removed"])

        with st.expander(f"**{dev['name']}** — {active_count} active, {removed_count} removed ({len(dev_apps)} total)"):
            st.caption(f"Developer page: {dev['developer_url']}")
            st.caption(f"Source: {dev.get('source', 'unknown')} | Last checked: {dev.get('last_checked', 'never')}")

            if dev_apps:
                table_data = [{
                    "Icon": a.get("icon_url"),
                    "Title": a["title"],
                    "Package": a["package_name"],
                    "Status": a["status"],
                    "Installs": a.get("installs_bracket") or "-",
                    "Pre-registration": "Yes" if a.get("is_pre_registration") else "No",
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
    st.subheader("Add a new ad network ID")
    st.caption("These are the IDs used to match against app-ads.txt during tracing (exact ID + DIRECT relationship required).")

    with st.form("add_id_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            network_input = st.text_input("Network domain (e.g. google.com, applovin.com, facebook.com)")
        with col2:
            account_id_input = st.text_input("Account ID (e.g. pub-1234567890123456, or the raw ID)")
        label_input = st.text_input("Label (optional note, e.g. 'Main AdMob account')")

        submitted = st.form_submit_button("Add ID", type="primary")

        if submitted:
            if not network_input.strip() or not account_id_input.strip():
                st.warning("Both network and account ID are required.")
            else:
                try:
                    existing = supabase.table("ad_network_ids") \
                        .select("id") \
                        .eq("network", network_input.strip()) \
                        .eq("account_id", account_id_input.strip()) \
                        .execute()
                    if existing.data:
                        st.info("This network + ID combination is already saved.")
                    else:
                        supabase.table("ad_network_ids").insert({
                            "network": network_input.strip(),
                            "account_id": account_id_input.strip(),
                            "label": label_input.strip() or None,
                        }).execute()
                        st.success(f"Added `{account_id_input.strip()}` under `{network_input.strip()}`.")
                except Exception as e:
                    st.error(f"Failed to add ID: {e}")

    st.divider()
    st.subheader("Currently tracked ad network IDs")

    ids_data = supabase.table("ad_network_ids").select("*").order("id", desc=True).execute().data

    if not ids_data:
        st.info("No ad network IDs added yet.")
    else:
        if "editing_id" not in st.session_state:
            st.session_state.editing_id = None

        for row in ids_data:
            if st.session_state.editing_id == row["id"]:
                # Edit mode for this row
                with st.form(f"edit_form_{row['id']}"):
                    col1, col2 = st.columns(2)
                    with col1:
                        new_network = st.text_input("Network", value=row["network"])
                    with col2:
                        new_account_id = st.text_input("Account ID", value=row["account_id"])
                    new_label = st.text_input("Label", value=row.get("label") or "")

                    save_col, cancel_col = st.columns(2)
                    save_clicked = save_col.form_submit_button("Save", type="primary")
                    cancel_clicked = cancel_col.form_submit_button("Cancel")

                    if save_clicked:
                        if not new_network.strip() or not new_account_id.strip():
                            st.warning("Network and Account ID cannot be empty.")
                        else:
                            supabase.table("ad_network_ids").update({
                                "network": new_network.strip(),
                                "account_id": new_account_id.strip(),
                                "label": new_label.strip() or None,
                            }).eq("id", row["id"]).execute()
                            st.session_state.editing_id = None
                            st.rerun()

                    if cancel_clicked:
                        st.session_state.editing_id = None
                        st.rerun()
            else:
                # Normal display row
                col1, col2, col3, col4, col5 = st.columns([2, 3, 2, 1, 1])
                col1.write(row["network"])
                col2.code(row["account_id"])
                col3.write(row.get("label") or "-")
                if col4.button("Edit", key=f"edit_{row['id']}"):
                    st.session_state.editing_id = row["id"]
                    st.rerun()
                if col5.button("Delete", key=f"del_{row['id']}"):
                    supabase.table("ad_network_ids").delete().eq("id", row["id"]).execute()
                    st.rerun()