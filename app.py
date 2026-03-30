from flask  import Flask, jsonify, render_template
from flask_cors import CORS
import libsql_experimental as libsql
import os
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_URL")
TURSO_KEY = os.getenv("TURSO_KEY")


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
CORS(app)

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/user_stats", methods=['POST'])
def user_stats():
    conn=libsql.connect(TURSO_URL, auth_token=TURSO_KEY)
    cursor=conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count=cursor.fetchone()[0]
    conn.close()
    return user_count


while True:
    try:
        user_count=user_stats()

    except Exception as e:
        print(F"Error fetching total users: {e}")



if __name__ == '__main__':
    app.run(debug=True)