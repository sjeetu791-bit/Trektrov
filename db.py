import sqlite3
import os
import secrets
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


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
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

    conn.close()
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
    }
