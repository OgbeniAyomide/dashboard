import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, Response, jsonify, render_template, request
from dotenv import load_dotenv

try:
    from flask_cors import CORS
except ImportError:
    CORS = None

try:
    import libsql
except ImportError:
    try:
        import libsql_experimental as libsql
    except ImportError:
        libsql = None

# Load environment variables
load_dotenv()

TURSO_URL = os.getenv("TURSO_URL")
TURSO_KEY = os.getenv("TURSO_KEY")
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH")  # Optional local SQLite path for local preview

if not LOCAL_DB_PATH and (not TURSO_URL or not TURSO_KEY):
    raise ValueError("TURSO_URL or TURSO_KEY is missing from .env")

# Automatically locate dashboard.html whether it is in ./templates/dashboard.html or ./dashboard.html
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = (
    os.path.join(BASE_DIR, "templates")
    if os.path.exists(os.path.join(BASE_DIR, "templates", "dashboard.html"))
    else BASE_DIR
)

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)

# Optional comma-separated allowed origins (defaults to same-origin instead of wildcard '*')
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if CORS and ALLOWED_ORIGINS:
    CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#1a1a2e"/>
  <rect x="7" y="7" width="7" height="7" rx="1.5" fill="none" stroke="#ffffff" stroke-width="2.2"/>
  <rect x="18" y="7" width="7" height="7" rx="1.5" fill="none" stroke="#ffffff" stroke-width="2.2"/>
  <rect x="7" y="18" width="7" height="7" rx="1.5" fill="none" stroke="#ffffff" stroke-width="2.2"/>
  <rect x="18" y="18" width="7" height="7" rx="1.5" fill="none" stroke="#a5b4fc" stroke-width="2.2"/>
</svg>"""


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def get_connection():
    """Open a fresh connection to Turso (libSQL) or optional local SQLite DB."""
    if LOCAL_DB_PATH:
        return sqlite3.connect(LOCAL_DB_PATH)
    if libsql is None:
        raise RuntimeError("Neither libsql nor libsql_experimental is installed.")
    return libsql.connect(
        database=TURSO_URL,
        auth_token=TURSO_KEY,
    )


def _match_column(columns, exact_candidates, substring_candidates=()):
    """Case-insensitively match the best column name from a table's column list."""
    lower_map = {col.lower(): col for col in columns}
    for cand in exact_candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    for sub in substring_candidates:
        sub_l = sub.lower()
        for col in columns:
            if sub_l in col.lower():
                return col
    return None


