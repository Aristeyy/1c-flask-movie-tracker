import base64
import csv
import io
import json
import logging
import os
import sqlite3
from datetime import date

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)

OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "movies.db")
POSTER_DIR = os.path.join(BASE_DIR, "static", "posters")
os.makedirs(POSTER_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS viewings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            film TEXT,
            film_id TEXT,
            rating INTEGER,
            date TEXT,
            emotion TEXT,
            genre TEXT,
            year INTEGER,
            imdb_rating REAL,
            note TEXT DEFAULT '',
            poster TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS omdb_cache (
            name_key TEXT PRIMARY KEY,
            payload TEXT,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    existing = {row[1] for row in c.execute("PRAGMA table_info(viewings)").fetchall()}
    for col in ("note", "poster"):
        if col not in existing:
            c.execute(f"ALTER TABLE viewings ADD COLUMN {col} TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def get_trailer_url(title):
    query = requests.utils.quote(f"{title} official trailer")
    return f"https://www.youtube.com/results?search_query={query}"


@app.route("/api/viewing", methods=["POST"])
def receive_viewing():
    try:
        data = request.get_json(force=True)
        conn = get_db()
        conn.execute('''
            INSERT INTO viewings (film, film_id, rating, date, emotion, genre, year, imdb_rating)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get("film", ""),
            data.get("film_id", ""),
            data.get("rating", 0),
            data.get("date", ""),
            data.get("emotion", ""),
            data.get("genre", ""),
            data.get("year", 0),
            data.get("imdb_rating", 0.0),
        ))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "message": "Данные сохранены"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/movies", methods=["GET"])
def get_movies():
    conn = get_db()
    rows = conn.execute("SELECT * FROM viewings ORDER BY date DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/movies", methods=["POST"])
def add_movie():
    try:
        data = request.get_json(force=True)
        if not data.get("film"):
            return jsonify({"status": "error", "message": "Название обязательно"}), 400
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO viewings (film, rating, date, emotion, genre, year, imdb_rating, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get("film", ""),
            int(data.get("rating", 0) or 0),
            data.get("date", ""),
            data.get("emotion", ""),
            data.get("genre", ""),
            int(data.get("year", 0) or 0),
            float(data.get("imdb_rating", 0) or 0),
            data.get("note", ""),
        ))
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return jsonify({"status": "ok", "id": new_id}), 201
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/movies/<int:movie_id>", methods=["PATCH", "DELETE"])
def modify_movie(movie_id):
    conn = get_db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM viewings WHERE id = ?", (movie_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})

    data = request.get_json(force=True)
    allowed = {"film", "rating", "date", "emotion", "genre", "year", "imdb_rating", "note"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        conn.close()
        return jsonify({"status": "error", "message": "Нет полей для обновления"}), 400
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE viewings SET {set_clause} WHERE id = ?", (*fields.values(), movie_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/movies/<int:movie_id>/poster", methods=["POST"])
def upload_poster(movie_id):
    try:
        payload = request.get_json(force=True)
        data_url = payload.get("image", "")
        if "," not in data_url:
            return jsonify({"status": "error", "message": "Некорректное изображение"}), 400
        header, b64 = data_url.split(",", 1)
        ext = "png"
        if "jpeg" in header or "jpg" in header:
            ext = "jpg"
        elif "webp" in header:
            ext = "webp"
        filename = f"poster_{movie_id}.{ext}"
        with open(os.path.join(POSTER_DIR, filename), "wb") as f:
            f.write(base64.b64decode(b64))
        rel_path = f"/static/posters/{filename}"
        conn = get_db()
        conn.execute("UPDATE viewings SET poster = ? WHERE id = ?", (rel_path, movie_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "poster": rel_path})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/export/csv", methods=["GET"])
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM viewings ORDER BY date DESC").fetchall()
    conn.close()
    output = io.StringIO()
    output.write("\ufeff")
    fieldnames = ["film", "genre", "rating", "date", "emotion", "year", "imdb_rating", "note"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: dict(row).get(k, "") for k in fieldnames})
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=kinodashboard_export.csv"},
    )


@app.route("/api/export/json", methods=["GET"])
def export_json():
    conn = get_db()
    rows = conn.execute("SELECT * FROM viewings ORDER BY date DESC").fetchall()
    conn.close()
    data = [dict(row) for row in rows]
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=kinodashboard_backup.json"},
    )


@app.route("/api/import/csv", methods=["POST"])
def import_csv():
    try:
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "Файл не передан"}), 400
        file = request.files["file"]
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        conn = get_db()
        count = 0
        for row in reader:
            conn.execute('''
                INSERT INTO viewings (film, genre, rating, date, emotion, year, imdb_rating, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                row.get("film", ""),
                row.get("genre", ""),
                int(row.get("rating") or 0),
                row.get("date", ""),
                row.get("emotion", ""),
                int(row.get("year") or 0),
                float(row.get("imdb_rating") or 0),
                row.get("note", ""),
            ))
            count += 1
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "imported": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/stats", methods=["GET"])
def stats():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM viewings").fetchall()]
    conn.close()

    total = len(rows)
    rated = [r["rating"] for r in rows if r["rating"]]
    avg = round(sum(rated) / len(rated), 2) if rated else 0

    by_genre = {}
    by_emotion = {}
    by_weekday = [0] * 7
    for r in rows:
        if r.get("genre"):
            by_genre[r["genre"]] = by_genre.get(r["genre"], 0) + 1
        if r.get("emotion"):
            by_emotion[r["emotion"]] = by_emotion.get(r["emotion"], 0) + 1
        d = r.get("date", "")
        if d and len(d) >= 10:
            try:
                wd = date.fromisoformat(d[:10]).weekday()
                by_weekday[wd] += 1
            except ValueError:
                pass

    best = max(rows, key=lambda r: r["rating"] or 0, default=None)
    return jsonify({
        "total": total,
        "avg_rating": avg,
        "by_genre": by_genre,
        "by_emotion": by_emotion,
        "by_weekday": by_weekday,
        "best_film": best["film"] if best else None,
        "best_rating": best["rating"] if best else None,
    })


@app.route("/api/recommend", methods=["GET"])
def recommend():
    conn = get_db()
    c = conn.cursor()
    favorite_genre = c.execute('''
        SELECT genre, AVG(rating) as avg_rating, COUNT(*) as cnt
        FROM viewings
        WHERE genre != ''
        GROUP BY genre
        ORDER BY avg_rating DESC, cnt DESC
        LIMIT 1
    ''').fetchone()

    if favorite_genre:
        genre_name = favorite_genre["genre"]
        top_movies = c.execute('''
            SELECT film, AVG(rating) as avg_rating
            FROM viewings
            WHERE genre = ? AND rating >= 8
            GROUP BY film
            ORDER BY avg_rating DESC
            LIMIT 3
        ''', (genre_name,)).fetchall()
        conn.close()
        return jsonify({
            "favorite_genre": genre_name,
            "recommendations": [{"film": m["film"], "avg_rating": round(m["avg_rating"], 1)} for m in top_movies],
        })

    conn.close()
    return jsonify({"favorite_genre": None, "recommendations": []})


@app.route("/api/fill", methods=["GET"])
def fill_from_omdb():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided"}), 400

    if not OMDB_API_KEY:
        return jsonify({"error": "OMDB_API_KEY не задан. Добавьте ключ в .env"}), 500

    key = name.lower()
    conn = get_db()
    cached = conn.execute("SELECT payload FROM omdb_cache WHERE name_key = ?", (key,)).fetchone()
    if cached:
        conn.close()
        return jsonify(json.loads(cached["payload"]))

    try:
        url = (
            f"https://www.omdbapi.com/?t={requests.utils.quote(name)}"
            f"&apikey={OMDB_API_KEY}&plot=full"
        )
        resp = requests.get(url, timeout=8, headers={"User-Agent": "KinoDashboard/1.0"})

        try:
            data = resp.json()
        except ValueError:
            conn.close()
            return jsonify({
                "error": "OMDB вернул не-JSON ответ. Вероятная причина: исчерпан "
                         "суточный лимит ключа (1000 запросов) или ключ недействителен.",
                "http_status": resp.status_code,
            }), 502

        if data.get("Response") == "True":
            result = {
                "title": data.get("Title", name),
                "year": data.get("Year", ""),
                "genre": data.get("Genre", "").split(",")[0],
                "director": data.get("Director", ""),
                "plot": data.get("Plot", ""),
                "imdb_rating": data.get("imdbRating", "0"),
                "poster_url": data.get("Poster", ""),
                "trailer_url": get_trailer_url(data.get("Title", name)),
            }
            conn.execute(
                "INSERT OR REPLACE INTO omdb_cache (name_key, payload) VALUES (?, ?)",
                (key, json.dumps(result, ensure_ascii=False)),
            )
            conn.commit()
            conn.close()
            return jsonify(result)
        else:
            conn.close()
            return jsonify({"error": data.get("Error", "Movie not found")}), 404
    except requests.exceptions.RequestException as e:
        conn.close()
        return jsonify({"error": f"Не удалось связаться с OMDB: {e}"}), 502
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/")
def dashboard():
    return render_template("index.html")


def main():
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    init_db()
    print("КиноДашборд: http://127.0.0.1:5000  (Ctrl+C — остановить)")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
