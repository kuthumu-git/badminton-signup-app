# app.py
# Streamlit Badminton Signup & Waitlist App — Google Sheets backend (Streamlit Cloud ready)
# --------------------------------------------------------------------------------------
# What changed vs. SQLite version
# - Data persistence now uses Google Sheets via gspread.
# - Works great on Streamlit Community Cloud using st.secrets.
# - Two worksheets: `sessions` and `signups`.
# - Same logic: core gets priority; outsiders waitlisted; admin auto-fills after cutoff.
#
# Setup (for Streamlit Cloud)
# 1) Create a Google Service Account and a Google Sheet with two empty tabs named `sessions` and `signups`.
# 2) Share the Sheet with the service account email (Editor).
# 3) In Streamlit Cloud, add Secrets with keys:
#    [gcp_service_account]
#    type = "service_account"
#    project_id = "..."
#    private_key_id = "..."
#    private_key = "-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
"
#    client_email = "...@...gserviceaccount.com"
#    client_id = "..."
#    auth_uri = "https://accounts.google.com/o/oauth2/auth"
#    token_uri = "https://oauth2.googleapis.com/token"
#    auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
#    client_x509_cert_url = "..."
#
#    [app]
#    sheet_id = "YOUR_GOOGLE_SHEET_ID"
#    admin_pin = "1234"  # change this
#
# 4) Create headers in the two tabs (or the app will auto-create them):
#    sessions: id, session_date, capacity, cutoff_utc, title, notes
#    signups: id, session_id, name, role, added_by, created_utc, status
#
# 5) requirements.txt should include: streamlit, gspread, google-auth

import datetime as dt
import uuid
from typing import Optional, List, Tuple

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# ---------------------- Config ----------------------
ADMIN_PIN = st.secrets.get("app", {}).get("admin_pin", "1234")
SHEET_ID = st.secrets.get("app", {}).get("sheet_id", "")

# ---------------------- Google Sheets Helpers ----------------------
@st.cache_resource(show_spinner=False)
def get_client():
    creds_info = st.secrets["gcp_service_account"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def get_sheet():
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)
    # Ensure worksheets exist
    try:
        ws_sessions = sh.worksheet("sessions")
    except gspread.WorksheetNotFound:
        ws_sessions = sh.add_worksheet("sessions", rows=1000, cols=10)
    try:
        ws_signups = sh.worksheet("signups")
    except gspread.WorksheetNotFound:
        ws_signups = sh.add_worksheet("signups", rows=5000, cols=12)

    # Ensure headers
    if not ws_sessions.get_all_values():
        ws_sessions.append_row(["id","session_date","capacity","cutoff_utc","title","notes"]) 
    if not ws_signups.get_all_values():
        ws_signups.append_row(["id","session_id","name","role","added_by","created_utc","status"]) 
    return sh, ws_sessions, ws_signups

# Utility

def utc_now_str() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()

# Data access

def read_sessions() -> List[dict]:
    _, ws_sessions, _ = get_sheet()
    rows = ws_sessions.get_all_records()
    # Normalize types
    for r in rows:
        r["capacity"] = int(r.get("capacity", 0) or 0)
    # Sort newest first by date
    try:
        rows.sort(key=lambda r: r.get("session_date",""), reverse=True)
    except Exception:
        pass
    return rows


def write_session(session_date: dt.date, capacity: int, cutoff_utc: dt.datetime, title: str, notes: str) -> str:
    sh, ws_sessions, _ = get_sheet()
    sid = str(uuid.uuid4())
    ws_sessions.append_row([
        sid,
        session_date.isoformat(),
        str(capacity),
        cutoff_utc.replace(microsecond=0).isoformat(),
        title,
        notes,
    ])
    return sid


def read_signups(session_id: str) -> List[dict]:
    _, _, ws_signups = get_sheet()
    rows = ws_signups.get_all_records()
    return [r for r in rows if r.get("session_id") == session_id and r.get("status") != "removed"]


def append_signup(session_id: str, name: str, role: str, added_by: Optional[str]) -> Tuple[bool, str]:
    if not name.strip():
        return False, "Name is required."
    if role not in ("core","outsider"):
        return False, "Invalid role."

    # Check duplicate
    existing = read_signups(session_id)
    if any(s["name"].strip().lower() == name.strip().lower() for s in existing):
        return False, f"{name} is already listed for this session."

    _, _, ws_signups = get_sheet()
    ws_signups.append_row([
        str(uuid.uuid4()),
        session_id,
        name.strip(),
        role,
        (added_by or "").strip(),
        utc_now_str(),
        "waitlist",
    ])
    # Re-apply priority (writes back confirmed statuses)
    apply_priority_logic(session_id)
    return True, f"Added {name} as {role}."


