"""
monitor.py — Play Store Watchdog: Monitoring Tool

Runs on a schedule (every 2 hours via GitHub Actions).
For every watched developer:
  1. Re-fetches their full catalog (list of apps)
  2. Compares against stored DB state to find:
       - new_upload         (brand new package, in pre-registration)
       - transferred_in     (brand new package, already has installs)
       - transferred         (existing package now under a different watched developer)
       - listing_changed     (title or icon changed)
For apps that vanished from their developer's catalog:
       - checks the app page directly (gl=us, then pk/gb/in/ca fallback)
       - if it now shows a DIFFERENT (possibly unwatched) developer -> transferred,
         auto-adds the new developer + their full catalog if not already watched
       - if it fails to load everywhere -> marked removed (rechecked only every 24h after that)

Sends ONE Discord digest message per run, grouped by event type.
Always sends a message, even if nothing changed.
"""

import os
import re
import io
import hashlib
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client
from PIL import Image
import imagehash

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

FALLBACK_COUNTRIES = ["us", "pk", "gb", "in", "ca"]
REMOVED_RECHECK_HOURS = 24

COLOR_NEW = 0x2ecc71
COLOR_REMOVED = 0xe74c3c
COLOR_TRANSFER = 0xe67e22
COLOR_LISTING = 0x3498db
COLOR_INFO = 0x95a5a6


def fetch_app_page(package_name, country="us"):
    url = f"https://play.google.com/store/apps/details?id={package_name}&gl={country}&hl=en"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


def extract_developer_info(soup):
    dev_link = None
    dev_name = None
    for a in soup.find_all("a", href=True):
        if "/store/apps/developer?id=" in a["href"] or "/store/apps/dev?id=" in a["href"]:
            dev_link = "https://play.google.com" + a["href"] if a["href"].startswith("/") else a["href"]
            dev_name = a.get_text(strip=True) or dev_name
            break
    return dev_name, dev_link


def extract_dev_id_from_link(dev_link):
    m = re.search(r"[?&]id=([a-zA-Z0-9._+-]+)", dev_link)
    return m.group(1) if m else dev_link


def extract_install_info(soup):
    page_text = soup.get_text(" ", strip=True)
    if re.search(r"pre-?register", page_text, re.IGNORECASE):
        return True, None
    m = re.search(r"([\d.,]+[KMB]?\+)\s*Downloads", page_text, re.IGNORECASE)
    if m:
        return False, m.group(1)
    return False, None


