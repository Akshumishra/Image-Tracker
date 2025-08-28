import os
import sqlite3
import datetime
import shortuuid
import requests
from PIL import Image
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, jsonify, session, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader

# ======================================================
# ------------- Configuration --------------------------
# ======================================================

APP_SECRET = os.environ.get("SECRET_KEY", "dev_secret_change_this")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
DB_FILE = os.path.join("generated", "hits.db")  # persistent in Render volume
OUTDIR = "generated"
os.makedirs(OUTDIR, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = APP_SECRET

# Rate limiting
limiter = Limiter(key_func=get_remote_address, default_limits=["2000/day","200/hour"])
limiter.init_app(app)

# ======================================================
# ------------- Database Helpers -----------------------
# ======================================================

def get_conn():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    # Hits table
    c.execute('''
    CREATE TABLE IF NOT EXISTS hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_ref TEXT,
        user_id TEXT,
        ip TEXT,
        ua TEXT,
        ts TEXT,
        lat REAL,
        lon REAL,
        city TEXT,
        region TEXT,
        country TEXT
    )''')
    # Users table
    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )''')
    conn.commit()
    conn.close()

def insert_hit(doc_ref, user_id, ip, ua, lat=None, lon=None, city=None, region=None, country=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        INSERT INTO hits (doc_ref, user_id, ip, ua, ts, lat, lon, city, region, country)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    ''', (doc_ref,user_id,ip,ua,datetime.datetime.utcnow().isoformat()+"Z",lat,lon,city,region,country))
    conn.commit()
    conn.close()

def fetch_hits(limit=1000):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM hits ORDER BY id DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

init_db()

# ======================================================
# ------------- Geolocation Helper ---------------------
# ======================================================

def geo_ip(ip):
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}?fields=status,message,lat,lon,city,regionName,country", timeout=4).json()
        if r.get("status") == "success":
            return r.get("lat"), r.get("lon"), r.get("city"), r.get("regionName"), r.get("country")
    except:
        pass
    return None, None, None, None, None

# ======================================================
# ------------- PDF Helper -----------------------------
# ======================================================

def create_pdf_with_clickable_image(image_path: str, pdf_path: str, page_size=letter, url: str = None):
    c = canvas.Canvas(pdf_path, pagesize=page_size)
    width, height = page_size
    img = ImageReader(image_path)
    iw, ih = img.getSize()
    margin = 36
    max_w, max_h = width-2*margin, height-2*margin
    scale = min(max_w/iw, max_h/ih)
    draw_w, draw_h = iw*scale, ih*scale
    x = (width-draw_w)/2
    y = (height-draw_h)/2
    c.drawImage(img, x, y, draw_w, draw_h, preserveAspectRatio=True, mask='auto')
    if url:
        c.linkURL(url, (x, y, x+draw_w, y+draw_h), relative=0)
    c.showPage()
    c.save()

# ======================================================
# ------------- Routes ---------------------------------
# ======================================================

@app.route("/")
def index():
    return render_template("index.html", logged_in=session.get("admin", False))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        if request.form.get("password","") == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("make"))
        flash("Wrong password","error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# User registration/login routes with SQLite (same as previous code)

@app.route("/make", methods=["GET","POST"])
def make():
    if not session.get("admin"):
        return redirect(url_for("login"))
    if request.method=="POST":
        mode = request.form.get("mode","png")
        file = request.files.get("image")
        base_image = Image.open(file.stream).convert("RGBA") if file and file.filename else Image.new("RGBA",(800,600),(255,255,255,255))
        doc_ref = shortuuid.uuid()[:8]

        # Save image
        fname = f"document_{doc_ref}.png"
        fpath = os.path.join(OUTDIR,fname)
        base_image.save(fpath,"PNG")

        # Create PDF
        url = url_for('clickable_redirect', doc_ref=doc_ref, _external=True)
        pdf_name = f"document_{doc_ref}.pdf"
        pdf_path = os.path.join(OUTDIR,pdf_name)
        create_pdf_with_clickable_image(fpath,pdf_path,url=url)

        return render_template("made_file.html",
                               doc_ref=doc_ref,
                               file_url=url_for("download_generated", name=fname, _external=True),
                               file_kind="PNG",
                               pdf_url=url_for("download_generated", name=pdf_name, _external=True),
                               dl_pdf_url=url_for("dl_pdf", doc_ref=doc_ref, pdfname=pdf_name, _external=True))
    return render_template("make.html")

@app.route("/click/<doc_ref>")
@limiter.limit("60/minute")
def clickable_redirect(doc_ref):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    ua = request.headers.get("User-Agent","")
    lat,lon,city,region,country = geo_ip(ip)
    user_id = session.get("user_id","anonymous")
    insert_hit(doc_ref,user_id,ip,ua,lat,lon,city,region,country)
    return redirect("https://your-site.com/thank-you")

@app.route("/dl_pdf/<doc_ref>/<pdfname>")
def dl_pdf(doc_ref,pdfname):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    ua = request.headers.get("User-Agent","")
    lat,lon,city,region,country = geo_ip(ip)
    user_id = session.get("user_id","anonymous")
    insert_hit(doc_ref,user_id,ip,ua,lat,lon,city,region,country)
    path = os.path.join(OUTDIR,pdfname)
    if not os.path.exists(path):
        return "Not found",404
    return send_file(path,as_attachment=True,download_name=pdfname,mimetype="application/pdf")

@app.route("/download_generated/<name>")
def download_generated(name):
    path = os.path.join(OUTDIR,name)
    if not os.path.exists(path):
        return "Not found",404
    ext=name.lower().split(".")[-1]
    mime_map={"svg":"image/svg+xml","png":"image/png","pdf":"application/pdf"}
    return send_file(path,as_attachment=True,download_name=name,mimetype=mime_map.get(ext,"application/octet-stream"))

@app.route("/logs")
def logs():
    if not session.get("admin"):
        return redirect(url_for("login"))
    rows = fetch_hits()
    return render_template("logs.html", table_data=rows)

# ======================================================
# ------------- Run App --------------------------------
# ======================================================

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5000)
