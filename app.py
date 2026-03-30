from flask import Flask, jsonify, render_template
from flask_cors import CORS
from libsql_client import create_client
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_URL")
TURSO_KEY = os.getenv("TURSO_KEY")



app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
CORS(app)


async def fetch_stats():
    async with create_client(TURSO_URL, auth_token=TURSO_KEY) as client:
        users_result = await client.execute("SELECT COUNT(*) FROM users")
        users_count = users_result.rows[0][0]

        try:
            sessions_result = await client.execute("SELECT COUNT(*) FROM sessions")
            sessions_count = sessions_result.rows[0][0]
            avg_sessions = round(sessions_count / 30) if sessions_count else 0
        except Exception:
            sessions_count = None
            avg_sessions = None

        return {
            "users_count": users_count,
            "sessions_count": sessions_count,
            "avg_sessions": avg_sessions
        }


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/user_stats", methods=['GET'])
def user_stats():
    try:
        data = asyncio.run(fetch_stats())
        return jsonify(data)
    except Exception as e:
        print(f"Error fetching stats: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
