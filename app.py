from flask import Flask, jsonify, render_template
from flask_cors import CORS
import libsql
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get credentials
TURSO_URL = os.getenv("TURSO_URL")
TURSO_KEY = os.getenv("TURSO_KEY")

# Debug check (optional)
if not TURSO_URL or not TURSO_KEY:
    raise ValueError("TURSO_URL or TURSO_KEY is missing from .env")

# Initialize Flask
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
CORS(app)

# Create DB connection 
def get_connection():
    return libsql.connect(
        database=TURSO_URL,
        auth_token=TURSO_KEY
    )


def fetch_stats():
    conn = get_connection()
    cursor = conn.cursor()

    # Total users
    users_count = cursor.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    # Sessions (optional table)
    try:
        sessions_count = cursor.execute(
            "SELECT COUNT(*) FROM tutor_sessions"
        ).fetchone()[0]

        avg_sessions = (sessions_count / 30) if sessions_count else 0

    except Exception as e:
        print("Session table error:", e)
        sessions_count = None
        avg_sessions = None

    return {
        "users_count": users_count,
        "sessions_count": sessions_count,
        "avg_sessions": avg_sessions
    }


@app.route("/auth")
def auth():
        return render_template("verify.html")

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    if email == os.getenv("ADMIN_EMAIL") and password == os.getenv("ADMIN_PASSWORD"):
        return jsonify({"success": True})
    else:
        return jsonify({"success": False}), 401


@app.route("/api/user_stats", methods=["GET"])
def user_stats():
    try:
        data = fetch_stats()
        return jsonify(data)
    except Exception as e:
        print("Error fetching stats:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
