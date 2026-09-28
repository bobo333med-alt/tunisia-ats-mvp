from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from pypdf import PdfReader
from docx import Document
import psycopg2
import psycopg2.extras
import io, os, re, uuid, math
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("ATS_API_KEY", "demo-change-me")
app = FastAPI(title="Selecta", version="4.0")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY, title TEXT, description TEXT,
    must_have TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS candidates(
    id TEXT PRIMARY KEY, filename TEXT, name TEXT, email TEXT,
    phone TEXT, location TEXT, skills TEXT, education TEXT,
    experience TEXT, raw_text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS applications(
    id TEXT PRIMARY KEY, job_id TEXT, candidate_id TEXT,
    score REAL, skill_score REAL, semantic_score REAL,
    experience_score REAL, missing TEXT, status TEXT,
    explanation TEXT, created_at TEXT,
    UNIQUE(job_id, candidate_id));
CREATE TABLE IF NOT EXISTS audit(
    id SERIAL PRIMARY KEY, event TEXT, detail TEXT, created_at TEXT);
"""


def db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(SCHEMA)
    conn.commit()
    cur.close()
    return conn


def q_one(conn, sql, params=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def q_all(conn, sql, params=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def q_run(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    cur.close()


def audit(event, detail):
    try:
        conn = db()
        q_run(conn,
              "INSERT INTO audit(event,detail,created_at) VALUES(%s,%s,%s)",
              (event, detail, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clean(s):
    if s is None:
        return None
    return s.strip().replace("\u200b", "").replace("\ufeff", "")


def tokens(s):
    return set(re.findall(r"[\w\u00C0-\u00FF+#.-]{2,}", (s or "").lower()))


def extract_text(name, data):
    ext = os.path.splitext(name.lower())[1]
    if ext == ".pdf":
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages)
    if ext == ".docx":
        d = Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    if ext in (".txt", ".md"):
        return data.decode("utf-8", "ignore")
    raise ValueError("Only PDF, DOCX and TXT are supported.")


def first(pattern, text):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else ""


def parse_candidate(text):
    email = first(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", text)
    phone = first(r"((?:\+?216[ .-]?)?(?:\d[ .-]?){8})", text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    name = lines[0][:120] if lines else "Unknown candidate"
    skills = []
    common = [
        "python", "java", "javascript", "typescript", "react", "flutter",
        "sql", "excel", "word", "powerpoint", "fastapi", "docker", "git",
        "linux", "ai", "machine learning", "recruitment", "sales",
        "marketing", "accounting", "mechanic", "automotive",
        "comptabilite", "fiscalite", "declaration",
        "english", "french", "arabic"
    ]
    low = text.lower()
    for s in common:
        if s in low:
            skills.append(s)
    return name, email, phone, ", ".join(skills), "", ""


def cosine(a, b):
    A = tokens(a)
    B = tokens(b)
    if not A or not B:
        return 0
    return len(A & B) / math.sqrt(len(A) * len(B)) * 100


def match(job, cand):
    jd = job["description"] or ""
    must = tokens(job["must_have"] or "")
    cv = tokens(cand["raw_text"] or "")
    common = must & cv
    skill_score = 100 * len(common) / max(1, len(must)) if must else 100
    semantic = cosine(jd, cand["raw_text"])
    years = [
        int(x) for x in re.findall(
            r"(\d{1,2})\s*(?:years?|ans|\u0633\u0646\u0648\u0627\u062A)",
            cand["raw_text"],
            re.I
        )
    ]
    exp = min(100, max(years) * 10) if years else 50
    score = round(skill_score * .50 + semantic * .35 + exp * .15, 1)
    missing = sorted(must - cv)
    explanation = (
        str(len(common)) + "/" + str(len(must) if must else 0) +
        " must-have skills matched; semantic " +
        str(round(semantic, 1)) + "%; experience " + str(int(exp)) + "%."
    )
    return score, skill_score, semantic, exp, missing, explanation


class Job(BaseModel):
    title: str
    description: str
    must_have: str = ""


def auth(x_api_key):
    if x_api_key is None:
        return
    if clean(x_api_key) != API_KEY:
        raise HTTPException(401, "Invalid API key")


# ============ Lifecycle ============
@app.on_event("startup")
def startup():
    try:
        db().close()
    except Exception:
        pass


# ============ Health ============
@app.api_route("/health", methods=["GET", "HEAD"])
def health(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        html = (
            "<!DOCTYPE html><html lang='ar' dir='rtl'><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Selecta - Health</title>"
            "<style>"
            "*{box-sizing:border-box;margin:0;padding:0}"
            "body{font-family:-apple-system,sans-serif;"
            "background:linear-gradient(135deg,#eff6ff,#bfdbfe);"
            "min-height:100vh;padding:12px;display:flex;"
            "align-items:center;justify-content:center}"
            ".card{background:#fff;border-radius:16px;padding:32px 22px;"
            "width:100%;max-width:480px;text-align:center;"
            "box-shadow:0 12px 40px rgba(30,64,175,.15)}"
            "h1{color:#16a34a;font-size:30px;margin-bottom:8px}"
            ".sub{color:#64748b;margin-bottom:22px;font-size:15px}"
            ".row{display:flex;justify-content:space-between;"
            "padding:14px 16px;background:#f8fafc;border-radius:10px;"
            "margin-bottom:10px;font-size:15px}"
            ".row b{color:#1e40af}"
            ".ok{color:#16a34a}"
            "a{display:block;margin-top:18px;padding:15px;"
            "background:#2563eb;color:#fff;border-radius:10px;"
            "text-decoration:none;font-weight:700;font-size:16px}"
            "</style></head><body><div class='card'>"
            "<h1>&#10004; Selecta</h1>"
            "<p class='sub'>Service en ligne / Service online</p>"
            "<div class='row'><span>Status</span><b class='ok'>OK</b></div>"
            "<div class='row'><span>Version</span><b>4.0</b></div>"
            "<div class='row'><span>Service</span><b>Selecta</b></div>"
            "<a href='/'>&#8592; Accueil / Home</a>"
            "</div></body></html>"
        )
        return HTMLResponse(html)
    return JSONResponse({"status": "ok", "service": "Selecta", "version": "4.0"})


# ============ API: Jobs ============
@app.post("/api/jobs")
def create_job(job: Job, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    jid = str(uuid.uuid4())
    conn = db()
    q_run(conn,
          "INSERT INTO jobs VALUES(%s,%s,%s,%s,%s)",
          (jid, clean(job.title), clean(job.description),
           clean(job.must_have) or "", datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    audit("job_created", jid)
    return {"id": jid, "title": job.title, "must_have": job.must_have}


@app.get("/api/jobs")
def list_jobs(x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn, "SELECT * FROM jobs ORDER BY created_at DESC")
    conn.close()
    return rows


# ============ API: Candidates ============
@app.post("/api/jobs/{job_id}/candidates")
async def candidates(
    job_id: str,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, "SELECT * FROM jobs WHERE id=%s", (job_id,))
    if not job:
        conn.close()
        raise HTTPException(404, "Job not found")

    ranked = []
    for f in files:
        data = await f.read()
        try:
            text = extract_text(f.filename, data)
        except ValueError as e:
            conn.close()
            raise HTTPException(400, str(e))
        except Exception:
            conn.close()
            raise HTTPException(400, "Could not read file: " + f.filename)

        name, email, phone, skills, education, experience = parse_candidate(text)

        existing = None
        if email:
            existing = q_one(conn,
                "SELECT id FROM candidates WHERE email=%s LIMIT 1",
                (email,))
        if not existing:
            existing = q_one(conn,
                "SELECT id FROM candidates WHERE filename=%s LIMIT 1",
                (f.filename,))

        if existing:
            cid = existing["id"]
            q_run(conn,
                "UPDATE candidates SET filename=%s, name=%s, email=%s, "
                "phone=%s, skills=%s, raw_text=%s WHERE id=%s",
                (f.filename, name, email, phone, skills, text, cid))
        else:
            cid = str(uuid.uuid4())
            q_run(conn,
                "INSERT INTO candidates VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (cid, f.filename, name, email, phone, "",
                 skills, education, experience, text,
                 datetime.utcnow().isoformat()))

        cand = {"raw_text": text}
        score, ss, sem, ex, missing, explain = match(job, cand)
        status = (
            "shortlist" if score >= 70
            else "review" if score >= 50
            else "reject"
        )

        app_existing = q_one(conn,
            "SELECT id FROM applications WHERE job_id=%s AND candidate_id=%s",
            (job_id, cid))

        if app_existing:
            q_run(conn,
                "UPDATE applications SET score=%s, skill_score=%s, "
                "semantic_score=%s, experience_score=%s, missing=%s, "
                "status=%s, explanation=%s, created_at=%s WHERE id=%s",
                (score, ss, sem, ex, ",".join(missing), status,
                 explain, datetime.utcnow().isoformat(),
                 app_existing["id"]))
        else:
            aid = str(uuid.uuid4())
            q_run(conn,
                "INSERT INTO applications VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (aid, job_id, cid, score, ss, sem, ex,
                 ",".join(missing), status, explain,
                 datetime.utcnow().isoformat()))

        ranked.append({
            "id": cid,
            "filename": f.filename,
            "name": name,
            "email": email,
            "score": score,
            "skill_score": round(ss, 1),
            "semantic_score": round(sem, 1),
            "experience_score": round(ex, 1),
            "missing": missing,
            "status": status,
            "explanation": explain
        })

    conn.commit()
    conn.close()
    ranked.sort(key=lambda x: x["score"], reverse=True)
    audit("bulk_import", job_id + ":" + str(len(ranked)))
    return {
        "job_id": job_id,
        "count": len(ranked),
        "files_received": [x["filename"] for x in ranked],
        "ranked": ranked
    }


# ============ API: Clear job candidates ============
@app.api_route("/api/jobs/{job_id}/clear", methods=["GET", "DELETE"])
def clear_job(job_id: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    job_id = clean(job_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM applications WHERE job_id=%s", (job_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return {"deleted": deleted, "job_id": job_id, "status": "cleared"}


@app.api_route("/api/admin/clear-all", methods=["GET", "DELETE"])
def clear_all(x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM applications")
    a = cur.rowcount
    cur.execute("DELETE FROM candidates")
    c = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return {"applications_deleted": a, "candidates_deleted": c, "status": "cleared"}


# ============ API: Results ============
@app.get("/api/jobs/{job_id}/results")
def results(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    job_id = clean(job_id)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        key = x_api_key or ""
        return RedirectResponse(
            url="/results/" + job_id + "?x_api_key=" + key,
            status_code=307
        )

    conn = db()
    rows = q_all(conn,
                 "SELECT a.*, c.name, c.email, c.filename "
                 "FROM applications a "
                 "JOIN candidates c ON c.id = a.candidate_id "
                 "WHERE a.job_id = %s ORDER BY a.score DESC",
                 (job_id,))
    conn.close()
    return rows


@app.get("/api/candidates/search")
def search(q: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn,
                 "SELECT id,name,email,filename,skills FROM candidates "
                 "WHERE raw_text LIKE %s LIMIT 100",
                 ("%" + q + "%",))
    conn.close()
    return rows


# ============ API: CSV Export ============
@app.get("/api/jobs/{job_id}/export.csv")
def export_csv(job_id: str, x_api_key: str | None = None):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, "SELECT title FROM jobs WHERE id=%s", (job_id,))
    if not job:
        conn.close()
        raise HTTPException(404, "Job not found")

    rows = q_all(conn,
                 "SELECT a.score, a.status, a.skill_score, a.semantic_score, "
                 "a.experience_score, a.missing, c.name, c.email, c.phone, "
                 "c.filename FROM applications a "
                 "JOIN candidates c ON c.id = a.candidate_id "
                 "WHERE a.job_id = %s ORDER BY a.score DESC",
                 (job_id,))
    conn.close()

    def esc(v):
        if v is None:
            return ""
        s = str(v).replace('"', '""')
        if "," in s or '"' in s or "\n" in s:
            return '"' + s + '"'
        return s

    lines = ["Rank,Name,Email,Phone,File,Score,Skills,Semantic,Experience,Status,Missing"]
    for i, r in enumerate(rows, 1):
        lines.append(",".join([
            str(i), esc(r["name"]), esc(r["email"]), esc(r["phone"]),
            esc(r["filename"]), str(r["score"] or 0),
            str(r["skill_score"] or 0), str(r["semantic_score"] or 0),
            str(r["experience_score"] or 0), esc(r["status"]),
            esc(r["missing"])
        ]))

    csv = "\ufeff" + "\n".join(lines)
    return HTMLResponse(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename=selecta_" + job_id[:8] + ".csv"}
    )


# ============ Shared CSS ============
BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html{width:100%;min-height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 50%,#bfdbfe 100%);
  color:#111;min-height:100vh;width:100%;overflow-x:hidden;
  padding:4px;
  padding-top:max(4px,env(safe-area-inset-top,4px));
  padding-bottom:max(4px,env(safe-area-inset-bottom,4px));
  -webkit-text-size-adjust:100%;
}
.wrap{width:100%;max-width:100%;margin:0;padding:0}
.card{
  background:#fff;border-radius:12px;padding:18px 14px;width:100%;
  margin-bottom:8px;
  box-shadow:0 4px 20px rgba(30,64,175,.1);
}
h1{font-size:26px;color:#1e40af;text-align:center;margin-bottom:4px;font-weight:900}
.subtitle{text-align:center;color:#64748b;font-size:14px;
  margin-bottom:18px;font-weight:600}
.lang-switch{display:flex;justify-content:center;gap:5px;
  margin-bottom:14px;flex-wrap:wrap}
.lang-switch button{
  padding:8px 12px;border:2px solid #e2e8f0;background:#f8fafc;
  color:#64748b;border-radius:20px;font-size:12px;font-weight:700;
  cursor:pointer;font-family:inherit;transition:all .15s;
  -webkit-tap-highlight-color:transparent;
}
.lang-switch button.active{background:#2563eb;color:#fff;border-color:#2563eb}
.lang-switch button:active{transform:scale(.95)}
.lang-ar,.lang-fr,.lang-en{display:none}
body[data-lang="ar"] .lang-ar{display:block}
body[data-lang="fr"] .lang-fr{display:block}
body[data-lang="en"] .lang-en{display:block}
body[data-lang="ar"] .lang-ar.inline,
body[data-lang="fr"] .lang-fr.inline,
body[data-lang="en"] .lang-en.inline{display:inline-block}
body[data-lang="ar"]{direction:rtl;text-align:right}
body[data-lang="fr"],body[data-lang="en"]{direction:ltr;text-align:left}
.field{display:block;font-weight:700;font-size:15px;color:#374151;
  margin:14px 0 6px}
input[type=text],input[type=password]{
  width:100%;padding:14px;font-size:16px;border:2px solid #e2e8f0;
  border-radius:11px;background:#f8fafc;font-family:inherit;
  transition:border .15s;
}
input[type=text]:focus,input[type=password]:focus{
  outline:none;border-color:#3b82f6;background:#fff;
}
.file-btn{
  display:flex;align-items:center;justify-content:center;
  padding:22px 14px;background:#f0f9ff;
  border:2px dashed #60a5fa;border-radius:12px;
  font-size:17px;font-weight:800;color:#1e40af;cursor:pointer;
  user-select:none;transition:background .15s;
  -webkit-tap-highlight-color:transparent;
}
.file-btn:active{background:#dbeafe}
input[type=file]{display:none}
.hint{font-size:13px;color:#64748b;text-align:center;margin-top:8px}
.file-list{margin-top:12px;padding:12px;background:#f0fdf4;
  border:2px solid #86efac;border-radius:11px;display:none}
.file-list.show{display:block}
.file-list .title{font-size:13px;color:#166534;font-weight:800;
  margin-bottom:9px}
.file-item{display:flex;justify-content:space-between;align-items:center;
  padding:10px 12px;background:#fff;border-radius:8px;margin-bottom:6px;
  font-size:14px;direction:ltr}
.file-item:last-child{margin-bottom:0}
.file-item .name{flex:1;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:#1e293b}
.file-item .remove{color:#dc2626;font-weight:800;cursor:pointer;
  padding:0 10px;font-size:20px;line-height:1}
button.main{width:100%;padding:18px;font-size:17px;
  background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff;
  border:none;border-radius:12px;font-weight:800;margin-top:14px;
  cursor:pointer;transition:transform .1s;font-family:inherit;
  box-shadow:0 4px 14px rgba(37,99,235,.3);
  -webkit-tap-highlight-color:transparent;
}
button.main:active{transform:scale(.98)}
button.main:disabled{background:#94a3b8;cursor:not-allowed;box-shadow:none}
button.danger{width:100%;padding:14px;font-size:15px;
  background:linear-gradient(135deg,#dc2626,#b91c1c);color:#fff;
  border:none;border-radius:12px;font-weight:800;margin-top:10px;
  cursor:pointer;font-family:inherit;
  -webkit-tap-highlight-color:transparent;
}
button.danger:active{transform:scale(.98)}
pre{background:#0f172a;color:#10b981;padding:12px;border-radius:10px;
  font-size:12px;overflow-x:auto;max-height:280px;white-space:pre-wrap;
  word-break:break-all;margin-top:6px;direction:ltr;text-align:left;
  font-family:monospace}
ul.menu{list-style:none}
ul.menu li{margin-bottom:10px}
ul.menu li a{display:flex;align-items:center;justify-content:space-between;
  padding:18px 20px;background:#f8fafc;border:2px solid #e2e8f0;
  border-radius:12px;text-decoration:none;color:#1e293b;font-weight:700;
  font-size:17px;transition:all .15s;
  -webkit-tap-highlight-color:transparent;
}
ul.menu li a:active{background:#dbeafe;border-color:#3b82f6;
  transform:scale(.98)}
ul.menu li a.primary{background:linear-gradient(135deg,#2563eb,#1d4ed8);
  border:none;color:#fff;font-size:18px;
  box-shadow:0 4px 14px rgba(37,99,235,.35)}
ul.menu li a.primary:active{background:#1e40af}
ul.menu .icon{font-size:24px}
.status{text-align:center;color:#059669;font-weight:700;font-size:15px;
  margin-bottom:20px;display:flex;align-items:center;
  justify-content:center;gap:6px}
.status::before{content:'';width:8px;height:8px;background:#10b981;
  border-radius:50%;box-shadow:0 0 0 3px rgba(16,185,129,.2)}
.r-header{background:#fff;border-radius:12px;padding:18px 14px;
  margin-bottom:8px;box-shadow:0 4px 20px rgba(30,64,175,.1)}
.r-header h1{font-size:22px;margin-bottom:10px}
.job-title{font-size:17px;color:#0f172a;font-weight:800;
  text-align:center;margin-bottom:6px;word-break:break-word}
.must-have{font-size:13px;color:#64748b;text-align:center;
  margin-bottom:16px;word-break:break-word}
.stats{display:flex;gap:7px}
.stat{flex:1;text-align:center;padding:12px 6px;border-radius:11px;
  font-size:13px;font-weight:800}
.stat .num{display:block;font-size:24px;margin-bottom:2px}
.stat.green{background:#dcfce7;color:#166534}
.stat.yellow{background:#fef3c7;color:#92400e}
.stat.red{background:#fee2e2;color:#991b1b}
.r-card{background:#fff;border-radius:12px;padding:16px;margin-bottom:8px;
  position:relative;box-shadow:0 2px 10px rgba(0,0,0,.05)}
.r-rank{position:absolute;top:12px;left:12px;color:#fff;font-weight:800;
  font-size:13px;padding:4px 12px;border-radius:20px;z-index:1}
[dir="rtl"] .r-rank{left:auto;right:12px}
.r-name{font-size:17px;font-weight:800;color:#0f172a;margin-bottom:8px;
  padding-left:54px}
[dir="rtl"] .r-name{padding-left:0;padding-right:54px}
.r-score{font-size:30px;font-weight:900}
.r-score-label{font-size:13px;color:#94a3b8;margin-left:4px}
[dir="rtl"] .r-score-label{margin-left:0;margin-right:4px}
.r-status{font-size:15px;font-weight:800;margin-bottom:8px}
.r-meta{font-size:14px;color:#475569;margin-top:4px;
  word-break:break-all;direction:ltr;text-align:left}
.r-missing{font-size:14px;color:#991b1b;margin-top:8px;padding:10px 12px;
  background:#fff;border-radius:8px;border-left:3px solid #dc2626}
[dir="rtl"] .r-missing{border-left:none;border-right:3px solid #dc2626}
.r-missing.ok{color:#166534}
[dir="rtl"] .r-missing.ok{border-right-color:#16a34a}
[dir="ltr"] .r-missing.ok{border-left-color:#16a34a}
.empty{text-align:center;padding:50px 18px;color:#64748b;background:#fff;
  border-radius:12px;font-size:16px}
.actions{display:flex;gap:9px;margin-top:14px;flex-wrap:wrap}
.btn{flex:1;min-width:140px;text-align:center;padding:16px 18px;
  border-radius:12px;text-decoration:none;font-weight:800;font-size:16px;
  transition:transform .1s;-webkit-tap-highlight-color:transparent}
.btn:active{transform:scale(.97)}
.btn.green{background:linear-gradient(135deg,#16a34a,#15803d);color:#fff}
.btn.blue{background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff}
.btn.red{background:linear-gradient(135deg,#dc2626,#b91c1c);color:#fff}
"""


LANG_JS = """
function setLang(l){
  document.body.setAttribute('data-lang', l);
  document.documentElement.setAttribute('lang', l);
  document.documentElement.setAttribute('dir', l==='ar'?'rtl':'ltr');
  try{ localStorage.setItem('lang', l); }catch(e){}
  var btns = document.querySelectorAll('.lang-switch button');
  for(var i=0;i<btns.length;i++){
    btns[i].classList.toggle('active',
      btns[i].getAttribute('data-lang') === l);
  }
}
(function(){
  var saved = 'ar';
  try{ saved = localStorage.getItem('lang') || 'ar'; }catch(e){}
  setLang(saved);
})();
"""


LANG_SWITCH = (
    '<div class="lang-switch">'
    '<button data-lang="ar" onclick="setLang(\'ar\')">&#127481;&#127475; &#1593;&#1585;&#1576;&#1610;</button>'
    '<button data-lang="fr" onclick="setLang(\'fr\')">&#127467;&#127479; Fran&ccedil;ais</button>'
    '<button data-lang="en" onclick="setLang(\'en\')">&#127468;&#127463; English</button>'
    '</div>'
)


def page_wrap(title, body, extra_js=""):
    return (
        '<!DOCTYPE html>\n'
        '<html lang="ar" dir="rtl">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="theme-color" content="#2563eb">\n'
        '<title>' + title + '</title>\n'
        '<style>\n' + BASE_CSS + '\n</style>\n'
        '</head>\n'
        '<body data-lang="ar">\n'
        '<div class="wrap">\n' + body + '\n</div>\n'
        '<script>\n' + LANG_JS + '\n' + extra_js + '\n</script>\n'
        '</body>\n'
        '</html>'
    )


# ============ Page: Home ============
@app.get("/", response_class=HTMLResponse)
def root():
    body = LANG_SWITCH + (
        '<div class="card">'
        '<h1>&#127919; Selecta</h1>'
        '<p class="subtitle">'
        '<span class="lang-ar inline">&#1601;&#1585;&#1586; &#1584;&#1603;&#1610; &#1604;&#1604;&#1587;&#1610;&#1585; &#1575;&#1604;&#1584;&#1575;&#1578;&#1610;&#1577;</span>'
        '<span class="lang-fr inline">Tri intelligent de CV</span>'
        '<span class="lang-en inline">Smart CV Ranking</span>'
        '</p>'
        '<p class="status">'
        '<span class="lang-ar inline">&#1575;&#1604;&#1582;&#1583;&#1605;&#1577; &#1578;&#1593;&#1605;&#1604;</span>'
        '<span class="lang-fr inline">Service en ligne</span>'
        '<span class="lang-en inline">Service online</span>'
        '</p>'
        '<ul class="menu">'
        '<li><a class="primary" href="/upload">'
        '<span class="lang-ar inline">&#1585;&#1601;&#1593; &#1575;&#1604;&#1587;&#1610;&#1585; &#1575;&#1604;&#1584;&#1575;&#1578;&#1610;&#1577;</span>'
        '<span class="lang-fr inline">T&eacute;l&eacute;verser des CV</span>'
        '<span class="lang-en inline">Upload CVs</span>'
        '<span class="icon">&#128196;</span>'
        '</a></li>'
        '<li><a href="/docs">'
        '<span class="lang-ar inline">&#1608;&#1575;&#1580;&#1607;&#1577; API</span>'
        '<span class="lang-fr inline">API (Swagger)</span>'
        '<span class="lang-en inline">API (Swagger)</span>'
        '<span class="icon">&#128218;</span>'
        '</a></li>'
        '<li><a href="/health">'
        '<span class="lang-ar inline">&#1601;&#1581;&#1589; &#1575;&#1604;&#1581;&#1575;&#1604;&#1577;</span>'
        '<span class="lang-fr inline">&Eacute;tat du service</span>'
        '<span class="lang-en inline">Health check</span>'
        '<span class="icon">&#128154;</span>'
        '</a></li>'
        '</ul>'
        '</div>'
    )
    return HTMLResponse(page_wrap("Selecta", body))


# ============ Page: Upload ============
@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    body = LANG_SWITCH + (
        '<div class="card">'
        '<h1>&#127919; Selecta</h1>'
        '<p class="subtitle">'
        '<span class="lang-ar inline">&#1585;&#1601;&#1593; &#1575;&#1604;&#1587;&#1610;&#1585; &#1575;&#1604;&#1584;&#1575;&#1578;&#1610;&#1577;</span>'
        '<span class="lang-fr inline">T&eacute;l&eacute;verser des CV</span>'
        '<span class="lang-en inline">Upload CVs</span>'
        '</p>'

        '<label class="field">'
        '<span class="lang-ar inline">&#1605;&#1593;&#1585;&#1617;&#1601; &#1575;&#1604;&#1608;&#1592;&#1610;&#1601;&#1577;</span>'
        '<span class="lang-fr inline">Identifiant de loffre</span>'
        '<span class="lang-en inline">Job ID</span>'
        '</label>'
        '<input id="job_id" type="text" placeholder="c9d62982-..." autocomplete="off" autocapitalize="off" spellcheck="false">'

        '<label class="field">'
        '<span class="lang-ar inline">&#1605;&#1601;&#1578;&#1575;&#1581; API</span>'
        '<span class="lang-fr inline">Cl&eacute; API</span>'
        '<span class="lang-en inline">API Key</span>'
        '</label>'
        '<input id="api_key" type="password" placeholder="..." autocomplete="off">'

        '<label class="field">'
        '<span class="lang-ar inline">&#1605;&#1604;&#1601;&#1575;&#1578; &#1575;&#1604;&#1587;&#1610;&#1585; &#1575;&#1604;&#1584;&#1575;&#1578;&#1610;&#1577; (PDF, DOCX, TXT)</span>'
        '<span class="lang-fr inline">Fichiers CV (PDF, DOCX, TXT)</span>'
        '<span class="lang-en inline">CV files (PDF, DOCX, TXT)</span>'
        '</label>'
        '<label for="filePicker" class="file-btn">'
        '<span class="lang-ar inline">&#128206; &#1575;&#1590;&#1594;&#1591; &#1604;&#1575;&#1582;&#1578;&#1610;&#1575;&#1585; &#1605;&#1604;&#1601;</span>'
        '<span class="lang-fr inline">&#128206; Choisir un fichier</span>'
        '<span class="lang-en inline">&#128206; Choose a file</span>'
        '</label>'
        '<input id="filePicker" type="file">'
        '<p class="hint">'
        '<span class="lang-ar inline">&#1610;&#1605;&#1603;&#1606;&#1603; &#1573;&#1590;&#1575;&#1601;&#1577; &#1593;&#1583;&#1577; &#1605;&#1604;&#1601;&#1575;&#1578;</span>'
        '<span class="lang-fr inline">Plusieurs fichiers possibles</span>'
        '<span class="lang-en inline">Multiple files allowed</span>'
        '</p>'

        '<div class="file-list" id="fileList">'
        '<div class="title">'
        '<span class="lang-ar inline">&#9989; &#1575;&#1604;&#1605;&#1604;&#1601;&#1575;&#1578; &#1575;&#1604;&#1605;&#1582;&#1578;&#1575;&#1585;&#1577; (<span id="cnt-ar">0</span>)</span>'
        '<span class="lang-fr inline">&#9989; Fichiers (<span id="cnt-fr">0</span>)</span>'
        '<span class="lang-en inline">&#9989; Files (<span id="cnt-en">0</span>)</span>'
        '</div>'
        '<div id="items"></div>'
        '</div>'

        '<button class="main" id="uploadBtn" onclick="doUpload()">'
        '<span class="lang-ar inline">&#128640; &#1575;&#1585;&#1601;&#1593; &#1608;&#1602;&#1610;&#1617;&#1605;</span>'
        '<span class="lang-fr inline">&#128640; T&eacute;l&eacute;verser</span>'
        '<span class="lang-en inline">&#128640; Upload and rank</span>'
        '</button>'

        '<button class="danger" id="clearBtn" onclick="doClear()">'
        '<span class="lang-ar inline">&#128465; &#1605;&#1587;&#1581; &#1575;&#1604;&#1606;&#1578;&#1575;&#1574;&#1580; &#1575;&#1604;&#1602;&#1583;&#1610;&#1605;&#1577;</span>'
        '<span class="lang-fr inline">&#128465; Effacer les anciens r&eacute;sultats</span>'
        '<span class="lang-en inline">&#128465; Clear old results</span>'
        '</button>'

        '<label class="field">'
        '<span class="lang-ar inline">&#1575;&#1604;&#1606;&#1578;&#1610;&#1580;&#1577;</span>'
        '<span class="lang-fr inline">R&eacute;sultat</span>'
        '<span class="lang-en inline">Result</span>'
        '</label>'
        '<pre id="result">&mdash;</pre>'
        '</div>'
    )

    extra_js = """
var pickedFiles = [];
var picker = document.getElementById('filePicker');
var fileListBox = document.getElementById('fileList');
var itemsBox = document.getElementById('items');

function updateCount(){
  var n = pickedFiles.length;
  var a=document.getElementById('cnt-ar');
  var f=document.getElementById('cnt-fr');
  var e=document.getElementById('cnt-en');
  if(a)a.textContent=n;
  if(f)f.textContent=n;
  if(e)e.textContent=n;
}

function renderList(){
  itemsBox.innerHTML = '';
  updateCount();
  if(pickedFiles.length === 0){
    fileListBox.classList.remove('show');
    return;
  }
  fileListBox.classList.add('show');
  for(var i=0;i<pickedFiles.length;i++){
    (function(idx){
      var f = pickedFiles[idx];
      var row = document.createElement('div');
      row.className = 'file-item';
      var nm = document.createElement('span');
      nm.className = 'name';
      nm.textContent = (idx+1) + '. ' + f.name;
      var rm = document.createElement('span');
      rm.className = 'remove';
      rm.textContent = '\\u2715';
      rm.onclick = function(){ pickedFiles.splice(idx, 1); renderList(); };
      row.appendChild(nm);
      row.appendChild(rm);
      itemsBox.appendChild(row);
    })(i);
  }
}

picker.addEventListener('change', function(){
  if(!picker.files.length) return;
  for(var i=0;i<picker.files.length;i++){
    var f = picker.files[i];
    var exists = false;
    for(var j=0;j<pickedFiles.length;j++){
      if(pickedFiles[j].name === f.name &&
         pickedFiles[j].size === f.size){
        exists = true;
        break;
      }
    }
    if(!exists) pickedFiles.push(f);
  }
  picker.value = '';
  renderList();
});

function cleanInput(s){
  return (s || '').replace(/[\\u200B-\\u200D\\uFEFF]/g, '').trim();
}

function msg(key){
  var L = document.body.getAttribute('data-lang') || 'ar';
  var M = {
    fill: {ar:'\\u0627\\u0645\\u0644\\u0623 \\u0627\\u0644\\u062d\\u0642\\u0648\\u0644',
           fr:'Remplissez les champs',
           en:'Fill all fields'},
    pick: {ar:'\\u0627\\u062e\\u062a\\u0631 \\u0645\\u0644\\u0641\\u0627',
           fr:'Choisissez un fichier',
           en:'Choose a file'},
    up:   {ar:'\\u062c\\u0627\\u0631\\u064d \\u0627\\u0644\\u0631\\u0641\\u0639 ',
           fr:'Televersement ',
           en:'Uploading '},
    up2:  {ar:' \\u0645\\u0644\\u0641...',
           fr:' fichier(s)...',
           en:' file(s)...'},
    ok:   {ar:'\\u062a\\u0645 \\u0631\\u0641\\u0639 ',
           fr:'',
           en:''},
    ok2:  {ar:' \\u0645\\u0644\\u0641. \\u062c\\u0627\\u0631\\u064d \\u0627\\u0644\\u062a\\u062d\\u0648\\u064a\\u0644...',
           fr:' fichier(s). Redirection...',
           en:' file(s). Redirecting...'},
    err:  {ar:'\\u062e\\u0637\\u0623: ',
           fr:'Erreur: ',
           en:'Error: '},
    cleared: {ar:'\\u062a\\u0645 \\u0627\\u0644\\u0645\\u0633\\u062d',
              fr:'Efface',
              en:'Cleared'}
  };
  return (M[key][L] || M[key].ar);
}

async function doUpload(){
  var jid = cleanInput(document.getElementById('job_id').value);
  var key = cleanInput(document.getElementById('api_key').value);
  var out = document.getElementById('result');
  var btn = document.getElementById('uploadBtn');

  if(!jid || !key){ out.textContent = msg('fill'); return; }
  if(pickedFiles.length === 0){ out.textContent = msg('pick'); return; }

  var fd = new FormData();
  for(var i=0;i<pickedFiles.length;i++){
    fd.append('files', pickedFiles[i]);
  }

  btn.disabled = true;
  out.textContent = msg('up') + pickedFiles.length + msg('up2');

  try{
    var r = await fetch(
      '/api/jobs/' + encodeURIComponent(jid) + '/candidates',
      {method:'POST', headers:{'x-api-key': key}, body: fd}
    );
    var d = await r.json();
    if(d.count > 0){
      out.textContent = msg('ok') + d.count + msg('ok2');
      setTimeout(function(){
        window.location.href = '/results/' + encodeURIComponent(jid) +
                               '?x_api_key=' + encodeURIComponent(key);
      }, 1200);
    } else {
      out.textContent = JSON.stringify(d, null, 2);
      btn.disabled = false;
    }
  }catch(e){
    out.textContent = msg('err') + e.message;
    btn.disabled = false;
  }
}

async function doClear(){
  var jid = cleanInput(document.getElementById('job_id').value);
  var key = cleanInput(document.getElementById('api_key').value);
  var out = document.getElementById('result');
  if(!jid || !key){ out.textContent = msg('fill'); return; }
  if(!confirm('Delete all results for this job?\\n\\n' + jid)) return;
  out.textContent = '...';
  try{
    var r = await fetch(
      '/api/jobs/' + encodeURIComponent(jid) + '/clear?x_api_key=' +
      encodeURIComponent(key),
      {method: 'GET'}
    );
    var d = await r.json();
    out.textContent = JSON.stringify(d, null, 2);
  }catch(e){
    out.textContent = msg('err') + e.message;
  }
}
"""
    return HTMLResponse(page_wrap("Selecta - Upload", body, extra_js))


# ============ Page: Results ============
@app.get("/results/{job_id}", response_class=HTMLResponse)
def results_page(job_id: str, x_api_key: str | None = None):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, "SELECT * FROM jobs WHERE id=%s", (job_id,))
    if not job:
        conn.close()
        body = (
            '<div class="card" style="text-align:center">'
            '<h1 style="color:#dc2626">&#10060;</h1>'
            '<p class="subtitle">'
            '<span class="lang-ar inline">&#1575;&#1604;&#1608;&#1592;&#1610;&#1601;&#1577; &#1594;&#1610;&#1585; &#1605;&#1608;&#1580;&#1608;&#1583;&#1577;</span>'
            '<span class="lang-fr inline">Offre introuvable</span>'
            '<span class="lang-en inline">Job not found</span>'
            '</p>'
            '<a class="btn-back" href="/upload" style="display:block;'
            'text-align:center;margin-top:16px;padding:14px;'
            'background:#2563eb;color:#fff;border-radius:12px;'
            'text-decoration:none;font-weight:800;font-size:16px">'
            '<span class="lang-ar inline">&#8592; &#1575;&#1604;&#1593;&#1608;&#1583;&#1577;</span>'
            '<span class="lang-fr inline">&#8592; Retour</span>'
            '<span class="lang-en inline">&#8592; Back</span>'
            '</a>'
            '</div>'
        )
        return HTMLResponse(page_wrap("Not found", LANG_SWITCH + body),
                            status_code=404)

    rows = q_all(conn,
                 "SELECT a.score, a.status, a.missing, "
                 "c.name, c.email, c.filename "
                 "FROM applications a "
                 "JOIN candidates c ON c.id = a.candidate_id "
                 "WHERE a.job_id = %s ORDER BY a.score DESC",
                 (job_id,))
    conn.close()

    rows_html = ""
    shortlist = review = reject = 0

    for i, r in enumerate(rows, 1):
        score = r["score"] or 0
        status = r["status"] or "review"

        if status == "shortlist":
            shortlist += 1
            color = "#16a34a"
            bg = "#f0fdf4"
            icon = "&#9989;"
        elif status == "review":
            review += 1
            color = "#f59e0b"
            bg = "#fffbeb"
            icon = "&#128993;"
        else:
            reject += 1
            color = "#dc2626"
            bg = "#fef2f2"
            icon = "&#10060;"

        missing = (r["missing"] or "").strip()
        if missing:
            miss_inner = (
                '<span class="lang-ar inline">&#9888; &#1606;&#1575;&#1602;&#1589;: '
                + missing.replace(",", "\u060C ") + '</span>'
                '<span class="lang-fr inline">&#9888; Manquant: '
                + missing.replace(",", ", ") + '</span>'
                '<span class="lang-en inline">&#9888; Missing: '
                + missing.replace(",", ", ") + '</span>'
            )
            miss_class = "r-missing"
        else:
            miss_inner = (
                '<span class="lang-ar inline">&#10003; &#1580;&#1605;&#1610;&#1593; &#1575;&#1604;&#1605;&#1607;&#1575;&#1585;&#1575;&#1578; &#1605;&#1608;&#1580;&#1608;&#1583;&#1577;</span>'
                '<span class="lang-fr inline">&#10003; Toutes comp&eacute;tences pr&eacute;sentes</span>'
                '<span class="lang-en inline">&#10003; All skills present</span>'
            )
            miss_class = "r-missing ok"

        if status == "shortlist":
            st_html = (
                '<span class="lang-ar inline">&#1605;&#1572;&#1607;&#1604;</span>'
                '<span class="lang-fr inline">Qualifi&eacute;</span>'
                '<span class="lang-en inline">Qualified</span>'
            )
        elif status == "review":
            st_html = (
                '<span class="lang-ar inline">&#1610;&#1581;&#1578;&#1575;&#1580; &#1605;&#1585;&#1575;&#1580;&#1593;&#1577;</span>'
                '<span class="lang-fr inline">&Agrave; examiner</span>'
                '<span class="lang-en inline">Needs review</span>'
            )
        else:
            st_html = (
                '<span class="lang-ar inline">&#1605;&#1585;&#1601;&#1608;&#1590;</span>'
                '<span class="lang-fr inline">Rejet&eacute;</span>'
                '<span class="lang-en inline">Rejected</span>'
            )

        nm = r["name"] or "?"
        em = r["email"] or "&mdash;"
        fn = r["filename"] or "&mdash;"

        rows_html += (
            '<div class="r-card" style="background:' + bg + ';'
            'border-right:6px solid ' + color + '">'
            '<div class="r-rank" style="background:' + color + '">#'
            + str(i) + '</div>'
            '<div class="r-name">' + icon + ' ' + nm + '</div>'
            '<div><span class="r-score" style="color:' + color + '">'
            + str(score) + '</span>'
            '<span class="r-score-label">/ 100</span></div>'
            '<div class="r-status" style="color:' + color + '">'
            + st_html + '</div>'
            '<div class="r-meta">&#128231; ' + em + '</div>'
            '<div class="r-meta">&#128196; ' + fn + '</div>'
            '<div class="' + miss_class + '">' + miss_inner + '</div>'
            '</div>'
        )

    if not rows:
        rows_html = (
            '<div class="empty">'
            '<span class="lang-ar inline">&#1604;&#1575; &#1610;&#1608;&#1580;&#1583; &#1605;&#1585;&#1588;&#1581;&#1608;&#1606;</span>'
            '<span class="lang-fr inline">Aucun candidat</span>'
            '<span class="lang-en inline">No candidates yet</span>'
            '</div>'
        )

    title = job["title"] or "&mdash;"
    must = job["must_have"] or "&mdash;"
    key_q = x_api_key or ""

    header = (
        '<div class="r-header">'
        + LANG_SWITCH +
        '<h1>'
        '<span class="lang-ar inline">&#128202; &#1606;&#1578;&#1575;&#1574;&#1580; &#1575;&#1604;&#1601;&#1585;&#1586;</span>'
        '<span class="lang-fr inline">&#128202; R&eacute;sultats du tri</span>'
        '<span class="lang-en inline">&#128202; Ranking results</span>'
        '</h1>'
        '<div class="job-title">&#128188; ' + title + '</div>'
        '<div class="must-have">&#127919; ' + must + '</div>'
        '<div class="stats">'
        '<div class="stat green"><span class="num">' + str(shortlist)
        + '</span>'
        '<span class="lang-ar inline">&#1605;&#1572;&#1607;&#1604;</span>'
        '<span class="lang-fr inline">Qualifi&eacute;s</span>'
        '<span class="lang-en inline">Qualified</span></div>'
        '<div class="stat yellow"><span class="num">' + str(review)
        + '</span>'
        '<span class="lang-ar inline">&#1605;&#1585;&#1575;&#1580;&#1593;&#1577;</span>'
        '<span class="lang-fr inline">&Agrave; revoir</span>'
        '<span class="lang-en inline">Review</span></div>'
        '<div class="stat red"><span class="num">' + str(reject)
        + '</span>'
        '<span class="lang-ar inline">&#1605;&#1585;&#1601;&#1608;&#1590;</span>'
        '<span class="lang-fr inline">Rejet&eacute;s</span>'
        '<span class="lang-en inline">Rejected</span></div>'
        '</div></div>'
    )

    actions = (
        '<div class="actions">'
        '<a class="btn green" href="/api/jobs/' + job_id
        + '/export.csv?x_api_key=' + key_q + '">'
        '<span class="lang-ar inline">&#128229; Excel</span>'
        '<span class="lang-fr inline">&#128229; Excel</span>'
        '<span class="lang-en inline">&#128229; Excel</span>'
        '</a>'
        '<a class="btn blue" href="/upload">'
        '<span class="lang-ar inline">&#128260; &#1605;&#1604;&#1601;&#1575;&#1578; &#1580;&#1583;&#1610;&#1583;&#1577;</span>'
        '<span class="lang-fr inline">&#128260; Nouveaux</span>'
        '<span class="lang-en inline">&#128260; New files</span>'
        '</a>'
        '<a class="btn red" href="/api/jobs/' + job_id
        + '/clear?x_api_key=' + key_q + '" '
        'onclick="return confirm(\'Delete all results?\')">'
        '<span class="lang-ar inline">&#128465; &#1605;&#1587;&#1581; &#1575;&#1604;&#1603;&#1604;</span>'
        '<span class="lang-fr inline">&#128465; Tout effacer</span>'
        '<span class="lang-en inline">&#128465; Clear all</span>'
        '</a>'
        '</div>'
    )

    body = header + rows_html + actions
    return HTMLResponse(page_wrap("Selecta - Results", body))


# ============ OpenAPI fix ============
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    for comp in schema.get("components", {}).get("schemas", {}).values():
        for prop in comp.get("properties", {}).values():
            if prop.get("contentMediaType") == "application/octet-stream":
                del prop["contentMediaType"]
                prop["format"] = "binary"
            items = prop.get("items", {})
            if items.get("contentMediaType") == "application/octet-stream":
                del items["contentMediaType"]
                items["format"] = "binary"
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