def update_signup_status(signup_id: str, new_status: str):
    _, _, ws_signups = get_sheet()
    data = ws_signups.get_all_values()
    headers = data[0]
    idx_map = {h:i for i,h in enumerate(headers)}
    for r_i, row in enumerate(data[1:], start=2):  # 1-based with header
        if row[idx_map["id"]] == signup_id:
            ws_signups.update_cell(r_i, idx_map["status"]+1, new_status)
            return


def get_session_by_id(session_id: str) -> Optional[dict]:
    sessions = read_sessions()
    for s in sessions:
        if s["id"] == session_id:
            return s
    return None


def list_sessions(limit: int = 50) -> List[Tuple[str,str,int,str,str]]:
    sessions = read_sessions()
    out = []
    for s in sessions[:limit]:
        out.append((s["id"], s["session_date"], s["capacity"], s["cutoff_utc"], s.get("title","")))
    return out

# Logic

def apply_priority_logic(session_id: str):
    s = get_session_by_id(session_id)
    if not s:
        return
    capacity = int(s["capacity"]) if s else 0
    signups = read_signups(session_id)
    # Order: core first, then outsiders; each by created_utc
    signups.sort(key=lambda r: (0 if r["role"]=="core" else 1, r["created_utc"]))

    # Determine who is confirmed vs waitlist
    to_confirm = set()
    for i, r in enumerate(signups):
        if i < capacity:
            to_confirm.add(r["id"])

    # Write back statuses
    _, _, ws_signups = get_sheet()
    data = ws_signups.get_all_values()
    headers = data[0]
    idx_map = {h:i for i,h in enumerate(headers)}
    for r_i, row in enumerate(data[1:], start=2):
        if row[idx_map["session_id"]] == session_id and row[idx_map["status"]] != "removed":
            sid = row[idx_map["id"]]
            desired = "confirmed" if sid in to_confirm else "waitlist"
            if row[idx_map["status"]] != desired:
                ws_signups.update_cell(r_i, idx_map["status"]+1, desired)


def auto_fill_from_waitlist(session_id: str, force: bool = False) -> Tuple[int,int]:
    s = get_session_by_id(session_id)
    if not s:
        return 0, 0
    capacity = int(s["capacity"])
    cutoff = dt.datetime.fromisoformat(s["cutoff_utc"]) if s.get("cutoff_utc") else None
    if not force and cutoff and dt.datetime.utcnow() < cutoff:
        return 0, 0

    signups = read_signups(session_id)
    confirmed = [r for r in signups if r["status"] == "confirmed"]
    remaining = max(0, capacity - len(confirmed))
    if remaining == 0:
        return 0, 0

    # Candidates: everyone not confirmed, order outsiders first by created_utc (since cores should already be ahead)
    candidates = [r for r in signups if r["status"] != "confirmed"]
    # Order: outsiders first, then any remaining cores (unlikely), by time
    candidates.sort(key=lambda r: (0 if r["role"]=="outsider" else 1, r["created_utc"]))

    promoted = 0
    for r in candidates:
        if promoted >= remaining:
            break
        update_signup_status(r["id"], "confirmed")
        promoted += 1
    return promoted, max(0, remaining - promoted)

# ---------------------- UI ----------------------
st.set_page_config(page_title="Badminton Signup", page_icon="🏸", layout="wide")

st.title("🏸 Badminton Signup & Waitlist (Google Sheets)")

view_tab, signup_tab, admin_tab = st.tabs(["📋 Current Session", "✍️ Sign Up", "🔐 Admin"]) 

# -------- Current Session Tab --------
with view_tab:
    sessions = list_sessions(limit=100)
    if not SHEET_ID:
        st.error("Google Sheet ID missing. Set [app].sheet_id in Streamlit secrets.")
    if not sessions:
        st.info("No sessions yet. Ask admin to create one in the Admin tab.")
    else:
        options = {f"{s[1]} • {s[4] or 'Session'} (cap {s[2]})": s[0] for s in sessions}
        sel_label = st.selectbox("Choose a session", list(options.keys()))
        session_id = options[sel_label]
        s = get_session_by_id(session_id)

        st.subheader(s.get("title") or f"Session on {s['session_date']}")
        st.caption(f"Date: {s['session_date']} • Capacity: {s['capacity']} • Cutoff (UTC): {s['cutoff_utc']}")
        if s.get("notes"):
            st.write(s["notes"]) 

        signups = read_signups(session_id)
        # Build confirmed / waitlist lists
        confirmed = [r for r in signups if r["status"] == "confirmed"]
        waitlist = [r for r in signups if r["status"] != "confirmed"]

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### ✅ Confirmed")
            if confirmed:
                for i, r in enumerate(sorted(confirmed, key=lambda x: x["created_utc"])):
                    by = f" (by {r['added_by']})" if r['role']=="outsider" and r.get('added_by') else ""
                    st.write(f"{i+1}. {r['name']} — {r['role']}{by}")
            else:
                st.write("No one confirmed yet.")
        with c2:
            st.markdown("### ⏳ Waitlist")
            if waitlist:
                for i, r in enumerate(sorted(waitlist, key=lambda x: (0 if x['role']=='core' else 1, x['created_utc']))):
                    by = f" (by {r['added_by']})" if r['role']=="outsider" and r.get('added_by') else ""
                    st.write(f"{i+1}. {r['name']} — {r['role']}{by}")
            else:
                st.write("Empty.")

        st.divider()
        left = max(0, int(s['capacity']) - len(confirmed))
        st.metric("Spots remaining", left)

