import csv, io, json, os, re, sqlite3, time, urllib.error, urllib.parse, urllib.request
from datetime import timedelta
from functools import wraps
from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DATABASE_PATH", os.path.join(BASE, "tandem.db"))
app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
if app.secret_key == "dev-only-change-me":
    print("WARNING: set the SECRET_KEY environment variable before deploying.")

TYPES = ["Fleet operators", "Corporate buyers", "Rental and cab firms",
         "Delivery and logistics", "Insurance and finance agents", "Driving schools"]
KEYWORDS = ["fleet", "vehicle", "van", "cab", "expan", "tender", "contract",
            "depot", "hire", "branch", "launch"]
SCHEMA = """
create table if not exists users(
  id integer primary key, email text unique not null, name text not null default '',
  pw text not null, cities text not null default '', types text not null default '[0,1,2,3]',
  created real not null);
create table if not exists companies(
  id integer primary key, user_id integer not null references users(id) on delete cascade,
  name text not null, type integer not null default 0, city text not null default '',
  website text not null default '', signal text not null default '', notes text not null default '',
  source text not null default 'manual', score real not null default 0, reasons text not null default '[]',
  stage integer not null default 1, draft text not null default '',
  q1 integer not null default 0, q2 integer not null default 0, q3 integer not null default 0, q4 integer not null default 0,
  test_drive integer not null default 0, quote_k real not null default 0, won integer not null default 0,
  created real not null);
create index if not exists ix_companies_user on companies(user_id);
create table if not exists demo_requests(id integer primary key, email text not null, created real not null);
create table if not exists find_usage(
  user_id integer not null references users(id) on delete cascade, day text not null, n integer not null default 0,
  primary key(user_id, day));
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("pragma foreign_keys=on")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def err(msg, code=400):
    return jsonify(error=msg), code


@app.before_request
def csrf_guard():
    if request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Requested-With") != "fetch":
            return err("Bad request", 403)


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"):
            return err("Please log in", 401)
        return f(*a, **k)
    return w


def me_row():
    return db().execute("select * from users where id=?", (session["uid"],)).fetchone()


def me_json(u):
    return {"email": u["email"], "name": u["name"], "cities": u["cities"],
            "types": json.loads(u["types"]), "ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "find": bool(os.environ.get("TAVILY_API_KEY")), "typeNames": TYPES}


def score_company(c, user):
    s, r = 0.15, ["+0.15 starting score"]
    if c["type"] in json.loads(user["types"]):
        s += 0.30; r.append("+0.30 matches a buyer type you target")
    cities = [x.strip().lower() for x in user["cities"].split(",") if x.strip()]
    if c["city"] and c["city"].lower() in cities:
        s += 0.25; r.append("+0.25 is in your area")
    if c["website"]:
        s += 0.05; r.append("+0.05 has a website on file")
    sig = (c["signal"] or "").lower()
    if sig.strip():
        s += 0.10; r.append("+0.10 you noted a recent signal")
        hits = [k for k in KEYWORDS if k in sig]
        if hits:
            b = min(0.15, 0.05 * len(hits)); s += b
            r.append("+%.2f the signal mentions %s" % (b, ", ".join(hits[:3])))
    return round(min(s, 1.0), 2), r


def out(row):
    d = dict(row)
    d["reasons"] = json.loads(d["reasons"])
    return d


def clean(v, n):
    return str(v or "").strip()[:n]


# ---------- pages ----------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- auth ----------
_tries = {}


@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    email, pw, name = clean(d.get("email"), 120).lower(), str(d.get("password") or ""), clean(d.get("name"), 80)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return err("Enter a valid email address.")
    if len(pw) < 8:
        return err("Password must be at least 8 characters.")
    if db().execute("select 1 from users where email=?", (email,)).fetchone():
        return err("An account with that email already exists.", 409)
    cur = db().execute("insert into users(email,name,pw,created) values(?,?,?,?)",
                       (email, name, generate_password_hash(pw), time.time()))
    db().commit()
    session.clear(); session["uid"] = cur.lastrowid; session.permanent = True
    return jsonify(me_json(me_row()))


@app.post("/api/login")
def login():
    ip = request.remote_addr or "?"
    n, t = _tries.get(ip, (0, time.time()))
    if time.time() - t > 600:
        n, t = 0, time.time()
    if n >= 8:
        return err("Too many attempts. Try again in a few minutes.", 429)
    d = request.get_json(silent=True) or {}
    u = db().execute("select * from users where email=?", (clean(d.get("email"), 120).lower(),)).fetchone()
    if not u or not check_password_hash(u["pw"], str(d.get("password") or "")):
        _tries[ip] = (n + 1, t)
        return err("Email or password is incorrect.", 401)
    _tries.pop(ip, None)
    session.clear(); session["uid"] = u["id"]; session.permanent = True
    return jsonify(me_json(u))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(me_json(me_row()))


@app.put("/api/profile")
@login_required
def profile():
    d = request.get_json(silent=True) or {}
    types = [t for t in (d.get("types") or []) if isinstance(t, int) and 0 <= t < len(TYPES)]
    db().execute("update users set cities=?, types=? where id=?",
                 (clean(d.get("cities"), 300), json.dumps(types), session["uid"]))
    db().commit()
    return jsonify(me_json(me_row()))


@app.post("/api/demo")
def demo():
    email = clean((request.get_json(silent=True) or {}).get("email"), 120).lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return err("Enter a valid email address.")
    db().execute("insert into demo_requests(email,created) values(?,?)", (email, time.time()))
    db().commit()
    return jsonify(ok=True)


# ---------- companies ----------
@app.get("/api/companies")
@login_required
def list_companies():
    rows = db().execute("select * from companies where user_id=? order by score desc, id desc",
                        (session["uid"],)).fetchall()
    return jsonify([out(r) for r in rows])


def save_new(user, d, source="manual"):
    name = clean(d.get("name"), 120)
    if not name:
        return None
    t = d.get("type")
    c = {"type": t if isinstance(t, int) and 0 <= t < len(TYPES) else 0, "city": clean(d.get("city"), 80),
         "website": clean(d.get("website"), 200), "signal": clean(d.get("signal"), 300)}
    s, r = score_company(c, user)
    cur = db().execute(
        "insert into companies(user_id,name,type,city,website,signal,source,score,reasons,created) values(?,?,?,?,?,?,?,?,?,?)",
        (user["id"], name, c["type"], c["city"], c["website"], c["signal"], source, s, json.dumps(r), time.time()))
    return cur.lastrowid


@app.post("/api/companies")
@login_required
def add_company():
    u = me_row()
    d = request.get_json(silent=True) or {}
    cid = save_new(u, d, "web search" if d.get("source") == "web search" else "manual")
    if not cid:
        return err("Company name is required.")
    db().commit()
    return jsonify(out(db().execute("select * from companies where id=?", (cid,)).fetchone())), 201


def owned(cid):
    return db().execute("select * from companies where id=? and user_id=?", (cid, session["uid"])).fetchone()


@app.patch("/api/companies/<int:cid>")
@login_required
def patch_company(cid):
    row = owned(cid)
    if not row:
        return err("Not found", 404)
    d = request.get_json(silent=True) or {}
    upd = {}
    for k, n in (("name", 120), ("city", 80), ("website", 200), ("signal", 300), ("notes", 4000), ("draft", 4000)):
        if k in d:
            upd[k] = clean(d[k], n)
    if "name" in upd and not upd["name"]:
        return err("Company name is required.")
    if "type" in d and isinstance(d["type"], int) and 0 <= d["type"] < len(TYPES):
        upd["type"] = d["type"]
    if "stage" in d and d["stage"] in (1, 2, 3, 4):
        upd["stage"] = d["stage"]
    for k in ("q1", "q2", "q3", "q4", "test_drive", "won"):
        if k in d:
            upd[k] = 1 if d[k] else 0
    if "quote_k" in d:
        try:
            upd["quote_k"] = max(0.0, min(float(d["quote_k"]), 1e7))
        except (TypeError, ValueError):
            return err("Quote must be a number.")
    if upd:
        if any(k in upd for k in ("type", "city", "website", "signal")):
            merged = {**dict(row), **upd}
            upd["score"], r = score_company(merged, me_row())
            upd["reasons"] = json.dumps(r)
        db().execute("update companies set " + ",".join(k + "=?" for k in upd) + " where id=?",
                     list(upd.values()) + [cid])
        db().commit()
    return jsonify(out(owned(cid)))


@app.post("/api/companies/<int:cid>/score")
@login_required
def rescore(cid):
    row = owned(cid)
    if not row:
        return err("Not found", 404)
    s, r = score_company(dict(row), me_row())
    db().execute("update companies set score=?, reasons=? where id=?", (s, json.dumps(r), cid))
    db().commit()
    return jsonify(out(owned(cid)))


@app.post("/api/rescore")
@login_required
def rescore_all():
    u = me_row()
    rows = db().execute("select * from companies where user_id=?", (u["id"],)).fetchall()
    for r in rows:
        sc, rs = score_company(dict(r), u)
        db().execute("update companies set score=?, reasons=? where id=?", (sc, json.dumps(rs), r["id"]))
    db().commit()
    return jsonify(updated=len(rows))


@app.delete("/api/companies/<int:cid>")
@login_required
def delete_company(cid):
    if not owned(cid):
        return err("Not found", 404)
    db().execute("delete from companies where id=?", (cid,))
    db().commit()
    return jsonify(ok=True)


PERSONAL = ("email", "e-mail", "phone", "mobile", "linkedin", "contact", "person", "whatsapp")


@app.post("/api/companies/import")
@login_required
def import_csv():
    f = request.files.get("file")
    if not f:
        return err("Choose a CSV file first.")
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return err("The file must be a UTF-8 CSV.")
    rd = csv.DictReader(io.StringIO(text))
    heads = {(h or "").strip().lower(): h for h in (rd.fieldnames or [])}
    ignored = [h for h in heads if any(p in h for p in PERSONAL)]
    namek = next((heads[h] for h in ("name", "company", "company name") if h in heads), None)
    if not namek:
        return err("The file needs a column called name or company.")
    u = me_row()
    have = {r["name"].lower() for r in db().execute("select name from companies where user_id=?", (u["id"],))}
    added = skipped = 0
    for i, row in enumerate(rd):
        if i >= 800:
            break
        name = clean(row.get(namek), 120)
        if not name or name.lower() in have:
            skipped += 1
            continue
        t = clean(row.get(heads.get("type", "")), 40).lower()
        ti = next((k for k, v in enumerate(TYPES) if t and (t in v.lower() or v.lower() in t)), 0)
        save_new(u, {"name": name, "type": ti, "city": row.get(heads.get("city", "")),
                     "website": row.get(heads.get("website", "")), "signal": row.get(heads.get("signal", ""))}, "import")
        have.add(name.lower()); added += 1
    db().commit()
    return jsonify(added=added, skipped=skipped, ignoredColumns=ignored)


JUNK_DOMAINS = ("justdial.com", "indiamart.com", "sulekha.com", "tradeindia.com", "exportersindia.com",
                "yellowpages.in", "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "twitter.com",
                "x.com", "wikipedia.org", "quora.com", "reddit.com", "medium.com", "glassdoor.com", "indeed.com",
                "naukri.com", "ambitionbox.com", "zaubacorp.com", "tofler.in", "crunchbase.com", "zoominfo.com",
                "dnb.com", "prospeo.io", "clutch.co", "economictimes.indiatimes.com", "timesofindia.indiatimes.com",
                "thehindu.com", "hindustantimes.com", "livemint.com", "business-standard.com", "moneycontrol.com",
                "ndtv.com", "indianexpress.com", "cxotoday.com", "pinterest.com", "tripadvisor.in", "tripadvisor.com",
                "maps.google.com", "olx.in", "cardekho.com", "carwale.com", "gov.in", "nic.in")
JUNK_TITLE = re.compile(r"\b(top\s*\d+|\d+\s+best|best|list of|news|report|reports|guide|how to|what is|reviews?|"
                        r"jobs|directory|trends|vs\.?|market size|statistics)\b", re.I)
JUNK_PATH = re.compile(r"/(blog|blogs|news|article|articles|press|media|report|reports|insights|post|posts|"
                       r"category|tag|search|jobs|careers)(/|$)|/20\d\d/", re.I)


def looks_like_company_site(url, title):
    """Keep results that look like a business's own website, not news, directories or reports."""
    u = urllib.parse.urlparse(url)
    host = (u.netloc or "").lower().removeprefix("www.")
    if not host or any(host == d or host.endswith("." + d) for d in JUNK_DOMAINS):
        return False
    if JUNK_PATH.search(u.path or "") or JUNK_TITLE.search(title or ""):
        return False
    return True


