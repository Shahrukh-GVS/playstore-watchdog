"""Google Analytics 4 (Firebase Analytics) reader for the Firebase tab.

Everything goes through ONE Google Cloud service account whose JSON key lives in
Streamlit secrets under [gcp_service_account]. The service account only needs
"Viewer" access in Google Analytics — no Firebase project role is required,
because Firebase Analytics data is stored in the linked GA4 property.

Flow:
  package name --(Analytics Admin API)--> GA4 property + Android data stream
  property + stream --(Analytics Data API)--> daily users / sessions / sources
"""

from datetime import date, timedelta

import streamlit as st

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
DATA = "https://analyticsdata.googleapis.com/v1beta"

# GA4 default channel groups that mean "someone paid for this user".
PAID_CHANNELS = {
    "Paid Search", "Paid Social", "Paid Video", "Paid Shopping", "Paid Other",
    "Display", "Cross-network", "Audio", "Affiliates",
}


class GAError(Exception):
    pass


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def is_configured():
    try:
        return "gcp_service_account" in st.secrets
    except Exception:
        return False


def service_account_email():
    try:
        return st.secrets["gcp_service_account"].get("client_email")
    except Exception:
        return None


@st.cache_resource(show_spinner=False)
def _session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    if not is_configured():
        raise GAError(
            "Missing **[gcp_service_account]** in Streamlit secrets. "
            "Paste the service account JSON key there (see setup guide)."
        )
    info = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def _call(method, url, **kwargs):
    resp = _session().request(method, url, timeout=30, **kwargs)
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        if resp.status_code == 403 and "has not been used" in msg:
            msg += " → Enable this API in Google Cloud Console, wait 2 minutes, retry."
        raise GAError(f"Google API {resp.status_code}: {msg}")
    return resp.json()


# ---------------------------------------------------------------------------
# Package name -> GA4 property + stream
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def android_stream_index():
    """{package_name: {property_id, property_name, stream_id, app_id}} for every
    Android stream the service account can see. Cached for an hour."""
    index = {}
    page_token = None
    properties = []
    while True:
        params = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        data = _call("GET", f"{ADMIN}/accountSummaries", params=params)
        for acc in data.get("accountSummaries", []):
            for prop in acc.get("propertySummaries", []):
                properties.append((prop["property"], prop.get("displayName", "")))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    for prop_path, prop_name in properties:
        token = None
        while True:
            params = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            try:
                data = _call("GET", f"{ADMIN}/{prop_path}/dataStreams", params=params)
            except GAError:
                break  # one inaccessible property shouldn't break the rest
            for s in data.get("dataStreams", []):
                android = s.get("androidAppStreamData")
                if not android or not android.get("packageName"):
                    continue
                index[android["packageName"].lower()] = {
                    "property_id": prop_path.split("/")[-1],
                    "property_name": prop_name,
                    "stream_id": s["name"].split("/")[-1],
                    "app_id": android.get("firebaseAppId"),
                }
            token = data.get("nextPageToken")
            if not token:
                break
    return index


def find_stream(package_name, refresh=False):
    """-> (link_dict or None, error_message or None)"""
    try:
        if refresh:
            android_stream_index.clear()
        link = android_stream_index().get(package_name.lower())
    except GAError as e:
        return None, str(e)
    except Exception as e:
        return None, f"Could not reach Google Analytics: {e}"
    if not link:
        who = service_account_email() or "the service account"
        return None, (f"No GA4 Android stream for `{package_name}` is visible to {who}. "
                      "Add that email as Viewer on the game's GA4 property (or account).")
    return link, None


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _run_report(property_id, stream_id, start, end, dimensions, metrics, limit=10000):
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": limit,
        "keepEmptyRows": False,
    }
    if stream_id:
        body["dimensionFilter"] = {"filter": {
            "fieldName": "streamId", "stringFilter": {"value": str(stream_id)}}}
    data = _call("POST", f"{DATA}/properties/{property_id}:runReport", json=body)

    rows = []
    for r in data.get("rows", []):
        row = {}
        for d, v in zip(dimensions, r.get("dimensionValues", [])):
            val = v.get("value")
            if d == "date" and val and len(val) == 8:
                val = f"{val[:4]}-{val[4:6]}-{val[6:]}"
            row[d] = val
        for m, v in zip(metrics, r.get("metricValues", [])):
            try:
                row[m] = float(v.get("value") or 0)
            except ValueError:
                row[m] = 0.0
        rows.append(row)
    return rows


DAILY_METRICS = [
    "activeUsers", "newUsers", "totalUsers", "sessions", "engagedSessions",
    "averageSessionDuration", "userEngagementDuration", "engagementRate",
    "screenPageViews", "eventCount",
]


@st.cache_data(ttl=600, show_spinner=False)
def game_report(property_id, stream_id, end_iso, days):
    """Everything the detail view needs, for `days` days ending on end_iso.
    Cached 10 minutes per game/date so clicking around is cheap."""
    end = date.fromisoformat(end_iso)
    start = end - timedelta(days=max(days, 2) - 1)

    daily = _run_report(property_id, stream_id, start, end, ["date"], DAILY_METRICS)
    channels = _run_report(property_id, stream_id, start, end,
                           ["date", "firstUserDefaultChannelGroup"],
                           ["newUsers", "activeUsers"])
    campaigns = _run_report(property_id, stream_id, start, end,
                            ["date", "firstUserCampaignName", "firstUserSource"],
                            ["newUsers", "activeUsers"])
    countries = _run_report(property_id, stream_id, start, end,
                            ["date", "country"], ["activeUsers", "newUsers"])
    return {"daily": daily, "channels": channels,
            "campaigns": campaigns, "countries": countries}


def is_paid(channel):
    return (channel or "") in PAID_CHANNELS