def _get_table_columns(cursor, table_name):
    """Safely inspect column names for a table in SQLite / Turso libSQL."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
        return []
    try:
        info_rows = cursor.execute("PRAGMA table_info({})".format(table_name)).fetchall()
        if info_rows:
            return [row[1] for row in info_rows]
    except Exception:
        pass
    try:
        cursor.execute("SELECT * FROM {} LIMIT 1".format(table_name)).fetchall()
        if cursor.description:
            return [desc[0] for desc in cursor.description]
    except Exception:
        pass
    return []


def _list_tables(cursor):
    """List all user tables (excluding sqlite internals)."""
    for sql in (
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ):
        try:
            rows = cursor.execute(sql).fetchall()
            if rows:
                return [r[0] for r in rows]
        except Exception:
            continue
    return []


def _safe_ident(name):
    """Quote an identifier that has already been validated against the real column list."""
    return '"{}"'.format(str(name).replace('"', '""'))


def _parse_datetime(raw_value):
    """Parse common SQLite / Turso timestamp formats into a UTC datetime."""
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        ts = float(raw_value)
        if ts > 1e12:
            ts /= 1000.0
        if ts <= 0:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(raw_value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d{10,13}(\.\d+)?", text):
        ts = float(text)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%b %d, %Y %H:%M:%S",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def _detect_datetime_column(cursor, table_name, columns, sample_limit=300):
    """
    Fallback scanner: find the column whose values most often parse as dates.
    Returns (column_name_or_None, parseable_count, total_sampled).
    Used when the standard name matcher finds no join/time column (e.g. live
    Turso `users` table whose timestamp column has an unexpected name).
    """
    if not table_name or not columns:
        return None, 0, 0
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
        return None, 0, 0
    try:
        rows = cursor.execute(
            "SELECT * FROM {} LIMIT {}".format(table_name, int(sample_limit))
        ).fetchall()
    except Exception:
        return None, 0, 0
    if not rows:
        return None, 0, 0

    best_col, best_hits = None, 0
    now_utc = datetime.now(timezone.utc)
    max_plausible = now_utc + timedelta(days=2)
    min_plausible = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for idx, col in enumerate(columns):
        # Never treat id-like columns as dates: small integer ids (1, 2, 3...)
        # would otherwise parse as 1970 epoch dates and produce a fake "0 in 7d".
        col_l = str(col).lower()
        if col_l == "id" or col_l == "uid" or col_l == "uuid" or col_l.endswith("_id") or col_l.endswith("id"):
            # Only skip if the name is purely id-like; names like "valid" contain
            # "id" but are not ids — require exact/underscore boundary match.
            if col_l == "id" or col_l == "uid" or col_l == "uuid" or col_l.endswith("_id"):
                continue
        hits = 0
        for row in rows:
            try:
                val = row[idx]
            except Exception:
                continue
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            # Small integers are ids/counts, not epoch dates (epoch dates are >= 1e9)
            if isinstance(val, (int, float)) and float(val) < 1e9:
                continue
            if isinstance(val, str) and re.fullmatch(r"\d{1,9}(\.\d+)?", val.strip()):
                # Short numeric strings are ids, not 10-digit epoch timestamps
                continue
            parsed = _parse_datetime(val)
            if parsed is None:
                continue
            # Require plausible app dates (2000 → near future) to avoid 1970 ids
            if parsed < min_plausible or parsed > max_plausible:
                continue
            hits += 1
        # Require at least 2 parseable values and a clear majority to avoid
        # false positives from small integer ids.
        if hits >= 2 and hits > best_hits:
            best_col, best_hits = col, hits

    total = len(rows)
    if best_col and best_hits >= max(2, int(total * 0.4)):
        return best_col, best_hits, total
    return None, best_hits, total


def _fetch_timestamps(cursor, table_name, time_col):
    """Fetch and parse all timestamps from a time column. Returns list of datetimes."""
    if not table_name or not time_col:
        return []
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
        return []
    try:
        rows = cursor.execute(
            "SELECT {} FROM {} WHERE {} IS NOT NULL".format(
                _safe_ident(time_col), table_name, _safe_ident(time_col)
            )
        ).fetchall()
    except Exception:
        return []
    out = []
    for (val,) in rows:
        parsed = _parse_datetime(val)
        if parsed is not None:
            out.append(parsed)
    return out


def _compute_daily_series(timestamps, days=14, now=None):
    """Actual count per calendar day (UTC) for the last `days` days, zeros included."""
    if now is None:
        now = datetime.now(timezone.utc)
    counts = {}
    for dt in timestamps:
        key = dt.date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        series.append({"date": day, "count": counts.get(day, 0)})
    return series


def _count_since(timestamps, cutoff):
    return sum(1 for dt in timestamps if dt >= cutoff)


def _compute_period_trend(timestamps, days, unit_label):
    """Compute real percentage or count change across rolling `days` vs prior `days`."""
    if not timestamps:
        return None

    now = datetime.now(timezone.utc)
    current_cutoff = now - timedelta(days=days)
    previous_cutoff = now - timedelta(days=days * 2)

    current_count = sum(1 for dt in timestamps if dt >= current_cutoff)
    previous_count = sum(1 for dt in timestamps if previous_cutoff <= dt < current_cutoff)

    if previous_count > 0:
        pct = ((current_count - previous_count) / previous_count) * 100.0
        if pct > 0.05:
            return {"text": "↑ {:.1f}% {}".format(pct, unit_label), "direction": "up", "current": current_count}
        if pct < -0.05:
            return {"text": "↓ {:.1f}% {}".format(abs(pct), unit_label), "direction": "down", "current": current_count}
        return {"text": "0.0% {}".format(unit_label), "direction": "neutral", "current": current_count}

    if current_count > 0:
        return {"text": "+{} {}".format(current_count, unit_label), "direction": "up", "current": current_count}

    return {"text": "0 new {}".format(unit_label), "direction": "neutral", "current": 0}


def _fetch_top_topics(cursor, table_name, topic_col, limit=5):
    """Top values of a topic/title column. Returns [{topic, count}]."""
    if not table_name or not topic_col:
        return []
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
        return []
    try:
        rows = cursor.execute(
            "SELECT {}, COUNT(*) AS c FROM {} WHERE {} IS NOT NULL AND TRIM(CAST({} AS TEXT)) != '' "
            "GROUP BY {} ORDER BY c DESC LIMIT {}".format(
                _safe_ident(topic_col), table_name,
                _safe_ident(topic_col), _safe_ident(topic_col),
                _safe_ident(topic_col), int(limit),
            )
        ).fetchall()
        return [{"topic": str(r[0]), "count": int(r[1])} for r in rows if r[0] is not None]
    except Exception:
        return []


def _discover_chat_metrics(cursor, now, tables):
    """
    Auto-discover chat/message usage from whatever tables exist.
    Priority: chat_messages > messages > chats > conversations > chat_sessions
              > tutor_chats > tutor_sessions > sessions.
    Falls back to tutor_sessions (session activity) when no dedicated chat
    table exists, so the dashboard always shows real usage numbers.
    """
    empty = {
        "available": False,
        "source_table": None,
        "source_label": "Chat",
        "total": None,
        "today": None,
        "yesterday": None,
        "last_7d": None,
        "last_30d": None,
        "per_day_7d": None,
        "active_users": None,
        "avg_per_active_user": None,
        "daily_series": [],
        "trend": {"text": "No chat table detected", "direction": "neutral"},
        "top_topics": [],
        "time_column": None,
    }
    if not tables:
        return empty

    priority = {
        "chat_messages": 100, "messages": 95, "chats": 90, "chat": 88,
        "conversations": 85, "conversation": 84, "chat_sessions": 82,
        "tutor_chats": 80, "chat_history": 78, "tutor_sessions": 70,
        "sessions": 60, "session": 58,
    }

    def score(name):
        n = name.lower()
        s = priority.get(n, 0)
        if "chat" in n:
            s += 50
        if "message" in n:
            s += 50
        if "conversation" in n:
            s += 40
        if "session" in n:
            s += 30
        return s

    candidates = sorted(tables, key=score, reverse=True)
    chosen = None
    chosen_score = 0
    for name in candidates:
        sc = score(name)
        if sc >= 60:
            chosen = name
            chosen_score = sc
            break
    if not chosen:
        return empty

    try:
        total = cursor.execute("SELECT COUNT(*) FROM {}".format(chosen)).fetchone()[0]
    except Exception:
        return empty

    cols = _get_table_columns(cursor, chosen)
    time_col = _match_column(
        cols,
        ["created_at", "started_at", "timestamp", "date", "time", "datetime",
         "createdat", "message_at", "sent_at", "updated_at"],
        ["creat", "start", "time", "date", "sent"],
    )
    if not time_col:
        found, _, _ = _detect_datetime_column(cursor, chosen, cols)
        time_col = found

    user_col = _match_column(
        cols,
        ["user_id", "userid", "uid", "author_id", "sender_id", "student_id", "userId"],
        ["user", "author", "sender", "student"],
    )
    topic_col = _match_column(
        cols,
        ["topic", "title", "subject", "category", "name"],
        ["topic", "title", "subject"],
    )

    timestamps = _fetch_timestamps(cursor, chosen, time_col) if time_col else []
    today_utc = now.date()
    today_start = datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc)
    yesterday_start = today_start - timedelta(days=1)
    seven_ago = now - timedelta(days=7)
    thirty_ago = now - timedelta(days=30)

    today_count = sum(1 for dt in timestamps if dt >= today_start)
    yesterday_count = sum(1 for dt in timestamps if yesterday_start <= dt < today_start)
    last_7d = _count_since(timestamps, seven_ago)
    last_30d = _count_since(timestamps, thirty_ago)
    per_day_7d = round(last_7d / 7.0, 2) if timestamps else None

    active_users = None
    if user_col:
        try:
            active_users = cursor.execute(
                "SELECT COUNT(DISTINCT {}) FROM {} WHERE {} IS NOT NULL".format(
                    _safe_ident(user_col), chosen, _safe_ident(user_col)
                )
            ).fetchone()[0]
        except Exception:
            active_users = None

    avg_per_active = None
    if active_users:
        avg_per_active = round(float(total) / float(active_users), 2)

    trend = _compute_period_trend(timestamps, 7, "last 7d") or {
        "text": "Recorded activity", "direction": "neutral"
    }

    if chosen.lower() in ("chat_messages", "messages"):
        label = "Chat Messages"
    elif chosen.lower() in ("chats", "chat", "conversations", "conversation", "chat_sessions", "tutor_chats", "chat_history"):
        label = "Chats"
    elif chosen.lower() in ("tutor_sessions", "sessions", "session"):
        label = "Tutor Sessions"
    else:
        label = chosen.replace("_", " ").title()

    return {
        "available": True,
        "source_table": chosen,
        "source_label": label,
        "total": int(total),
        "today": int(today_count) if timestamps else None,
        "yesterday": int(yesterday_count) if timestamps else None,
        "last_7d": int(last_7d) if timestamps else None,
        "last_30d": int(last_30d) if timestamps else None,
        "per_day_7d": per_day_7d,
        "active_users": int(active_users) if active_users is not None else None,
        "avg_per_active_user": avg_per_active,
        "daily_series": _compute_daily_series(timestamps, 14, now),
        "trend": trend,
        "top_topics": _fetch_top_topics(cursor, chosen, topic_col),
        "time_column": time_col,
    }


def fetch_dashboard_data():
    """Fetch all registered users + real dashboard KPIs from the database."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc)
        today_utc = now.date()
        today_start = datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc)
        yesterday_start = today_start - timedelta(days=1)
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)

        # ---- Schema discovery (tables + columns) ----
        tables = _list_tables(cursor)
        table_details = []
        for t in tables:
            try:
                rc = cursor.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
            except Exception:
                rc = None
            table_details.append({
                "name": t,
                "columns": _get_table_columns(cursor, t),
                "row_count": int(rc) if rc is not None else None,
            })

        # Fetch all rows from `users` and detect column names dynamically
        rows = cursor.execute("SELECT * FROM users").fetchall()
        user_cols = [desc[0] for desc in cursor.description] if cursor.description else _get_table_columns(cursor, "users")
        col_idx = {col: idx for idx, col in enumerate(user_cols)}

        id_col = _match_column(user_cols, ["id", "user_id", "uid", "uuid", "_id"])
        email_col = _match_column(
            user_cols,
            ["email", "mail", "user_email", "email_address", "e_mail"],
            ["mail"],
        )
        name_col = _match_column(
            user_cols,
            ["name", "full_name", "fullname", "display_name", "username", "user_name", "student_name"],
            ["name"],
        )
        first_name_col = _match_column(user_cols, ["first_name", "firstname", "given_name"])
        last_name_col = _match_column(user_cols, ["last_name", "lastname", "surname", "family_name"])
        level_col = _match_column(
            user_cols,
            [
                "level",
                "user_level",
                "academic_level",
                "education_level",
                "study_level",
                "class_level",
                "grade_level",
                "grade",
                "current_level",
                "class",
                "stage",
                "tier",
                "year",
            ],
            ["level", "grade"],
        )
        joined_col = _match_column(
            user_cols,
            [
                "created_at",
                "joined_at",
                "time_joined",
                "date_joined",
                "registered_at",
                "signup_at",
                "signup_date",
                "createdat",
                "joinedat",
                "created",
                "timestamp",
                "date",
                "time",
                "datetime",
                "created_on",
                "joined_on",
                "enrolled_at",
                "updated_at",
            ],
            ["join", "creat", "regist", "signup", "enroll"],
        )
        joined_source = "standard"
        if not joined_col:
            # Generic scanner: the live Turso `users` table may store join dates
            # under an unexpected column name — find the date-like column instead
            # of silently returning null for every user.
            found, hits, total_sampled = _detect_datetime_column(cursor, "users", user_cols)
            if found:
                joined_col = found
                joined_source = "auto-detected"
            else:
                joined_source = "not-found"

        def get_val(row, col_name):
            if not col_name or col_name not in col_idx:
                return None
            return row[col_idx[col_name]]

        users_list = []
        user_timestamps = []
        unparseable_joined = 0

        for idx, row in enumerate(rows, start=1):
            raw_id = get_val(row, id_col)
            raw_email = get_val(row, email_col)
            raw_name = get_val(row, name_col)
            raw_first = get_val(row, first_name_col)
            raw_last = get_val(row, last_name_col)
            raw_level = get_val(row, level_col)
            raw_joined = get_val(row, joined_col)

            # Avoid accidentally using first_name/last_name twice if `name_col` matched via substring
            name_str = ""
            if name_col not in (first_name_col, last_name_col) and raw_name is not None:
                name_str = str(raw_name).strip()
            if not name_str and (raw_first or raw_last):
                parts = [
                    str(raw_first).strip() if raw_first else "",
                    str(raw_last).strip() if raw_last else "",
                ]
                name_str = " ".join(p for p in parts if p)

            email_str = str(raw_email).strip() if raw_email is not None else ""
            if not name_str and email_str and "@" in email_str:
                local_part = email_str.split("@", 1)[0]
                name_str = re.sub(r"[._\-]+", " ", local_part).title()

            level_str = str(raw_level).strip() if raw_level is not None and str(raw_level).strip() else None

            parsed_dt = _parse_datetime(raw_joined)
            if parsed_dt is not None:
                user_timestamps.append(parsed_dt)
                joined_iso = parsed_dt.isoformat()
                sort_ts = parsed_dt.timestamp()
            else:
                if joined_col and raw_joined is not None and str(raw_joined).strip():
                    unparseable_joined += 1
                joined_iso = None
                sort_ts = -1.0

            users_list.append({
                "id": raw_id if raw_id is not None else idx,
                "name": name_str or "Unnamed User",
                "email": email_str or "—",
                "level": level_str or "Unassigned",
                "joined_at": joined_iso,
                "joined_raw": str(raw_joined).strip() if raw_joined is not None and str(raw_joined).strip() else None,
                "_sort_ts": sort_ts,
                "_row_order": idx,
            })

        users_list.sort(key=lambda u: (u["_sort_ts"], u["_row_order"]), reverse=True)
        for u in users_list:
            u.pop("_sort_ts", None)
            u.pop("_row_order", None)

        users_count = len(users_list)
        has_join_dates = len(user_timestamps) > 0
        if has_join_dates:
            new_users_7d = _count_since(user_timestamps, seven_days_ago)
            new_users_30d = _count_since(user_timestamps, thirty_days_ago)
            new_users_today = sum(1 for dt in user_timestamps if dt >= today_start)
            new_users_yesterday = sum(1 for dt in user_timestamps if yesterday_start <= dt < today_start)
            last_signup_at = max(user_timestamps).isoformat()
        else:
            # Honest null (not 0): the DB stores no usable join dates, so a
            # 7-day count cannot be computed. Frontend renders "—" + explanation.
            new_users_7d = None
            new_users_30d = None
            new_users_today = None
            new_users_yesterday = None
            last_signup_at = None

        users_trend = _compute_period_trend(user_timestamps, 30, "last 30d") or {
            "text": "No join dates in DB" if not has_join_dates else "Registered total",
            "direction": "neutral",
            "current": users_count,
        }

        # ---- Sessions stats (optional table) ----
        sessions_count = None
        sessions_today = None
        sessions_yesterday = None
        sessions_last_7d = None
        sessions_last_30d = None
        sessions_per_day_7d = None
        sessions_per_day_30d = None
        avg_sessions = None
        sessions_daily_series = []
        sessions_trend = {"text": "No session table", "direction": "neutral"}
        active_users_sessions = None
        avg_sessions_per_active_user = None
        top_topics = []
        session_time_col = None

        try:
            sessions_count = cursor.execute("SELECT COUNT(*) FROM tutor_sessions").fetchone()[0]
            session_cols = _get_table_columns(cursor, "tutor_sessions")
            session_time_col = _match_column(
                session_cols,
                ["created_at", "started_at", "session_date", "timestamp", "date", "time", "datetime", "updated_at"],
                ["creat", "start", "time", "date"],
            )
            if not session_time_col:
                found, _, _ = _detect_datetime_column(cursor, "tutor_sessions", session_cols)
                session_time_col = found

            session_timestamps = _fetch_timestamps(cursor, "tutor_sessions", session_time_col) if session_time_col else []

            if session_timestamps:
                sessions_today = sum(1 for dt in session_timestamps if dt >= today_start)
                sessions_yesterday = sum(1 for dt in session_timestamps if yesterday_start <= dt < today_start)
                sessions_last_7d = _count_since(session_timestamps, seven_days_ago)
                sessions_last_30d = _count_since(session_timestamps, thirty_days_ago)
                sessions_per_day_7d = round(sessions_last_7d / 7.0, 2)
                sessions_per_day_30d = round(sessions_last_30d / 30.0, 2)
                avg_sessions = sessions_per_day_30d  # backwards-compat alias
                sessions_daily_series = _compute_daily_series(session_timestamps, 14, now)
                sessions_trend = _compute_period_trend(session_timestamps, 7, "last 7d") or {
                    "text": "Recorded sessions", "direction": "neutral"
                }
            else:
                # No usable timestamps: daily counts unknown (null), totals still real
                avg_sessions = round(sessions_count / 30.0, 2) if sessions_count else 0.0
                sessions_trend = {"text": "All recorded sessions", "direction": "neutral"}

            # Active users + top topics from tutor_sessions
            session_user_col = _match_column(
                session_cols,
                ["user_id", "userid", "uid", "student_id", "userId"],
                ["user", "student"],
            )
            if session_user_col:
                try:
                    active_users_sessions = cursor.execute(
                        "SELECT COUNT(DISTINCT {}) FROM tutor_sessions WHERE {} IS NOT NULL".format(
                            _safe_ident(session_user_col), _safe_ident(session_user_col)
                        )
                    ).fetchone()[0]
                except Exception:
                    active_users_sessions = None
            if active_users_sessions:
                avg_sessions_per_active_user = round(float(sessions_count) / float(active_users_sessions), 2)

            session_topic_col = _match_column(
                session_cols,
                ["topic", "title", "subject", "category", "name"],
                ["topic", "title", "subject"],
            )
            top_topics = _fetch_top_topics(cursor, "tutor_sessions", session_topic_col)
        except Exception as e:
            app.logger.warning("Session table query skipped: %s", e)

        # ---- Chat usage (auto-discovered; falls back to tutor_sessions) ----
        try:
            chat = _discover_chat_metrics(cursor, now, tables)
        except Exception as e:
            app.logger.warning("Chat discovery skipped: %s", e)
            chat = {
                "available": False, "source_table": None, "source_label": "Chat",
                "total": None, "today": None, "yesterday": None,
                "last_7d": None, "last_30d": None, "per_day_7d": None,
                "active_users": None, "avg_per_active_user": None,
                "daily_series": [], "trend": {"text": "Chat metrics unavailable", "direction": "neutral"},
                "top_topics": [], "time_column": None,
            }

        unique_levels = sorted(
            {u["level"] for u in users_list if u["level"] and u["level"] != "Unassigned"}
        )

        return {
            "users_count": users_count,
            "new_users_7d": new_users_7d,
            "new_users_30d": new_users_30d,
            "new_users_today": new_users_today,
            "new_users_yesterday": new_users_yesterday,
            "last_signup_at": last_signup_at,
            "joined_at_available": has_join_dates,
            "joined_at_source": joined_source,
            "unparseable_joined_count": unparseable_joined,
            "sessions_count": sessions_count,
            "sessions_today": sessions_today,
            "sessions_yesterday": sessions_yesterday,
            "sessions_last_7d": sessions_last_7d,
            "sessions_last_30d": sessions_last_30d,
            "sessions_per_day_7d": sessions_per_day_7d,
            "sessions_per_day_30d": sessions_per_day_30d,
            "avg_sessions": avg_sessions,
            "sessions_daily_series": sessions_daily_series,
            "users_daily_series": _compute_daily_series(user_timestamps, 14, now) if has_join_dates else [],
            "active_users_sessions": int(active_users_sessions) if active_users_sessions is not None else None,
            "avg_sessions_per_active_user": avg_sessions_per_active_user,
            "top_topics": top_topics,
            "chat": chat,
            "tables": tables,
            "table_details": table_details,
            "levels_count": len(unique_levels),
            "users_trend": users_trend,
            "sessions_trend": sessions_trend,
            "users": users_list,
            "levels": unique_levels,
            "detected_columns": {
                "id": id_col,
                "name": name_col if name_col not in (first_name_col, last_name_col) else "{}+{}".format(first_name_col, last_name_col),
                "email": email_col,
                "level": level_col,
                "joined_at": joined_col,
                "session_time": session_time_col,
            },
            "users_columns": user_cols,
            "today_utc": today_utc.isoformat(),
            "generated_at": now.isoformat(),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/favicon.ico")
def favicon():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/schema", methods=["GET"])
def schema():
    """Expose detected tables/columns so chat/join-date issues are diagnosable."""
    try:
        data = fetch_dashboard_data()
        return jsonify({
            "tables": data["tables"],
            "table_details": data["table_details"],
            "detected_columns": data["detected_columns"],
            "users_columns": data["users_columns"],
            "joined_at_available": data["joined_at_available"],
            "joined_at_source": data["joined_at_source"],
            "chat_source_table": data["chat"].get("source_table"),
            "chat_time_column": data["chat"].get("time_column"),
            "generated_at": data["generated_at"],
        })
    except Exception as e:
        app.logger.exception("Error fetching schema")
        return jsonify({"error": str(e)}), 500


@app.route("/api/user_stats", methods=["GET"])
def user_stats():
    """Returns flat KPI fields AND full `users` array so a single call populates everything."""
    try:
        data = fetch_dashboard_data()
        return jsonify(data)
    except Exception as e:
        app.logger.exception("Error fetching stats")
        return jsonify({"error": str(e)}), 500


@app.route("/api/users", methods=["GET"])
def list_users():
    try:
        data = fetch_dashboard_data()
        return jsonify({
            "total": len(data["users"]),
            "users": data["users"],
            "levels": data["levels"],
            "stats": {
                "users_count": data["users_count"],
                "new_users_7d": data["new_users_7d"],
                "new_users_30d": data["new_users_30d"],
                "new_users_today": data["new_users_today"],
                "sessions_count": data["sessions_count"],
                "sessions_today": data["sessions_today"],
                "sessions_last_7d": data["sessions_last_7d"],
                "sessions_per_day_7d": data["sessions_per_day_7d"],
                "sessions_per_day_30d": data["sessions_per_day_30d"],
                "avg_sessions": data["avg_sessions"],
                "active_users_sessions": data["active_users_sessions"],
                "avg_sessions_per_active_user": data["avg_sessions_per_active_user"],
                "top_topics": data["top_topics"],
                "chat": data["chat"],
                "joined_at_available": data["joined_at_available"],
                "last_signup_at": data["last_signup_at"],
                "levels_count": data["levels_count"],
                "users_trend": data["users_trend"],
                "sessions_trend": data["sessions_trend"],
                "generated_at": data["generated_at"],
            },
            "detected_columns": data["detected_columns"],
        })
    except Exception as e:
        app.logger.exception("Error fetching users list")
        return jsonify({"error": "Failed to load registered users", "detail": str(e)}), 500


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
