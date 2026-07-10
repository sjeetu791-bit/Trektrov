import json
import sqlite3
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webtrack.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT,
    site_key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    type TEXT NOT NULL,           -- 'pageview' or 'event'
    url TEXT,
    path TEXT,
    referrer TEXT,
    browser TEXT,
    os TEXT,
    device TEXT,
    country TEXT,
    city TEXT,
    visitor_hash TEXT,
    session_id TEXT,
    event_name TEXT,
    event_data TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (site_id) REFERENCES sites(id)
);

CREATE INDEX IF NOT EXISTS idx_events_site_time ON events(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_site_type ON events(site_id, type);

CREATE TABLE IF NOT EXISTS geo_cache (
    ip TEXT PRIMARY KEY,
    country TEXT,
    city TEXT,
    cached_at TEXT NOT NULL
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "pageview_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN pageview_id TEXT")
    if "duration_ms" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN duration_ms INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_pageview_id ON events(site_id, pageview_id)")
    conn.commit()


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.close()


def generate_site_key():
    return "wt_" + secrets.token_hex(8)


def create_site(name, domain):
    conn = get_conn()
    key = generate_site_key()
    conn.execute(
        "INSERT INTO sites (name, domain, site_key, created_at) VALUES (?, ?, ?, ?)",
        (name, domain, key, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return key


def ensure_site(name, domain, site_key):
    """Idempotently make sure a site with this exact key exists. Used to
    re-seed the tracking site on every boot, since the host's filesystem
    (and this sqlite file) may not persist across deploys."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sites (name, domain, site_key, created_at) VALUES (?, ?, ?, ?)",
        (name, domain, site_key, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def list_sites():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def get_site_by_key(site_key):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sites WHERE site_key = ?", (site_key,)).fetchone()
    conn.close()
    return row


def delete_site(site_key):
    conn = get_conn()
    site = conn.execute("SELECT id FROM sites WHERE site_key = ?", (site_key,)).fetchone()
    if site:
        conn.execute("DELETE FROM events WHERE site_id = ?", (site["id"],))
        conn.execute("DELETE FROM sites WHERE id = ?", (site["id"],))
        conn.commit()
    conn.close()


def insert_event(site_id, **fields):
    conn = get_conn()
    fields["site_id"] = site_id
    fields.setdefault("created_at", datetime.utcnow().isoformat())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    conn.execute(f"INSERT INTO events ({cols}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()
    conn.close()


def update_pageview_duration(site_id, pageview_id, duration_ms):
    if not pageview_id:
        return
    conn = get_conn()
    conn.execute(
        "UPDATE events SET duration_ms = ? WHERE site_id = ? AND pageview_id = ? AND type = 'pageview' "
        "AND (duration_ms IS NULL OR duration_ms < ?)",
        (duration_ms, site_id, pageview_id, duration_ms),
    )
    conn.commit()
    conn.close()


def get_geo_cache(ip):
    conn = get_conn()
    row = conn.execute("SELECT * FROM geo_cache WHERE ip = ?", (ip,)).fetchone()
    conn.close()
    if row:
        cached_at = datetime.fromisoformat(row["cached_at"])
        if datetime.utcnow() - cached_at < timedelta(days=7):
            return row["country"], row["city"]
    return None


def set_geo_cache(ip, country, city):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO geo_cache (ip, country, city, cached_at) VALUES (?, ?, ?, ?)",
        (ip, country, city, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def _range_start(range_key):
    now = datetime.utcnow()
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "7d":
        return now - timedelta(days=7)
    if range_key == "30d":
        return now - timedelta(days=30)
    return None  # all time


def _fmt_ms(ms):
    if not ms:
        return "0s"
    total_seconds = int(ms / 1000)
    m, s = divmod(total_seconds, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _parse_event_data(rows):
    parsed = []
    for r in rows:
        try:
            parsed.append(json.loads(r["event_data"] or "{}"))
        except ValueError:
            continue
    return parsed


def _top_activities(rows, limit=10):
    counter = Counter()
    meta = {}
    for d in _parse_event_data(rows):
        key = d.get("id") or d.get("title")
        if not key:
            continue
        counter[key] += 1
        meta[key] = d
    return [{"count": c, **meta[k]} for k, c in counter.most_common(limit)]


def _tour_preferences(activity_rows, filter_rows, limit=10):
    city_counter = Counter()
    category_counter = Counter()
    for d in _parse_event_data(activity_rows):
        if d.get("city"):
            city_counter[d["city"]] += 1
        if d.get("category"):
            category_counter[d["category"]] += 1
    for d in _parse_event_data(filter_rows):
        if d.get("city") and d["city"] != "all":
            city_counter[d["city"]] += 1
        for cat in d.get("categories") or []:
            category_counter[cat] += 1
    return city_counter.most_common(limit), category_counter.most_common(limit)


def site_stats(site_id, range_key="7d"):
    conn = get_conn()
    start = _range_start(range_key)
    where = "site_id = ?"
    params = [site_id]
    if start:
        where += " AND created_at >= ?"
        params.append(start.isoformat())

    total_pageviews = conn.execute(
        f"SELECT COUNT(*) c FROM events WHERE {where} AND type='pageview'", params
    ).fetchone()["c"]

    unique_visitors = conn.execute(
        f"SELECT COUNT(DISTINCT visitor_hash) c FROM events WHERE {where}", params
    ).fetchone()["c"]

    total_events = conn.execute(
        f"SELECT COUNT(*) c FROM events WHERE {where} AND type='event'", params
    ).fetchone()["c"]

    top_pages = conn.execute(
        f"SELECT path, COUNT(*) c FROM events WHERE {where} AND type='pageview' "
        f"GROUP BY path ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    top_referrers = conn.execute(
        f"SELECT COALESCE(NULLIF(referrer, ''), 'Direct / None') referrer, COUNT(*) c "
        f"FROM events WHERE {where} AND type='pageview' GROUP BY referrer ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    devices = conn.execute(
        f"SELECT device, COUNT(*) c FROM events WHERE {where} AND type='pageview' "
        f"GROUP BY device ORDER BY c DESC",
        params,
    ).fetchall()

    browsers = conn.execute(
        f"SELECT browser, COUNT(*) c FROM events WHERE {where} AND type='pageview' "
        f"GROUP BY browser ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    os_list = conn.execute(
        f"SELECT os, COUNT(*) c FROM events WHERE {where} AND type='pageview' "
        f"GROUP BY os ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    countries = conn.execute(
        f"SELECT COALESCE(NULLIF(country, ''), 'Unknown') country, COUNT(*) c "
        f"FROM events WHERE {where} AND type='pageview' GROUP BY country ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    recent_events = conn.execute(
        f"SELECT * FROM events WHERE {where} AND type='event' ORDER BY created_at DESC LIMIT 20",
        params,
    ).fetchall()

    timeseries = conn.execute(
        f"SELECT substr(created_at, 1, 10) day, COUNT(*) c FROM events "
        f"WHERE {where} AND type='pageview' GROUP BY day ORDER BY day ASC",
        params,
    ).fetchall()

    avg_durations = conn.execute(
        f"SELECT path, COUNT(*) c, AVG(duration_ms) avg_ms FROM events "
        f"WHERE {where} AND type='pageview' AND duration_ms IS NOT NULL "
        f"GROUP BY path ORDER BY c DESC LIMIT 10",
        params,
    ).fetchall()

    session_durations = conn.execute(
        f"SELECT session_id, SUM(duration_ms) total_ms FROM events "
        f"WHERE {where} AND type='pageview' AND duration_ms IS NOT NULL AND session_id != '' "
        f"GROUP BY session_id",
        params,
    ).fetchall()

    activity_view_rows = conn.execute(
        f"SELECT event_data FROM events WHERE {where} AND type='event' AND event_name='activity_view'",
        params,
    ).fetchall()

    filter_rows = conn.execute(
        f"SELECT event_data FROM events WHERE {where} AND type='event' AND event_name='search_filter'",
        params,
    ).fetchall()

    conn.close()

    avg_session_ms = (
        sum(r["total_ms"] for r in session_durations) / len(session_durations)
        if session_durations else 0
    )
    tour_city_pref, tour_category_pref = _tour_preferences(activity_view_rows, filter_rows)

    return {
        "total_pageviews": total_pageviews,
        "unique_visitors": unique_visitors,
        "total_events": total_events,
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "devices": devices,
        "browsers": browsers,
        "os_list": os_list,
        "countries": countries,
        "recent_events": recent_events,
        "timeseries": timeseries,
        "avg_durations": [
            {"path": r["path"], "views": r["c"], "avg_ms": r["avg_ms"], "avg_fmt": _fmt_ms(r["avg_ms"])}
            for r in avg_durations
        ],
        "avg_session_ms": avg_session_ms,
        "avg_session_fmt": _fmt_ms(avg_session_ms),
        "session_count": len(session_durations),
        "top_activities": _top_activities(activity_view_rows),
        "tour_city_pref": tour_city_pref,
        "tour_category_pref": tour_category_pref,
    }