def extract_canonical_title(soup):
    """
    The catalog listing page mashes title+rating+developer name together with
    no separators, and the exact pattern varies per developer's page template.
    Instead of trying to regex-parse that reliably, pull the title from the
    app's own detail page <title> tag, which Google keeps clean for SEO
    (e.g. "Police Simulator: Real Chase - Apps on Google Play").
    """
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def normalize_title(raw_title, developer_name=None):
    """
    Play Store catalog listings mash together the title, sometimes a duplicate
    accessibility copy of the title, a star rating, and occasionally the
    developer name — all with no separators, e.g.:
      "Police Simulator: Real Chase Police Simulator: Real Chase2.9star"
      "Miami Gangster Sim Mafia 3DBisma Apps Hub4.1star"
    This strips all of that down to just the real title so comparisons don't
    false-positive every time a rating shifts by a decimal point.
    """
    if not raw_title:
        return raw_title
    text = raw_title.strip()

    text = re.sub(r'^(icon image|app icon)\s*', '', text, flags=re.IGNORECASE).strip()

    # aria-label style: "Title, Rated 4.5 stars, Free, ..." -> keep only the first part
    if "," in text:
        text = text.split(",")[0].strip()

    # Strip trailing rating patterns like "4.1star", "4.1 stars", repeatedly just in case
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r'\d+(?:\.\d+)?\s*(?:stars?|★)\s*$', '', text, flags=re.IGNORECASE).strip()

    # Collapse an immediately duplicated title: "Title Title" -> "Title"
    m = re.match(r'^(.+?)\s+\1$', text, flags=re.IGNORECASE)
    if m:
        text = m.group(1).strip()

    # If the developer name got glued onto the end (no separator), strip it
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
    try:
        resp = requests.get(dev_link, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    apps = []
    seen = set()
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


def get_icon_hash(icon_url):
    """
    Perceptual hash instead of exact byte hash — Play Store's CDN sometimes
    re-encodes the same icon slightly differently between requests, which
    caused false 'icon changed' events even when visually identical.
    Perceptual hashing tolerates that kind of minor recompression noise.
    """
    if not icon_url:
        return None
    try:
        resp = requests.get(icon_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        return str(imagehash.phash(img))
    except Exception:
        return None


def icons_differ(old_hash_str, new_hash_str, threshold=6):
    """True only if the perceptual hashes differ by more than `threshold` bits —
    small differences (compression noise) are ignored; only real visual changes count."""
    if not old_hash_str or not new_hash_str:
        return False
    try:
        old_h = imagehash.hex_to_hash(old_hash_str)
        new_h = imagehash.hex_to_hash(new_hash_str)
        return (old_h - new_h) > threshold
    except Exception:
        # old hash might be from before this fix (different format) — can't compare, assume no change
        return False


def check_app_directly(package_name, last_status, last_seen_iso):
    if last_status == "removed" and last_seen_iso:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if datetime.now(timezone.utc) - last_seen < timedelta(hours=REMOVED_RECHECK_HOURS):
            return "still_removed", None

    for country in FALLBACK_COUNTRIES:
        soup = fetch_app_page(package_name, country)
        if soup:
            dev_name, dev_link = extract_developer_info(soup)
            if dev_link:
                return "found", (dev_name, dev_link)
            return "found", (None, None)

    return "removed", None


def send_digest(events, run_id=None):
    total = sum(len(v) for v in events.values())
    footer = {"text": f"Check run: {run_id}"} if run_id else None

    if total == 0:
        embed = {
            "title": "Watchdog check complete",
            "description": "Nothing new this cycle. All watched accounts checked.",
            "color": COLOR_INFO,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if footer:
            embed["footer"] = footer
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        return

    embeds = []

    def play_link(package_name):
        return f"https://play.google.com/store/apps/details?id={package_name}&gl=us"

    for ev in events.get("new_upload", []):
        link = play_link(ev["package_name"])
        embed = {
            "title": f"New upload: {ev['title']}",
            "url": link,
            "description": f"Package: `{ev['package_name']}`\nDeveloper: {ev['developer_name']}\n[Open on Play Store]({link})",
            "color": COLOR_NEW,
        }
        if ev.get("icon_url"):
            embed["thumbnail"] = {"url": ev["icon_url"]}
        embeds.append(embed)

    for ev in events.get("transferred_in", []):
        link = play_link(ev["package_name"])
        embed = {
            "title": f"Transferred in (already has installs): {ev['title']}",
            "url": link,
            "description": (f"Package: `{ev['package_name']}`\nNow under: {ev['developer_name']}\n"
                             f"Origin: {ev.get('origin', 'unknown')}\n[Open on Play Store]({link})"),
            "color": COLOR_TRANSFER,
        }
        if ev.get("icon_url"):
            embed["thumbnail"] = {"url": ev["icon_url"]}
        embeds.append(embed)

    for ev in events.get("transferred", []):
        link = play_link(ev["package_name"])
        embeds.append({
            "title": f"Transferred: {ev['title']}",
            "url": link,
            "description": (f"Package: `{ev['package_name']}`\nFrom: {ev['old_dev']}\nTo: {ev['new_dev']}\n"
                             f"[Open on Play Store]({link})"),
            "color": COLOR_TRANSFER,
        })

    for ev in events.get("removed", []):
        link = play_link(ev["package_name"])
        embeds.append({
            "title": f"Removed: {ev['title']}",
            "description": (f"Package: `{ev['package_name']}`\nLast known developer: {ev['developer_name']}\n"
                             f"[Last known Play Store link]({link}) (likely dead now)"),
            "color": COLOR_REMOVED,
        })

    for ev in events.get("listing_changed", []):
        link = play_link(ev["package_name"])
        desc_lines = []
        if ev.get("title_changed"):
            desc_lines.append(f"Title: **{ev['old_title']}** -> **{ev['new_title']}**")
        if ev.get("icon_changed"):
            desc_lines.append("Icon changed (see images below)")
        desc_lines.append(f"Package: `{ev['package_name']}`")
        desc_lines.append(f"[Open on Play Store]({link})")

        embed = {"title": "Listing changed", "url": link, "description": "\n".join(desc_lines), "color": COLOR_LISTING}
        if ev.get("icon_changed"):
            if ev.get("old_icon_url"):
                embed["thumbnail"] = {"url": ev["old_icon_url"]}
            if ev.get("new_icon_url"):
                embed["image"] = {"url": ev["new_icon_url"]}
        embeds.append(embed)

    if footer and embeds:
        embeds[-1]["footer"] = footer

    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": batch}, timeout=15)


def upsert_developer(dev_name, dev_link):
    dev_id = extract_dev_id_from_link(dev_link)
    existing = supabase.table("developers").select("id").eq("dev_id", dev_id).execute()
    if existing.data:
        return existing.data[0]["id"]
    result = supabase.table("developers").insert({
        "dev_id": dev_id,
        "name": dev_name,
        "developer_url": dev_link,
        "source": "transfer",
    }).execute()
    return result.data[0]["id"]


def log_change(app_id, developer_id, event_type, old_value, new_value,
                run_id=None, developer_name=None, app_title=None, package_name=None):
    supabase.table("change_log").insert({
        "app_id": app_id,
        "developer_id": developer_id,
        "event_type": event_type,
        "old_value": old_value,
        "new_value": new_value,
        "run_id": run_id,
        "developer_name": developer_name,
        "app_title": app_title,
        "package_name": package_name,
    }).execute()


def main():
    run_id = datetime.now(timezone.utc).isoformat()

    events = {
        "new_upload": [], "transferred_in": [], "transferred": [],
        "removed": [], "listing_changed": [],
    }

    developers_resp = supabase.table("developers").select("*").execute()
    developers = {d["id"]: d for d in developers_resp.data}

    apps_resp = supabase.table("apps").select("*").execute()
    db_apps = {a["package_name"]: a for a in apps_resp.data}

    fresh_map = {}

    for dev_id, dev in developers.items():
        catalog = fetch_developer_catalog(dev["developer_url"], developer_name=dev["name"])
        supabase.table("developers").update({
            "last_checked": datetime.now(timezone.utc).isoformat()
        }).eq("id", dev_id).execute()

        for app in catalog:
            fresh_map[app["package_name"]] = {
                "developer_id": dev_id,
                "developer_name": dev["name"],
                "title": app["title"],
                "icon_url": app["icon_url"],
            }

    for package_name, fresh in fresh_map.items():
        stored = db_apps.get(package_name)

        if stored is None:
            soup = fetch_app_page(package_name)
            is_pre_reg, installs = (True, None)
            if soup:
                is_pre_reg, installs = extract_install_info(soup)

            canonical_title = extract_canonical_title(soup) if soup else None
            final_title = canonical_title or fresh["title"]

            icon_hash = get_icon_hash(fresh["icon_url"])
            insert_res = supabase.table("apps").insert({
                "package_name": package_name,
                "developer_id": fresh["developer_id"],
                "title": final_title,
                "icon_url": fresh["icon_url"],
                "icon_hash": icon_hash,
                "installs_bracket": installs,
                "is_pre_registration": is_pre_reg,
                "status": "active",
            }).execute()
            new_app_id = insert_res.data[0]["id"]

            if is_pre_reg:
                events["new_upload"].append({
                    "package_name": package_name, "title": final_title,
                    "developer_name": fresh["developer_name"], "icon_url": fresh["icon_url"],
                })
                log_change(new_app_id, fresh["developer_id"], "new_upload", None, {"title": final_title},
                           run_id=run_id, developer_name=fresh["developer_name"],
                           app_title=final_title, package_name=package_name)
            else:
                events["transferred_in"].append({
                    "package_name": package_name, "title": final_title,
                    "developer_name": fresh["developer_name"], "icon_url": fresh["icon_url"],
                    "origin": "unknown",
                })
                log_change(new_app_id, fresh["developer_id"], "transferred_in", None, {"title": final_title},
                           run_id=run_id, developer_name=fresh["developer_name"],
                           app_title=final_title, package_name=package_name)
            continue

        if stored["developer_id"] != fresh["developer_id"]:
            old_dev_name = developers.get(stored["developer_id"], {}).get("name", "unknown")
            new_dev_name = fresh["developer_name"]
            supabase.table("apps").update({
                "developer_id": fresh["developer_id"],
                "status": "active",
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }).eq("id", stored["id"]).execute()
            events["transferred"].append({
                "package_name": package_name, "title": fresh["title"],
                "old_dev": old_dev_name, "new_dev": new_dev_name,
            })
            log_change(stored["id"], fresh["developer_id"], "transferred",
                       {"developer": old_dev_name}, {"developer": new_dev_name},
                       run_id=run_id, developer_name=new_dev_name,
                       app_title=fresh["title"], package_name=package_name)
            continue

        new_icon_hash = get_icon_hash(fresh["icon_url"])
        candidate_title_changed = stored["title"] != fresh["title"]
        old_icon_hash = stored.get("icon_hash")
        # Old entries may have an exact-byte hash (32 hex chars) from before this fix;
        # can't meaningfully compare that to the new perceptual hash format, so just
        # silently re-baseline it this cycle instead of comparing (and instead of
        # permanently failing to detect icon changes on that app going forward).
        needs_rebaseline = bool(old_icon_hash) and bool(new_icon_hash) and len(old_icon_hash) != len(new_icon_hash)
        icon_changed = False if needs_rebaseline else icons_differ(old_icon_hash, new_icon_hash)

        update_fields = {"last_seen": datetime.now(timezone.utc).isoformat(), "status": "active"}
        title_changed = False
        confirmed_new_title = fresh["title"]

        if candidate_title_changed:
            # Catalog text looked different — confirm against the canonical detail-page title
            # before trusting it, since catalog markup varies and can produce false positives.
            detail_soup = fetch_app_page(package_name)
            canonical_title = extract_canonical_title(detail_soup) if detail_soup else None
            if canonical_title:
                confirmed_new_title = canonical_title
                title_changed = stored["title"] != canonical_title
            else:
                # Couldn't confirm — fall back to trusting the (possibly noisy) catalog comparison
                title_changed = candidate_title_changed

        if title_changed or icon_changed:
            old_value = {}
            new_value = {}
            if title_changed:
                old_value["title"] = stored["title"]
                new_value["title"] = confirmed_new_title
            if icon_changed:
                old_value["icon_url"] = stored.get("icon_url")
                new_value["icon_url"] = fresh["icon_url"]

            events["listing_changed"].append({
                "package_name": package_name,
                "title_changed": title_changed,
                "icon_changed": icon_changed,
                "old_title": stored["title"], "new_title": confirmed_new_title,
                "old_icon_url": stored.get("icon_url"), "new_icon_url": fresh["icon_url"],
            })
            log_change(stored["id"], fresh["developer_id"], "listing_changed", old_value, new_value,
                       run_id=run_id, developer_name=fresh["developer_name"],
                       app_title=confirmed_new_title, package_name=package_name)
            if title_changed:
                update_fields["title"] = confirmed_new_title
            if icon_changed:
                update_fields["icon_url"] = fresh["icon_url"]
                update_fields["icon_hash"] = new_icon_hash
        elif new_icon_hash and (not old_icon_hash or needs_rebaseline):
            # First time we're able to compute a hash, or migrating from the old
            # hash format — store it silently, no event
            update_fields["icon_hash"] = new_icon_hash

        supabase.table("apps").update(update_fields).eq("id", stored["id"]).execute()

    for package_name, stored in db_apps.items():
        if package_name in fresh_map:
            continue

        result, payload = check_app_directly(package_name, stored["status"], stored.get("last_seen"))

        if result == "still_removed":
            continue

        if result == "removed":
            if stored["status"] != "removed":
                dev_name = developers.get(stored["developer_id"], {}).get("name", "unknown")
                events["removed"].append({
                    "package_name": package_name, "title": stored["title"], "developer_name": dev_name,
                })
                log_change(stored["id"], stored["developer_id"], "removed",
                           {"title": stored["title"]}, None,
                           run_id=run_id, developer_name=dev_name,
                           app_title=stored["title"], package_name=package_name)
            supabase.table("apps").update({
                "status": "removed",
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }).eq("id", stored["id"]).execute()
            continue

        if result == "found":
            dev_name, dev_link = payload
            if not dev_link:
                supabase.table("apps").update({
                    "last_seen": datetime.now(timezone.utc).isoformat()
                }).eq("id", stored["id"]).execute()
                continue

            new_dev_id_str = extract_dev_id_from_link(dev_link)
            old_dev = developers.get(stored["developer_id"], {})

            if new_dev_id_str == old_dev.get("dev_id"):
                supabase.table("apps").update({
                    "status": "active",
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                }).eq("id", stored["id"]).execute()
                continue

            existing_dev = supabase.table("developers").select("*").eq("dev_id", new_dev_id_str).execute()
            if existing_dev.data:
                new_developer_id = existing_dev.data[0]["id"]
            else:
                new_developer_id = upsert_developer(dev_name, dev_link)
                new_catalog = fetch_developer_catalog(dev_link, developer_name=dev_name)
                for a in new_catalog:
                    if a["package_name"] == package_name:
                        continue
                    already = supabase.table("apps").select("id").eq("package_name", a["package_name"]).execute()
                    if already.data:
                        continue
                    supabase.table("apps").insert({
                        "package_name": a["package_name"],
                        "developer_id": new_developer_id,
                        "title": a["title"],
                        "icon_url": a["icon_url"],
                        "status": "active",
                    }).execute()

            supabase.table("apps").update({
                "developer_id": new_developer_id,
                "status": "active",
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }).eq("id", stored["id"]).execute()

            events["transferred"].append({
                "package_name": package_name, "title": stored["title"],
                "old_dev": old_dev.get("name", "unknown"), "new_dev": dev_name,
            })
            log_change(stored["id"], new_developer_id, "transferred",
                       {"developer": old_dev.get("name", "unknown")}, {"developer": dev_name},
                       run_id=run_id, developer_name=dev_name,
                       app_title=stored["title"], package_name=package_name)

    send_digest(events, run_id)
    print("[info] Monitor cycle complete.")


if __name__ == "__main__":
    main()