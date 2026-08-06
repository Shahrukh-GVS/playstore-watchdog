"""
monitor_own_accounts.py — Play Store Watchdog: Own Accounts Monitor

Runs every 30 minutes (separate, faster cycle from the main monitor.py,
which handles the larger set of traced/competitor accounts on a 2-hour
cycle). Only checks developers tagged source='own_account'.

Sends alerts to BOTH Discord and Slack, with special emphasis on removals
since that's the most time-sensitive event for your own accounts.
"""

import os
import re
import io
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
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

FALLBACK_COUNTRIES = ["us", "pk", "gb", "in", "ca"]
REMOVED_RECHECK_HOURS = 6  # shorter than the main monitor's 24h, since these are your own accounts


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
    for a in soup.find_all("a", href=True):
        if "/store/apps/developer?id=" in a["href"] or "/store/apps/dev?id=" in a["href"]:
            dev_link = "https://play.google.com" + a["href"] if a["href"].startswith("/") else a["href"]
            dev_name = a.get_text(strip=True)
            return dev_name, dev_link
    return None, None


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


def extract_canonical_title(soup):
    if soup and soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.sub(r'\s*-\s*Apps on Google Play\s*$', '', t, flags=re.IGNORECASE)
        return t.strip()
    return None


def ensure_gl_us(url):
    """Makes sure a Play Store URL always includes gl=us, appending it if missing."""
    if "gl=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}gl=us"


def fetch_developer_catalog(dev_link, developer_name=None):
    dev_link = ensure_gl_us(dev_link)
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
    if not old_hash_str or not new_hash_str:
        return False
    try:
        old_h = imagehash.hex_to_hash(old_hash_str)
        new_h = imagehash.hex_to_hash(new_hash_str)
        return (old_h - new_h) > threshold
    except Exception:
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


def send_discord(embeds):
    """Returns True if all sends succeeded (or no webhook configured)."""
    if not DISCORD_WEBHOOK_URL:
        return True
    ok = True
    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": batch}, timeout=15)
            if resp.status_code >= 300:
                print(f"[warn] Discord returned status {resp.status_code}: {resp.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[warn] Discord send failed: {e}")
            ok = False
    return ok


def send_slack(text_blocks):
    """Returns True if all sends succeeded (or no webhook configured)."""
    if not SLACK_WEBHOOK_URL:
        return True
    ok = True
    for text in text_blocks:
        try:
            resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
            if resp.status_code >= 300:
                print(f"[warn] Slack returned status {resp.status_code}: {resp.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[warn] Slack send failed: {e}")
            ok = False
    return ok


def play_link(package_name):
    return f"https://play.google.com/store/apps/details?id={package_name}&gl=us"


def send_digest(events):
    total = sum(len(v) for v in events.values())
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if total == 0:
        send_discord([{
            "title": "My Accounts check complete",
            "description": "Nothing new this cycle (30-min check).",
            "color": 0x95a5a6,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }])
        send_slack([f":white_check_mark: *My Accounts check complete* — nothing new this cycle ({now_str})"])
        return True

    embeds = []
    slack_texts = []

    for ev in events.get("removed", []):
        link = play_link(ev["package_name"])
        embeds.append({
            "title": f"🚨 REMOVED: {ev['title']}",
            "description": f"Account: **{ev['developer_name']}**\nPackage: `{ev['package_name']}`\n[Last known link]({link})",
            "color": 0xe74c3c,
        })
        slack_texts.append(
            f":rotating_light: *GAME REMOVED* :rotating_light:\n"
            f"*{ev['title']}* under account *{ev['developer_name']}*\n"
            f"Package: `{ev['package_name']}`\n<{link}|Last known link>"
        )

    for ev in events.get("new_upload", []):
        link = play_link(ev["package_name"])
        embeds.append({
            "title": f"🆕 New upload: {ev['title']}",
            "description": f"Account: **{ev['developer_name']}**\nPackage: `{ev['package_name']}`\n[Open]({link})",
            "color": 0x2ecc71,
        })
        slack_texts.append(f":new: New upload: *{ev['title']}* ({ev['developer_name']}) — <{link}|Open>")

    for ev in events.get("listing_changed", []):
        link = play_link(ev["package_name"])
        lines = []
        if ev.get("title_changed"):
            lines.append(f"Title: {ev['old_title']} -> {ev['new_title']}")
        if ev.get("icon_changed"):
            lines.append("Icon changed")
        embeds.append({
            "title": f"🎨 Listing changed: {ev['new_title']}",
            "description": f"Account: **{ev['developer_name']}**\n" + "\n".join(lines) + f"\n[Open]({link})",
            "color": 0x3498db,
        })
        slack_texts.append(f":art: Listing changed: *{ev['new_title']}* ({ev['developer_name']}) — " + "; ".join(lines))

    discord_ok = send_discord(embeds)
    slack_ok = send_slack(slack_texts)
    return discord_ok and slack_ok


