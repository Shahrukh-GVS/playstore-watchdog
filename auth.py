"""
auth.py — Play Store Watchdog: authentication and per-user access

Supabase Auth is email-based, so usernames are mapped to synthetic addresses:
    shahrukh.gvs  ->  shahrukh.gvs@watchdog.local
Users only ever see and type the username. No mail is ever sent — email
confirmation must be disabled in Supabase (Authentication -> Providers -> Email).

Isolation is enforced by Row Level Security in the database, not by this file.
Once a user is signed in, their client carries their JWT and Postgres refuses
to return anyone else's rows. `user_id` columns default to auth.uid(), so
inserts stamp themselves.
"""

import time
from datetime import datetime, timedelta

import streamlit as st
from supabase import create_client
import extra_streamlit_components as stx

USERNAME_DOMAIN = "watchdog.local"
SESSION_COOKIE = "psw_session"
COOKIE_DAYS = 30


def init_cookies():
    """Construct the CookieManager once per script run.

    CookieManager fills its internal dict at construction time. Caching the
    instance across reruns therefore freezes whatever it saw on the very first
    render — which, right after a page reload, is nothing at all. Rebuilding it
    each run is what makes the cookie readable. The stable `key` keeps
    Streamlit treating it as the same component.
    """
    st.session_state.psw_cm = stx.CookieManager(key="psw_cookie_manager")
    return st.session_state.psw_cm


def _cookies():
    """The manager built for this run by init_cookies()."""
    return st.session_state.get("psw_cm")


def _save_session_cookie(refresh_token):
    """Persist the refresh token so a page reload can restore the session.

    Note this token lives in a browser cookie, so anyone with access to the
    machine/browser can resume the session for up to COOKIE_DAYS. That is the
    normal trade-off for 'stay signed in'; Sign out clears it immediately.
    """
    if not refresh_token:
        return
    cm = _cookies()
    if cm is None:
        return
    try:
        cm.set(
            SESSION_COOKIE, refresh_token,
            expires_at=datetime.now() + timedelta(days=COOKIE_DAYS),
            key="psw_set_cookie",
        )
    except Exception:
        pass


def _clear_session_cookie():
    cm = _cookies()
    if cm is None:
        return
    try:
        cm.delete(SESSION_COOKIE, key="psw_del_cookie")
    except Exception:
        pass


def _restore_session(supabase_url, anon_key):
    """Rebuild a signed-in client from the cookie after a page reload.

    CookieManager talks to the browser asynchronously, so on the first script
    run after a reload it reports nothing at all. We therefore allow a couple
    of rerun cycles for it to report in before concluding there's no cookie —
    giving up on that first empty read is exactly what kept logging you out.
    """
    if st.session_state.get("psw_restore_done"):
        return False

    cm = _cookies()
    if cm is None:
        return False
    try:
        token = cm.get(SESSION_COOKIE)
    except Exception:
        token = None

    if not token:
        tries = st.session_state.get("psw_cookie_tries", 0)
        if tries < 3:
            st.session_state.psw_cookie_tries = tries + 1
            time.sleep(0.25)
            st.rerun()
        st.session_state.psw_restore_done = True
        return False

    st.session_state.psw_restore_done = True
    try:
        client = create_client(supabase_url, anon_key)
        result = client.auth.refresh_session(token)
        if not result or not result.user:
            _clear_session_cookie()
            return False

        profile = None
        try:
            rows = client.table("profiles").select("*") \
                .eq("user_id", result.user.id).execute().data
            profile = rows[0] if rows else None
        except Exception:
            pass

        st.session_state.sb_client = client
        st.session_state.sb_user = {"id": result.user.id, "email": result.user.email}
        st.session_state.sb_profile = profile or {
            "user_id": result.user.id,
            "username": email_to_username(result.user.email),
            "is_admin": False,
        }
        if result.session and result.session.refresh_token:
            _save_session_cookie(result.session.refresh_token)
        return True
    except Exception:
        _clear_session_cookie()
        return False


def username_to_email(username):
    username = (username or "").strip().lower()
    return f"{username}@{USERNAME_DOMAIN}" if username else ""


def email_to_username(email):
    return (email or "").split("@")[0]


def get_user_client(supabase_url, anon_key):
    """The signed-in user's client, or None. All app queries go through this."""
    return st.session_state.get("sb_client")


def get_admin_client(supabase_url, service_key):
    """service_role client — used ONLY for creating users (Supabase has no
    user-scoped way to do that). Never used for normal data access."""
    if not service_key:
        return None
    return create_client(supabase_url, service_key)


def current_profile():
    return st.session_state.get("sb_profile")


def is_admin():
    prof = current_profile()
    return bool(prof and prof.get("is_admin"))


