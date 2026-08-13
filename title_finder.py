"""
title_finder.py — Play Store Watchdog: Title Finder

Generates candidate Google Play game titles by shuffling keywords from proven
competitor listings, then validates each candidate against a live Play Store
search test.

THE VALIDATION RULE
A candidate passes if, when searched on Play, at least N of the top M results
are games that were released within the last X days AND are doing at least Y
daily installs. That combination means Play is actively ranking new entrants
for that keyword cluster, so a new app can break in.

WHY BIGRAMS MATTER (do not simplify this)
Naive keyword shuffling produces titles that fail. A candidate only ranks when
it preserves an intact adjacent word-pair from a title that already has high
downloads — Play needs an existing cluster to slot it into. "Arrow Puzzle Maze
Rush" works because "Maze Rush" survives whole from a proven listing. "Arrow
Sort: Maze Escape Jam" fails because no pair in it is adjacent in any proven
title.

DATA SOURCE
AppstoreSpy. Two things worth knowing about their API:
  - Real Play SERP comes from the async /jobs/search endpoint (create, then
    poll). The synchronous /play/apps?q= endpoint is AppstoreSpy's own index
    search sorted by whatever you ask for — NOT Play's ranking — so using it
    would silently invalidate the whole test. We use /jobs/search.
  - Daily installs are available directly as `ipd`. No need to derive from a
    30-day delta, and no lifetime/age division (which understates new launches,
    exactly the apps this test looks for).
"""

import re
import time
import json
import itertools
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st

API_BASE = "https://api.appstorespy.com/v1"

# "cash" and "reward" force Play's 18+ Cash Rewards content rating, which badly
# cuts reach. Keep this list here, in one place, and edit it here only.
BLOCKED_TOKENS = {"cash", "reward", "rewards", "money", "win", "earn", "free"}

PLAY_TITLE_CHAR_LIMIT = 30

APP_META_TTL_DAYS = 7
SERP_TTL_DAYS = 1

# AppstoreSpy exposes closed enums for these — free-text values are rejected.
SUPPORTED_COUNTRIES = [
    "US", "CA", "AT", "BE", "CY", "CZ", "DK", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "NL", "NO", "PL", "PT", "RU", "ES", "SE", "CH", "TR", "UA",
    "GB", "EG", "IN", "IL", "MA", "NG", "SA", "ZA", "AE", "AU", "HK", "ID",
    "JP", "KZ", "KR", "MY", "NZ", "PK", "PH", "SG", "TW", "TH", "VN", "AR",
    "BR", "MX",
]
SUPPORTED_LANGUAGES = [
    "en_US", "ar", "en_GB", "fr_FR", "es_419", "de_DE", "pt_BR", "it_IT",
    "ja_JP", "ko_KR", "tr_TR", "ru_RU", "vi",
]


class TitleFinderError(Exception):
    """Surfaced to the UI with its message intact — never swallowed."""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def tokenize(title):
    """Lowercase words, punctuation stripped."""
    return [t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if t]


def build_proven_bigrams(titles):
    """Every adjacent word-pair across all supplied competitor titles."""
    bigrams = set()
    for title in titles:
        tokens = tokenize(title)
        for a, b in zip(tokens, tokens[1:]):
            bigrams.add((a, b))
    return bigrams


def count_proven_bigrams(candidate_tokens, proven_bigrams):
    return sum(
        1 for a, b in zip(candidate_tokens, candidate_tokens[1:])
        if (a, b) in proven_bigrams
    )


def generate_candidates(source_titles, head_word, max_candidates=4000):
    """
    Returns [{"title", "length", "bigram_score"}], ranked by bigram score
    (desc) then length (asc). Candidates with zero proven bigrams are
    discarded — they have no cluster for Play to slot them into.
    """
    source_titles = [t.strip() for t in source_titles if t.strip()]
    if not source_titles:
        return [], "No competitor titles supplied."

    head = (head_word or "").strip().lower()
    if not head:
        return [], "A head word is required — it's the niche's head search term."

    proven_bigrams = build_proven_bigrams(source_titles)

    pool = []
    seen_pool = set()
    for title in source_titles:
        for tok in tokenize(title):
            if tok == head or tok in BLOCKED_TOKENS or tok in seen_pool:
                continue
            seen_pool.add(tok)
            pool.append(tok)

    if len(pool) < 2:
        return [], "Token pool is too small — add more competitor titles."

    source_norm = {" ".join(tokenize(t)) for t in source_titles}

    results = {}
    truncated = False

    for k in (2, 3):
        for combo in itertools.permutations(pool, k):
            if len(results) >= max_candidates:
                truncated = True
                break

            tokens = [head] + list(combo)
            score = count_proven_bigrams(tokens, proven_bigrams)
            if score == 0:
                continue  # no proven cluster -> would fail the test

            if " ".join(tokens) in source_norm:
                continue  # exact match to a source title

            plain = " ".join(w.capitalize() for w in tokens)
            colon = f"{tokens[0].capitalize()} {tokens[1].capitalize()}: " + \
                    " ".join(w.capitalize() for w in tokens[2:])

            for form in (plain, colon.strip().rstrip(":")):
                if len(form) > PLAY_TITLE_CHAR_LIMIT:
                    continue
                if form in results:
                    continue
                results[form] = {
                    "title": form,
                    "length": len(form),
                    "bigram_score": score,
                }
        if truncated:
            break

    ranked = sorted(
        results.values(),
        key=lambda r: (-r["bigram_score"], r["length"]),
    )
    note = f"Capped at {max_candidates} candidates." if truncated else None
    return ranked, note


