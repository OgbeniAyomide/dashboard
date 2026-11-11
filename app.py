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


def fetch_dashboard_data():
    """Fetch all registered users + real dashboard KPIs from the database."""
    conn = get_connection()
    try:
        cursor = conn.cursor()

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
            ],
            ["join", "creat", "regist", "signup"],
        )

        def get_val(row, col_name):
            if not col_name or col_name not in col_idx:
                return None
            return row[col_idx[col_name]]

        users_list = []
        user_timestamps = []
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

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
        new_users_7d = (
            sum(1 for dt in user_timestamps if dt >= seven_days_ago)
            if user_timestamps
            else None
        )
        users_trend = _compute_period_trend(user_timestamps, 30, "last 30d") or {
            "text": "Registered total",
            "direction": "neutral",
            "current": users_count,
        }

        # Sessions stats (optional table)
        sessions_count = None
        avg_sessions = None
        sessions_trend = {"text": "No session table", "direction": "neutral"}

        try:
            sessions_count = cursor.execute("SELECT COUNT(*) FROM tutor_sessions").fetchone()[0]
            session_cols = _get_table_columns(cursor, "tutor_sessions")
            session_time_col = _match_column(
                session_cols,
                ["created_at", "started_at", "session_date", "timestamp", "date", "updated_at"],
                ["creat", "start", "time", "date"],
            )

            if session_time_col:
                s_rows = cursor.execute(
                    'SELECT "{}" FROM tutor_sessions WHERE "{}" IS NOT NULL'.format(session_time_col, session_time_col)
                ).fetchall()
                session_timestamps = []
                for (val,) in s_rows:
                    parsed_s = _parse_datetime(val)
                    if parsed_s is not None:
                        session_timestamps.append(parsed_s)

                if session_timestamps:
                    thirty_days_ago = now - timedelta(days=30)
                    recent_30d = sum(1 for dt in session_timestamps if dt >= thirty_days_ago)
                    base_for_avg = recent_30d if recent_30d > 0 else sessions_count
                    avg_sessions = round(base_for_avg / 30.0, 2) if base_for_avg else 0.0
                    sessions_trend = _compute_period_trend(session_timestamps, 7, "last 7d") or {
                        "text": "Recorded sessions",
                        "direction": "neutral",
                    }
                else:
                    avg_sessions = round(sessions_count / 30.0, 2) if sessions_count else 0.0
                    sessions_trend = {"text": "All recorded sessions", "direction": "neutral"}
            else:
                avg_sessions = round(sessions_count / 30.0, 2) if sessions_count else 0.0
                sessions_trend = {"text": "All recorded sessions", "direction": "neutral"}
        except Exception as e:
            app.logger.warning("Session table query skipped: %s", e)

        unique_levels = sorted(
            {u["level"] for u in users_list if u["level"] and u["level"] != "Unassigned"}
        )

        return {
            "users_count": users_count,
            "new_users_7d": new_users_7d,
            "sessions_count": sessions_count,
            "avg_sessions": avg_sessions,
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
            },
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
                "sessions_count": data["sessions_count"],
                "avg_sessions": data["avg_sessions"],
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