def sign_in(supabase_url, anon_key, username, password):
    """-> (ok, error_message)"""
    email = username_to_email(username)
    if not email or not password:
        return False, "Username and password are required."

    try:
        client = create_client(supabase_url, anon_key)
        result = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        msg = str(e)
        if "Invalid login" in msg or "invalid_credentials" in msg:
            return False, "Incorrect username or password."
        if "Email not confirmed" in msg:
            return False, ("This account needs email confirmation disabled in Supabase "
                           "(Authentication → Providers → Email → turn off 'Confirm email').")
        return False, f"Sign-in failed: {msg}"

    if not result or not result.user:
        return False, "Incorrect username or password."

    # Load or lazily create the profile row
    profile = None
    try:
        rows = client.table("profiles").select("*").eq("user_id", result.user.id).execute().data
        if rows:
            profile = rows[0]
        else:
            client.table("profiles").insert({
                "user_id": result.user.id,
                "username": email_to_username(email),
            }).execute()
            profile = {"user_id": result.user.id,
                       "username": email_to_username(email), "is_admin": False}
    except Exception:
        profile = {"user_id": result.user.id,
                   "username": email_to_username(email), "is_admin": False}

    try:
        client.table("user_settings").upsert(
            {"user_id": result.user.id}, on_conflict="user_id"
        ).execute()
    except Exception:
        pass

    st.session_state.sb_client = client
    st.session_state.sb_user = {"id": result.user.id, "email": email}
    st.session_state.sb_profile = profile
    if result.session and result.session.refresh_token:
        _save_session_cookie(result.session.refresh_token)
    return True, None


def sign_out():
    client = st.session_state.get("sb_client")
    if client:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    for key in ("sb_client", "sb_user", "sb_profile",
                "psw_restore_done", "psw_cookie_tries"):
        st.session_state.pop(key, None)
    _clear_session_cookie()


def change_password(new_password, confirm_password):
    """-> (ok, message)"""
    client = st.session_state.get("sb_client")
    if not client:
        return False, "Not signed in."
    if not new_password:
        return False, "Password can't be empty."
    if new_password != confirm_password:
        return False, "The two passwords don't match."
    try:
        client.auth.update_user({"password": new_password})
        return True, "Password updated."
    except Exception as e:
        return False, f"Could not update password: {e}"


def create_user(supabase_url, service_key, username, password, make_admin=False):
    """Admin-only. -> (ok, message)"""
    username = (username or "").strip().lower()
    if not username or not password:
        return False, "Username and password are required."
    if not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
        return False, "Username may only contain letters, numbers, dots, hyphens and underscores."

    admin = get_admin_client(supabase_url, service_key)
    if not admin:
        return False, ("No service key configured — add SUPABASE_KEY (the service_role key) "
                       "to secrets, or create the user in the Supabase dashboard instead.")

    email = username_to_email(username)
    try:
        created = admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,  # no confirmation mail; account is usable immediately
        })
    except Exception as e:
        msg = str(e)
        if "already" in msg.lower():
            return False, f"A user named '{username}' already exists."
        return False, f"Could not create user: {msg}"

    new_id = getattr(created, "user", None)
    new_id = new_id.id if new_id else None
    if not new_id:
        return False, "User was created but no id came back — check the Supabase dashboard."

    try:
        admin.table("profiles").insert({
            "user_id": new_id, "username": username, "is_admin": make_admin,
        }).execute()
        admin.table("user_settings").insert({"user_id": new_id}).execute()
    except Exception as e:
        return True, (f"User '{username}' created, but setting up their profile failed: {e}")

    return True, f"User '{username}' created. They can sign in now and change their own password."


def list_users(supabase_url, service_key):
    admin = get_admin_client(supabase_url, service_key)
    if not admin:
        return []
    try:
        return admin.table("profiles").select("*").order("created_at").execute().data
    except Exception:
        return []


def render_login(supabase_url, anon_key):
    """Full-page login gate. Returns True once signed in."""
    st.title("Play Store Watchdog")
    st.caption("Sign in to continue.")

    with st.form("login_form"):
        username = st.text_input("Username", placeholder="shahrukh.gvs")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            ok, err = sign_in(supabase_url, anon_key, username, password)
            if ok:
                # CookieManager.set() needs a render cycle to reach the browser.
                # Rerunning instantly would abort the write and lose the session.
                time.sleep(0.6)
                st.rerun()
            else:
                st.error(err)

    st.caption("No account? Accounts are created by the administrator.")
    return False


def require_login(supabase_url, anon_key):
    """Gate the whole app. -> True if signed in, False if the login page was shown."""
    init_cookies()
    if st.session_state.get("sb_client") and st.session_state.get("sb_user"):
        return True
    if _restore_session(supabase_url, anon_key):
        return True
    render_login(supabase_url, anon_key)
    return False


def render_account_sidebar(supabase_url, anon_key):
    prof = current_profile() or {}
    with st.sidebar:
        st.markdown(f"**Signed in as** `{prof.get('username', 'unknown')}`")
        if prof.get("is_admin"):
            st.caption("Administrator")
        if st.button("Sign out", use_container_width=True):
            sign_out()
            st.rerun()