def main():
    events = {"new_upload": [], "removed": [], "listing_changed": []}
    pending_removals = []

    developers_resp = supabase.table("developers").select("*").eq("source", "own_account").execute()
    developers = {d["id"]: d for d in developers_resp.data}

    if not developers:
        print("[info] No own accounts registered. Nothing to check.")
        return

    apps_resp = supabase.table("apps").select("*").execute()
    db_apps = {a["package_name"]: a for a in apps_resp.data if a["developer_id"] in developers}

    fresh_map = {}

    for dev_id, dev in developers.items():
        catalog = fetch_developer_catalog(dev["developer_url"], developer_name=dev["name"])
        supabase.table("developers").update({
            "last_checked": datetime.now(timezone.utc).isoformat()
        }).eq("id", dev_id).execute()

        for app in catalog:
            fresh_map[app["package_name"]] = {
                "developer_id": dev_id, "developer_name": dev["name"],
                "title": app["title"], "icon_url": app["icon_url"],
            }

    # New / existing apps found in catalogs
    for package_name, fresh in fresh_map.items():
        stored = db_apps.get(package_name)

        if stored is None:
            soup = fetch_app_page(package_name)
            canonical_title = extract_canonical_title(soup) if soup else None
            final_title = canonical_title or fresh["title"]
            icon_hash = get_icon_hash(fresh["icon_url"])

            insert_res = supabase.table("apps").insert({
                "package_name": package_name, "developer_id": fresh["developer_id"],
                "title": final_title, "icon_url": fresh["icon_url"],
                "icon_hash": icon_hash, "status": "active",
            }).execute()
            new_app_id = insert_res.data[0]["id"]

            events["new_upload"].append({
                "package_name": package_name, "title": final_title,
                "developer_name": fresh["developer_name"],
            })
            supabase.table("change_log").insert({
                "app_id": new_app_id, "developer_id": fresh["developer_id"], "event_type": "new_upload",
                "old_value": None, "new_value": {"title": final_title},
                "developer_name": fresh["developer_name"], "app_title": final_title, "package_name": package_name,
            }).execute()
            continue

        new_icon_hash = get_icon_hash(fresh["icon_url"])
        candidate_title_changed = stored["title"] != fresh["title"]
        old_icon_hash = stored.get("icon_hash")
        needs_rebaseline = bool(old_icon_hash) and bool(new_icon_hash) and len(old_icon_hash) != len(new_icon_hash)
        icon_changed = False if needs_rebaseline else icons_differ(old_icon_hash, new_icon_hash)

        title_changed = False
        confirmed_new_title = fresh["title"]
        if candidate_title_changed:
            detail_soup = fetch_app_page(package_name)
            canonical_title = extract_canonical_title(detail_soup) if detail_soup else None
            if canonical_title:
                confirmed_new_title = canonical_title
                title_changed = stored["title"] != canonical_title
            else:
                title_changed = candidate_title_changed

        update_fields = {"last_seen": datetime.now(timezone.utc).isoformat(), "status": "active"}

        if title_changed or icon_changed:
            events["listing_changed"].append({
                "package_name": package_name, "developer_name": fresh["developer_name"],
                "title_changed": title_changed, "icon_changed": icon_changed,
                "old_title": stored["title"], "new_title": confirmed_new_title,
            })
            old_value, new_value = {}, {}
            if title_changed:
                old_value["title"] = stored["title"]
                new_value["title"] = confirmed_new_title
                update_fields["title"] = confirmed_new_title
            if icon_changed:
                old_value["icon_url"] = stored.get("icon_url")
                new_value["icon_url"] = fresh["icon_url"]
                update_fields["icon_url"] = fresh["icon_url"]
                update_fields["icon_hash"] = new_icon_hash

            supabase.table("change_log").insert({
                "app_id": stored["id"], "developer_id": fresh["developer_id"], "event_type": "listing_changed",
                "old_value": old_value, "new_value": new_value,
                "developer_name": fresh["developer_name"], "app_title": confirmed_new_title, "package_name": package_name,
            }).execute()
        elif new_icon_hash and (not old_icon_hash or needs_rebaseline):
            update_fields["icon_hash"] = new_icon_hash

        supabase.table("apps").update(update_fields).eq("id", stored["id"]).execute()

    # Apps that vanished from their account's catalog -> check for removal
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
                # Defer the DB status change until AFTER notifications are sent.
                # Otherwise a failed/interrupted notification would leave the app
                # marked 'removed' in the DB, and the next run would see no change
                # and never alert — silently losing the notification forever.
                pending_removals.append({
                    "app_id": stored["id"], "developer_id": stored["developer_id"],
                    "title": stored["title"], "package_name": package_name, "dev_name": dev_name,
                })
            else:
                # Already known-removed: just refresh the timestamp
                supabase.table("apps").update({
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                }).eq("id", stored["id"]).execute()
            continue

        if result == "found":
            # Page still loads somewhere (maybe just failed a check transiently) — reactivate, no removal alert
            supabase.table("apps").update({
                "status": "active", "last_seen": datetime.now(timezone.utc).isoformat(),
            }).eq("id", stored["id"]).execute()

    # Send notifications FIRST — only mark removals in the DB once alerts
    # have actually gone out, so a failed send doesn't silently swallow them.
    notified_ok = send_digest(events)

    if pending_removals and not notified_ok:
        print("[warn] Notification delivery failed — NOT marking removals in DB. "
              "They will be re-detected and re-alerted on the next run.")
        print("[info] Own accounts check complete (with notification errors).")
        return

    for pr in pending_removals:
        supabase.table("change_log").insert({
            "app_id": pr["app_id"], "developer_id": pr["developer_id"], "event_type": "removed",
            "old_value": {"title": pr["title"]}, "new_value": None,
            "developer_name": pr["dev_name"], "app_title": pr["title"], "package_name": pr["package_name"],
        }).execute()
        supabase.table("apps").update({
            "status": "removed", "last_seen": datetime.now(timezone.utc).isoformat(),
        }).eq("id", pr["app_id"]).execute()

    if pending_removals:
        print(f"[info] Committed {len(pending_removals)} removal(s) after notification.")

    print("[info] Own accounts check complete.")


if __name__ == "__main__":
    main()