# ---------------------------------------------------------------------------
# AppstoreSpy adapter
# ---------------------------------------------------------------------------

def _headers(api_key):
    return {
        "accept": "application/json",
        "API-KEY": api_key,
        "Content-Type": "application/json",
    }


class SerpNotReady(TitleFinderError):
    """The crawl job exists but AppstoreSpy hasn't crawled it yet."""


def submit_search(api_key, query, country="US", lang="en_US", limit=10):
    """
    Phase 1: queue a Play SERP crawl for `query`. Returns immediately.
    -> {"query", "job_id", "ready", "packages"}

    If AppstoreSpy already has a recent crawl for this term, results come back
    on this very call and `ready` is True.
    """
    if not api_key:
        raise TitleFinderError(
            "Missing AppstoreSpy API key. Add APPSTORESPY_API_KEY to "
            ".streamlit/secrets.toml (and to your .env for local runs)."
        )

    payload = {
        "store": "play", "term": query, "country": country,
        "lang": lang, "limit": max(limit, 10),
    }

    try:
        resp = requests.post(
            f"{API_BASE}/jobs/search", json=payload,
            headers=_headers(api_key), timeout=20,
        )
    except Exception as e:
        raise TitleFinderError(f"Submit failed for '{query}': {e}")

    if resp.status_code == 403:
        raise TitleFinderError("AppstoreSpy rejected the API key (403 Forbidden).")
    if resp.status_code == 429:
        raise TitleFinderError("AppstoreSpy rate limit hit (429). Slow down or retry later.")
    if resp.status_code >= 400:
        raise TitleFinderError(
            f"Submit failed for '{query}' — HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        body = resp.json() if resp.content else {}
    except Exception:
        raise TitleFinderError(f"Submit for '{query}' returned non-JSON: {resp.text[:300]}")

    packages = _packages_from_search_result(body)
    return {
        "query": query,
        "job_id": body.get("id") if isinstance(body, dict) else None,
        "ready": bool(packages),
        "packages": packages,
    }


def get_search(api_key, query, country="US", limit=9, debug=None):
    """
    Phase 2: retrieve a previously-submitted crawl, by term.
    -> list of package ids in rank order, or None if not crawled yet.

    Looking up by term means job ids don't need persisting — a page reload
    can't lose submitted work.
    """
    if not api_key:
        raise TitleFinderError("Missing AppstoreSpy API key (APPSTORESPY_API_KEY).")

    try:
        resp = requests.get(
            f"{API_BASE}/jobs/search",
            params={"store": "play", "term": query, "country": country},
            headers=_headers(api_key), timeout=20,
        )
    except Exception as e:
        raise TitleFinderError(f"Collect failed for '{query}': {e}")

    if debug is not None:
        debug.append({
            "step": f"GET /jobs/search — {query}",
            "status": resp.status_code, "body": resp.text[:600],
        })

    if resp.status_code == 403:
        raise TitleFinderError("AppstoreSpy rejected the API key (403 Forbidden).")
    if resp.status_code == 429:
        raise TitleFinderError("AppstoreSpy rate limit hit (429).")
    if resp.status_code >= 400:
        raise TitleFinderError(
            f"Collect failed for '{query}' — HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        body = resp.json() if resp.content else []
    except Exception:
        return None

    items = body if isinstance(body, list) else [body]
    for item in items:
        packages = _packages_from_search_result(item)
        if packages:
            return packages[:limit]

    return None  # crawled is still null -> job queued but not run


def _packages_from_search_result(body):
    """Flatten SERP clusters into a single ranked, deduped package list."""
    if not isinstance(body, dict):
        return []
    out, seen = [], set()
    for cluster in body.get("results") or []:
        if not isinstance(cluster, dict):
            continue
        for pkg in cluster.get("items") or []:
            if isinstance(pkg, str) and pkg not in seen:
                seen.add(pkg)
                out.append(pkg)
    return out


def get_app_meta(api_key, package_id, country="US"):
    """
    -> {"package_id", "title", "released_at", "daily_installs", "is_game"}

    daily_installs comes from AppstoreSpy's `ipd` field directly. We do NOT
    divide lifetime installs by app age — that badly understates recent
    launches, which are exactly what this test is looking for.
    """
    if not api_key:
        raise TitleFinderError(
            "Missing AppstoreSpy API key. Add APPSTORESPY_API_KEY to "
            ".streamlit/secrets.toml (and to your .env for local runs)."
        )

    try:
        resp = requests.get(
            f"{API_BASE}/play/apps/{package_id}",
            params={"country": country,
                    "fields": "id,name,released,ipd,type,installs_exact"},
            headers=_headers(api_key), timeout=25,
        )
    except Exception as e:
        raise TitleFinderError(f"Detail request failed for {package_id}: {e}")

    if resp.status_code == 403:
        raise TitleFinderError("AppstoreSpy rejected the API key (403 Forbidden).")
    if resp.status_code == 429:
        raise TitleFinderError("AppstoreSpy rate limit hit (429). Slow down or retry later.")
    if resp.status_code in (204, 404):
        return {"package_id": package_id, "title": None, "released_at": None,
                "daily_installs": None, "is_game": None}
    if resp.status_code >= 400:
        raise TitleFinderError(
            f"Detail failed for {package_id} — HTTP {resp.status_code}: {resp.text[:300]}"
        )

    d = resp.json() if resp.content else {}
    released = (d.get("released") or "")[:10] or None
    app_type = (d.get("type") or "").upper()

    return {
        "package_id": package_id,
        "title": d.get("name"),
        "released_at": released,
        "daily_installs": d.get("ipd"),
        "is_game": ("GAME" in app_type) if app_type else None,
    }


# ---------------------------------------------------------------------------
# Caching (Supabase)
# ---------------------------------------------------------------------------

def _fresh(fetched_at_iso, ttl_days):
    if not fetched_at_iso:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts < timedelta(days=ttl_days)
    except Exception:
        return False


def cached_serp(supabase, api_key, query, country, lang, limit, stats, debug=None):
    """Cache first; otherwise collect an already-submitted crawl.
    Raises SerpNotReady if the crawl hasn't landed yet."""
    try:
        row = supabase.table("serp_cache").select("*") \
            .eq("query", query).eq("country", country).execute().data
        if row and _fresh(row[0].get("fetched_at"), SERP_TTL_DAYS):
            stats["serp_hits"] += 1
            return row[0]["results"][:limit]
    except Exception:
        pass  # cache miss on error — fall through to a live call

    packages = get_search(api_key, query, country, limit, debug=debug)
    if packages is None:
        raise SerpNotReady(
            f"'{query}' is still being crawled by AppstoreSpy. "
            "Wait a couple of minutes and press Collect again."
        )

    results = [
        {"package_id": pkg, "title": None, "position": i + 1}
        for i, pkg in enumerate(packages)
    ]
    stats["serp_calls"] += 1
    try:
        supabase.table("serp_cache").upsert({
            "query": query, "country": country, "results": results,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="query,country").execute()
    except Exception:
        pass
    return results


def cached_app_meta(supabase, api_key, package_id, country, stats):
    try:
        row = supabase.table("app_meta").select("*") \
            .eq("package_id", package_id).execute().data
        if row and _fresh(row[0].get("fetched_at"), APP_META_TTL_DAYS):
            stats["meta_hits"] += 1
            return {
                "package_id": package_id,
                "title": row[0].get("title"),
                "released_at": row[0].get("released_at"),
                "daily_installs": row[0].get("daily_installs"),
                "is_game": row[0].get("is_game"),
            }
    except Exception:
        pass

    meta = get_app_meta(api_key, package_id, country)
    stats["meta_calls"] += 1
    try:
        supabase.table("app_meta").upsert({
            "package_id": package_id,
            "title": meta.get("title"),
            "released_at": meta.get("released_at"),
            "daily_installs": meta.get("daily_installs"),
            "is_game": meta.get("is_game"),
            "raw": meta,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="package_id").execute()
    except Exception:
        pass
    return meta


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_title(supabase, api_key, title, country, lang, top_n, threshold,
               max_age_days, min_daily_installs, stats, debug=None):
    serp = cached_serp(supabase, api_key, title, country, lang, top_n, stats, debug=debug)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=max_age_days))

    rows, hits = [], 0
    for entry in serp[:top_n]:
        meta = cached_app_meta(supabase, api_key, entry["package_id"], country, stats)

        recent = False
        if meta.get("released_at"):
            try:
                recent = datetime.strptime(meta["released_at"], "%Y-%m-%d").date() >= cutoff
            except Exception:
                recent = False

        installs = meta.get("daily_installs")
        big_enough = bool(installs) and installs >= min_daily_installs
        is_game = meta.get("is_game") is not False  # unknown counts as possible

        counted = recent and big_enough and is_game
        if counted:
            hits += 1

        rows.append({
            "#": entry["position"],
            "Package": entry["package_id"],
            "Title": meta.get("title") or "-",
            "Released": meta.get("released_at") or "-",
            "Daily installs": installs if installs is not None else "-",
            "Game": {True: "Yes", False: "No", None: "?"}[meta.get("is_game")],
            "Counted": "✅" if counted else "—",
        })

    return {"hits": hits, "passed": hits >= threshold, "rows": rows}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def render(supabase, api_key):
    st.subheader("Title Finder")
    st.caption(
        "Shuffles keywords from proven competitor titles, keeps only candidates "
        "that preserve an intact word-pair from a high-download listing, then "
        "tests each against a live Play search."
    )

    if not api_key:
        st.error(
            "Missing AppstoreSpy API key. Add **APPSTORESPY_API_KEY** to "
            "`.streamlit/secrets.toml` (and to `.env` for local runs), then reload."
        )
        return

    with st.sidebar:
        st.markdown("### Title Finder settings")
        threshold = st.number_input("Hits required", 1, 20, 6, key="tf_threshold")
        top_n = st.number_input("Top results to check", 1, 20, 9, key="tf_topn")
        max_age_days = st.number_input("Max release age (days)", 1, 1095, 180, key="tf_age")
        min_installs = st.number_input("Min daily installs", 0, 1_000_000, 1000, step=100, key="tf_installs")
        country = st.selectbox("Country", SUPPORTED_COUNTRIES,
                               index=SUPPORTED_COUNTRIES.index("US"), key="tf_country")
        lang = st.selectbox("Language", SUPPORTED_LANGUAGES,
                            index=SUPPORTED_LANGUAGES.index("en_US"), key="tf_lang")
        head_word = st.text_input("Head word (pinned first)", "arrow", key="tf_head")
        st.caption(f"Excluded tokens: {', '.join(sorted(BLOCKED_TOKENS))}")

    source_text = st.text_area(
        "Competitor titles — one per line", height=180, key="tf_sources",
        placeholder="Arrow Maze Rush\nPuzzle Maze Escape\nArrow Sort Jam",
    )

    if st.button("Generate candidates", type="primary", key="tf_generate"):
        candidates, note = generate_candidates(
            source_text.splitlines(), head_word
        )
        if not candidates:
            st.warning(note or "No candidates survived the bigram filter.")
            st.session_state.tf_candidates = None
        else:
            if note:
                st.info(note)
            st.session_state.tf_candidates = candidates
            st.session_state.tf_results = None

    candidates = st.session_state.get("tf_candidates")
    if not candidates:
        return

    st.markdown(f"### {len(candidates)} candidates")
    st.caption("Higher bigram scores pass the test more often — they preserve more proven word-pairs.")
    st.dataframe(
        [{"Title": c["title"], "Chars": c["length"], "Bigram score": c["bigram_score"]}
         for c in candidates],
        use_container_width=True, hide_index=True,
    )

    titles = [c["title"] for c in candidates]
    chosen = st.multiselect(
        "Candidates to test", titles, default=titles[:12], key="tf_chosen",
    )

    st.markdown("---")
    st.markdown("### Testing is two steps")
    st.caption(
        "AppstoreSpy crawls Play's search results in the background — a term it "
        "hasn't seen before takes a couple of minutes to come back. So you submit "
        "the searches, wait, then collect. Nothing hangs and nothing is lost if you "
        "reload the page."
    )

    scol1, scol2 = st.columns(2)

    # ---- Phase 1: submit ----
    if scol1.button("1️⃣ Submit searches", type="primary",
                    use_container_width=True, key="tf_submit"):
        submitted, ready_now, errs = [], [], []
        progress = st.progress(0)
        status = st.empty()

        for i, title in enumerate(chosen):
            status.write(f"Queueing **{title}** ({i + 1}/{len(chosen)})")
            try:
                res = submit_search(api_key, title, country, lang, int(top_n))
                (ready_now if res["ready"] else submitted).append(title)
            except TitleFinderError as e:
                errs.append(f"**{title}** — {e}")
            except Exception as e:
                errs.append(f"**{title}** — unexpected error: {type(e).__name__}: {e}")
            progress.progress((i + 1) / len(chosen))

        status.empty()
        st.session_state.tf_submitted = chosen

        for e in errs:
            st.error(e)
        if ready_now:
            st.success(f"{len(ready_now)} already crawled and ready to collect now.")
        if submitted:
            st.info(
                f"{len(submitted)} queued for crawling. Give it ~2-3 minutes, "
                "then press **Collect results**. Anything not ready yet will just "
                "be reported as still crawling — press Collect again later."
            )

    # ---- Phase 2: collect ----
    if scol2.button("2️⃣ Collect results & test", use_container_width=True,
                    key="tf_collect"):
        targets = st.session_state.get("tf_submitted") or chosen
        stats = {"serp_calls": 0, "serp_hits": 0, "meta_calls": 0, "meta_hits": 0}
        results, errors, pending = [], [], []
        debug = []
        progress = st.progress(0)
        status = st.empty()

        for i, title in enumerate(targets):
            status.write(f"Collecting **{title}** ({i + 1}/{len(targets)})")
            try:
                outcome = test_title(
                    supabase, api_key, title, country, lang,
                    int(top_n), int(threshold), int(max_age_days),
                    int(min_installs), stats,
                    debug=debug if i == 0 else None,
                )
                score = next((c["bigram_score"] for c in candidates
                              if c["title"] == title), None)
                results.append({"title": title, "score": score, **outcome})

                try:
                    supabase.table("title_tests").insert({
                        "title": title, "country": country, "hits": outcome["hits"],
                        "top_n": int(top_n), "threshold": int(threshold),
                        "passed": outcome["passed"], "bigram_score": score,
                        "detail": outcome["rows"],
                    }).execute()
                except Exception:
                    pass

            except SerpNotReady:
                pending.append(title)
            except TitleFinderError as e:
                errors.append(f"**{title}** — {e}")
            except Exception as e:
                errors.append(f"**{title}** — unexpected error: {type(e).__name__}: {e}")

            progress.progress((i + 1) / len(targets))

        status.empty()
        st.session_state.tf_results = results
        st.session_state.tf_errors = errors
        st.session_state.tf_pending = pending
        st.session_state.tf_stats = stats
        st.session_state.tf_debug = debug

    # ---- Output ----
    pending = st.session_state.get("tf_pending") or []
    if pending:
        st.warning(
            f"Still crawling ({len(pending)}): {', '.join(pending)}\n\n"
            "Press **Collect results & test** again in a minute or two."
        )

    for err in st.session_state.get("tf_errors") or []:
        st.error(err)

    debug = st.session_state.get("tf_debug")
    if debug and (st.session_state.get("tf_errors") or pending):
        with st.expander("🐛 Debug — raw API response"):
            for d in debug:
                st.write(f"**{d['step']}** · HTTP {d['status']}")
                st.code(d["body"] or "(empty body)")

    results = st.session_state.get("tf_results")
    if not results:
        return

    stats = st.session_state.get("tf_stats") or {}
    if stats:
        st.caption(
            f"Live calls: {stats.get('serp_calls', 0)} searches, "
            f"{stats.get('meta_calls', 0)} details · "
            f"cache hits: {stats.get('serp_hits', 0)} / {stats.get('meta_hits', 0)}"
        )

    passed = [r for r in results if r["passed"]]

    if not passed:
        st.warning(
            f"Nothing cleared {threshold}/{top_n}. The keyword cluster may be locked up by "
            f"incumbents right now. Try lowering **Min daily installs** (currently "
            f"{min_installs:,}) or widening **Max release age** (currently {max_age_days} days) "
            "in the sidebar — that loosens what counts as an active new entrant. "
            "Adding more competitor titles also widens the proven-bigram set."
        )
    else:
        st.success(f"✅ {len(passed)} of {len(results)} passed.")

    st.markdown("### Results")
    st.dataframe(
        [{"Title": r["title"], "Chars": len(r["title"]),
          "Bigram score": r["score"],
          f"Hits (of {top_n})": r["hits"],
          "Passed": "✅" if r["passed"] else "—"} for r in results],
        use_container_width=True, hide_index=True,
    )

    for r in results:
        label = f"{'✅' if r['passed'] else '—'} {r['title']} — {r['hits']}/{top_n} hits"
        with st.expander(label):
            st.dataframe(r["rows"], use_container_width=True, hide_index=True)