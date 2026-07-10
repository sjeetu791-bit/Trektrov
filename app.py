import hashlib
import json
import os
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from user_agents import parse as parse_ua

import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

db.init_db()

# Trektrov Travels' pages already ship with this site_key hardcoded in their
# tracking snippet. Re-seed it on every boot so a redeploy (which can reset
# the sqlite file on hosts without a persistent disk) doesn't silently break
# tracking by leaving the embedded key pointing at a site that no longer
# exists.
SEED_SITE_KEY = os.environ.get("SEED_SITE_KEY", "wt_065123481cdee496")
SEED_SITE_NAME = os.environ.get("SEED_SITE_NAME", "Trektrov Travels")
SEED_SITE_DOMAIN = os.environ.get("SEED_SITE_DOMAIN", "trektrovtravels.in")
if SEED_SITE_KEY:
    db.ensure_site(SEED_SITE_NAME, SEED_SITE_DOMAIN, SEED_SITE_KEY)


# ---------------------------------------------------------------- auth ----

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------ dashboard ---

@app.route("/")
@login_required
def index():
    sites = db.list_sites()
    return render_template("index.html", sites=sites)


@app.route("/sites/new", methods=["POST"])
@login_required
def new_site():
    name = request.form.get("name", "").strip()
    domain = request.form.get("domain", "").strip()
    if name:
        db.create_site(name, domain)
    return redirect(url_for("index"))


@app.route("/sites/<site_key>/delete", methods=["POST"])
@login_required
def remove_site(site_key):
    db.delete_site(site_key)
    return redirect(url_for("index"))


@app.route("/site/<site_key>")
@login_required
def site_detail(site_key):
    site = db.get_site_by_key(site_key)
    if not site:
        return redirect(url_for("index"))
    range_key = request.args.get("range", "7d")
    stats = db.site_stats(site["id"], range_key)
    return render_template(
        "site_detail.html", site=site, stats=stats, range_key=range_key, request=request
    )


# ------------------------------------------------------------- tracking ---

@app.route("/t.js")
def tracking_script():
    js = render_template("track.js.j2", collect_url=request.url_root.rstrip("/") + "/collect")
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def _device_type(ua):
    if ua.is_mobile:
        return "Mobile"
    if ua.is_tablet:
        return "Tablet"
    if ua.is_pc:
        return "Desktop"
    return "Other"


def _lookup_geo(ip):
    if not ip or ip in ("127.0.0.1", "::1"):
        return "", ""
    cached = db.get_geo_cache(ip)
    if cached:
        return cached
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,city"},
            timeout=2,
        )
        data = r.json()
        if data.get("status") == "success":
            country = data.get("country", "") or ""
            city = data.get("city", "") or ""
            db.set_geo_cache(ip, country, city)
            return country, city
    except requests.RequestException:
        pass
    return "", ""


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


@app.route("/collect", methods=["POST", "OPTIONS"])
def collect():
    if request.method == "OPTIONS":
        return _cors(app.response_class(status=204))

    # navigator.sendBeacon delivers the body as a text/plain Blob (to avoid a
    # CORS preflight), so it won't be parsed by get_json() unless we fall back
    # to reading the raw body ourselves.
    payload = request.get_json(silent=True)
    if payload is None:
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            payload = {}

    site_key = payload.get("site_key", "")
    site = db.get_site_by_key(site_key)
    if not site:
        return _cors(jsonify({"error": "unknown site_key"})), 404

    event_type = payload.get("type", "pageview")

    if event_type == "duration":
        try:
            duration_ms = int(payload.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        db.update_pageview_duration(site["id"], payload.get("pageview_id", ""), duration_ms)
        return _cors(jsonify({"ok": True}))

    ua_string = request.headers.get("User-Agent", "")
    ua = parse_ua(ua_string)
    ip = _client_ip()
    country, city = _lookup_geo(ip)

    visitor_hash = hashlib.sha256(f"{site['id']}:{ip}:{ua_string}".encode()).hexdigest()[:32]

    url_val = payload.get("url", "")
    path_val = payload.get("path", "")

    db.insert_event(
        site["id"],
        type=event_type,
        url=url_val,
        path=path_val,
        referrer=payload.get("referrer", ""),
        browser=f"{ua.browser.family}",
        os=f"{ua.os.family}",
        device=_device_type(ua),
        country=country,
        city=city,
        visitor_hash=visitor_hash,
        session_id=payload.get("session_id", ""),
        pageview_id=payload.get("pageview_id", ""),
        event_name=payload.get("event_name", ""),
        event_data=json.dumps(payload.get("event_data", {})),
        created_at=datetime.utcnow().isoformat(),
    )
    return _cors(jsonify({"ok": True}))


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


if __name__ == "__main__":
    app.run(debug=True, port=5050)