def company_name(title, host):
    first = re.split(r"\s+[|\-\u2013\u2014:]\s+", (title or "").strip())[0].strip()
    if len(first) < 3 or first.lower() in ("home", "about us", "about", "welcome", "contact us"):
        first = host.split(".")[0].replace("-", " ").title()
    return clean(first, 120)


@app.post("/api/find")
@login_required
def find_companies():
    """Search the public web (Tavily) for possible target companies. Nothing is saved here:
    the user reviews the candidates and adds the ones they want through /api/companies."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return err("Company search is switched off. Set TAVILY_API_KEY to enable it.", 501)
    d = request.get_json(silent=True) or {}
    city, buyer = clean(d.get("city"), 60), clean(d.get("buyer_type"), 60)
    if not city or not buyer:
        return err("Enter a city and choose a buyer type.")
    limit = int(os.environ.get("FIND_DAILY_LIMIT", "10"))
    day = time.strftime("%Y-%m-%d", time.gmtime())
    row = db().execute("select n from find_usage where user_id=? and day=?", (session["uid"], day)).fetchone()
    if row and row["n"] >= limit:
        return err("Daily search limit reached (%d). Try again tomorrow." % limit, 429)
    body = json.dumps({"query": "%s company %s %s official website" % (buyer, city, os.environ.get("FIND_COUNTRY", "India")),
                       "max_results": 20, "search_depth": "basic", "include_answer": False,
                       "exclude_domains": [x for x in JUNK_DOMAINS if x not in ("gov.in", "nic.in")]}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", body, {
        "content-type": "application/json", "authorization": "Bearer " + key})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403):
            return err("The search key was rejected. Check TAVILY_API_KEY.", 502)
        if ex.code in (429, 432, 433):
            return err("The search service is at its limit. Try again shortly.", 429)
        return err("The search failed (%s)." % ex.code, 502)
    except Exception:
        return err("Could not reach the search service. Try again.", 502)
    db().execute("insert into find_usage(user_id,day,n) values(?,?,1) "
                 "on conflict(user_id,day) do update set n=n+1", (session["uid"], day))
    db().commit()
    have = {(urllib.parse.urlparse(r["website"]).netloc or "").lower().removeprefix("www.")
            for r in db().execute("select website from companies where user_id=?", (session["uid"],)) if r["website"]}
    cands, seen = [], set(have)
    for x in data.get("results", []):
        url, title = clean(x.get("url"), 300), clean(x.get("title"), 200)
        if not url.startswith(("http://", "https://")) or not looks_like_company_site(url, title):
            continue
        u = urllib.parse.urlparse(url)
        host = u.netloc.lower().removeprefix("www.")
        if host in seen:
            continue
        seen.add(host)
        cands.append({"name": company_name(title, host), "website": "%s://%s" % (u.scheme, u.netloc), "host": host,
                      "note": clean(x.get("content"), 300)})
        if len(cands) >= 10:
            break
    return jsonify(candidates=cands)


@app.post("/api/companies/<int:cid>/research")
@login_required
def research(cid):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return err("AI research is switched off. Set ANTHROPIC_API_KEY to enable it.", 501)
    row = owned(cid)
    if not row:
        return err("Not found", 404)
    prompt = ("Research this business using public web sources only: %s, %s. Website: %s. "
              "In 3 short bullet points say what it does and how it might use vehicles, then one line starting "
              "'Signal:' about anything recent that suggests it may buy vehicles. Do not include any personal "
              "details about individuals. If you cannot find it, say so." % (row["name"], row["city"] or "India", row["website"] or "unknown"))
    body = json.dumps({"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 800,
                       "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as ex:
        return err("The research request failed: %s" % ex, 502)
    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        return err("No research came back. Try again.", 502)
    note = (row["notes"] + "\n\n" if row["notes"] else "") + "[AI research, please verify]\n" + text
    db().execute("update companies set notes=?, source='ai research' where id=?", (note[:4000], cid))
    db().commit()
    return jsonify(out(owned(cid)))


@app.get("/api/export.<fmt>")
@login_required
def export(fmt):
    rows = db().execute("select * from companies where user_id=? and stage>=4 order by score desc",
                        (session["uid"],)).fetchall()
    if fmt == "json":
        return Response(json.dumps([out(r) for r in rows], indent=2), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=tandem-deals.json"})
    if fmt == "csv":
        b = io.StringIO(); w = csv.writer(b)
        w.writerow(["Company", "Type", "City", "Website", "Score", "Budget", "Authority", "Need", "Timing",
                    "Test drive", "Quote (INR thousand)", "Won"])
        for r in rows:
            w.writerow([r["name"], TYPES[r["type"]], r["city"], r["website"], r["score"], r["q1"], r["q2"],
                        r["q3"], r["q4"], r["test_drive"], r["quote_k"], r["won"]])
        return Response(b.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tandem-deals.csv"})
    return err("Unknown format", 404)


init_db()
if __name__ == "__main__":
    app.run(debug=bool(os.environ.get("DEBUG")), port=int(os.environ.get("PORT", 5000)))