# -------- Sign Up Tab --------
with signup_tab:
    sessions = list_sessions(limit=100)
    if not sessions:
        st.info("No sessions available to join yet.")
    else:
        options = {f"{s[1]} • {s[4] or 'Session'} (cap {s[2]})": s[0] for s in sessions}
        sel_label = st.selectbox("Select session to join", list(options.keys()), key="join_select")
        session_id = options[sel_label]

        st.markdown("#### Who's joining?")
        role = st.radio("Role", ["core", "outsider"], horizontal=True)
        name = st.text_input("Player name")
        added_by = None
        if role == "outsider":
            added_by = st.text_input("Which core member is adding this outsider? (your name)")

        if st.button("Add to list"):
            ok, msg = append_signup(session_id, name, role, added_by)
            st.success(msg) if ok else st.error(msg)

# -------- Admin Tab --------
with admin_tab:
    st.markdown("#### Admin Login")
    pin = st.text_input("Enter admin PIN", type="password")
    if pin == ADMIN_PIN:
        st.success("Admin authenticated.")
        st.markdown("### Create Session")
        today = dt.date.today()
        next_sat = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        session_date = st.date_input("Session date", value=next_sat)
        capacity = st.number_input("Capacity", min_value=2, max_value=30, value=8, step=1)

        # Default cutoff: day before at 18:00 (assume UTC to keep things simple)
        cutoff_time = st.time_input("Cutoff time (UTC)", value=dt.time(18,0))
        cutoff_utc = dt.datetime.combine(session_date - dt.timedelta(days=1), cutoff_time)

        title = st.text_input("Title", value="Weekly Badminton")
        notes = st.text_area("Notes (court, fee, etc.)", value="Location: ...
Fee: ...")

        if st.button("Create session"):
            sid = write_session(session_date, int(capacity), cutoff_utc, title, notes)
            st.success(f"Session created ✔")

        st.divider()
        st.markdown("### Manage Sessions")
        sessions = list_sessions(limit=100)
        if sessions:
            labels = {f"{s[1]} • {s[4] or 'Session'} (cap {s[2]})": s[0] for s in sessions}
            pick = st.selectbox("Select session", list(labels.keys()), key="adm_select")
            session_id = labels[pick]
            s = get_session_by_id(session_id)

            st.caption(f"Cutoff (UTC): {s['cutoff_utc']}")
            signups = read_signups(session_id)
            confirmed = [r for r in signups if r["status"] == "confirmed"]
            st.write(f"Confirmed: {len(confirmed)} / {s['capacity']}")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("Run auto-fill now (respect cutoff)"):
                    promoted, remaining = auto_fill_from_waitlist(session_id, force=False)
                    if promoted:
                        st.success(f"Promoted {promoted} from waitlist. Remaining: {remaining}")
                    else:
                        st.info("No promotions (before cutoff or no spots).")
            with c2:
                if st.button("Force auto-fill (ignore cutoff)"):
                    promoted, remaining = auto_fill_from_waitlist(session_id, force=True)
                    st.warning(f"Forced promotion: {promoted} moved up. Remaining: {remaining}")

            # Remove a signup
            st.markdown("#### Remove a signup")
            all_active = sorted(signups, key=lambda r: (0 if r['status']=="confirmed" else 1, 0 if r['role']=="core" else 1, r['created_utc']))
            if all_active:
                names = {f"{r['name']} ({r['role']}, {r['status']})": r['id'] for r in all_active}
                to_remove = st.selectbox("Pick a player to remove", list(names.keys()))
                if st.button("Remove player"):
                    update_signup_status(names[to_remove], "removed")
                    st.success("Removed.")
            else:
                st.info("No signups yet.")
        else:
            st.info("No sessions yet.")
    elif pin:
        st.error("Wrong PIN.")

st.caption("Tip: Share this app link in WhatsApp. Core members can add outsiders under Sign Up. On/after cutoff, run Auto-fill in Admin.")
