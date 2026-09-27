from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from pypdf import PdfReader
from docx import Document
import psycopg2
import psycopg2.extras
import io, os, re, uuid, math
from datetime import datetime

DATABASE_URL = os.getenv('DATABASE_URL')
API_KEY = os.getenv('ATS_API_KEY', 'demo-change-me')
app = FastAPI(title='HireRank', version='2.0')

SCHEMA = '''
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
'''


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
              'INSERT INTO audit(event,detail,created_at) VALUES(%s,%s,%s)',
              (event, detail, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def clean(s):
    if s is None:
        return None
    return s.strip().replace('\u200b', '').replace('\ufeff', '')


def tokens(s):
    return set(re.findall(r'[\w\u00C0-\u00FF+#.-]{2,}', (s or '').lower()))


def extract_text(name, data):
    ext = os.path.splitext(name.lower())[1]
    if ext == '.pdf':
        r = PdfReader(io.BytesIO(data))
        return '\n'.join((p.extract_text() or '') for p in r.pages)
    if ext == '.docx':
        d = Document(io.BytesIO(data))
        return '\n'.join(p.text for p in d.paragraphs)
    if ext in ('.txt', '.md'):
        return data.decode('utf-8', 'ignore')
    raise ValueError('Only PDF, DOCX and TXT are supported.')


def first(pattern, text):
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else ''


def parse_candidate(text):
    email = first(r'([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})', text)
    phone = first(r'((?:\+?216[ .-]?)?(?:\d[ .-]?){8})', text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    name = lines[0][:120] if lines else 'Unknown candidate'
    skills = []
    common = [
        'python', 'java', 'javascript', 'typescript', 'react', 'flutter',
        'sql', 'excel', 'word', 'powerpoint', 'fastapi', 'docker', 'git',
        'linux', 'ai', 'machine learning', 'recruitment', 'sales',
        'marketing', 'accounting', 'mechanic', 'automotive', 'comptabilité',
        'fiscalité', 'déclaration', 'english', 'french', 'arabic'
    ]
    low = text.lower()
    for s in common:
        if s in low:
            skills.append(s)
    return name, email, phone, ', '.join(skills), '', ''


def cosine(a, b):
    A = tokens(a)
    B = tokens(b)
    if not A or not B:
        return 0
    return len(A & B) / math.sqrt(len(A) * len(B)) * 100


def match(job, cand):
    jd = job['description'] or ''
    must = tokens(job['must_have'] or '')
    cv = tokens(cand['raw_text'] or '')
    common = must & cv
    skill_score = 100 * len(common) / max(1, len(must)) if must else 100
    semantic = cosine(jd, cand['raw_text'])
    years = [
        int(x) for x in re.findall(
            r'(\d{1,2})\s*(?:years?|ans|\u0633\u0646\u0648\u0627\u062A)',
            cand['raw_text'],
            re.I
        )
    ]
    exp = min(100, max(years) * 10) if years else 50
    score = round(skill_score * .50 + semantic * .35 + exp * .15, 1)
    missing = sorted(must - cv)
    explanation = (
        f'{len(common)}/{len(must) if must else 0} must-have skills matched; '
        f'semantic {semantic:.1f}%; experience {exp:.0f}%.'
    )
    return score, skill_score, semantic, exp, missing, explanation


class Job(BaseModel):
    title: str
    description: str
    must_have: str = ''


def auth(x_api_key):
    if x_api_key is None:
        return
    if clean(x_api_key) != API_KEY:
        raise HTTPException(401, 'Invalid API key')


@app.on_event('startup')
def startup():
    try:
        db().close()
    except Exception:
        pass


@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return {'status': 'ok', 'service': 'HireRank', 'version': '2.0'}


# ---------- API: Jobs ----------
@app.post('/api/jobs')
def create_job(job: Job, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    jid = str(uuid.uuid4())
    conn = db()
    q_run(conn,
          'INSERT INTO jobs VALUES(%s,%s,%s,%s,%s)',
          (jid, clean(job.title), clean(job.description),
           clean(job.must_have) or '', datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    audit('job_created', jid)
    return {'id': jid, 'title': job.title, 'must_have': job.must_have}


@app.get('/api/jobs')
def list_jobs(x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn, 'SELECT * FROM jobs ORDER BY created_at DESC')
    conn.close()
    return rows


# ---------- API: Candidates ----------
@app.post('/api/jobs/{job_id}/candidates')
async def candidates(
    job_id: str,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, 'SELECT * FROM jobs WHERE id=%s', (job_id,))
    if not job:
        conn.close()
        raise HTTPException(404, 'Job not found')

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
            raise HTTPException(400, f'Could not read file: {f.filename}')

        name, email, phone, skills, education, experience = parse_candidate(text)

        cid = None
        if email:
            existing = q_one(conn,
                'SELECT id FROM candidates WHERE email=%s LIMIT 1',
                (email,))
            if existing:
                cid = existing['id']
                q_run(conn,
                    '''UPDATE candidates
                       SET filename=%s, name=%s, phone=%s, skills=%s,
                           raw_text=%s
                       WHERE id=%s''',
                    (f.filename, name, phone, skills, text, cid))

        if not cid:
            cid = str(uuid.uuid4())
            q_run(conn,
                'INSERT INTO candidates VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (cid, f.filename, name, email, phone, '',
                 skills, education, experience, text,
                 datetime.utcnow().isoformat()))

        cand = {'raw_text': text}
        score, ss, sem, ex, missing, explain = match(job, cand)
        status = (
            'shortlist' if score >= 70
            else 'review' if score >= 50
            else 'reject'
        )

        app_existing = q_one(conn,
            'SELECT id FROM applications WHERE job_id=%s AND candidate_id=%s',
            (job_id, cid))

        if app_existing:
            q_run(conn,
                '''UPDATE applications
                   SET score=%s, skill_score=%s, semantic_score=%s,
                       experience_score=%s, missing=%s, status=%s,
                       explanation=%s, created_at=%s
                   WHERE id=%s''',
                (score, ss, sem, ex, ','.join(missing), status,
                 explain, datetime.utcnow().isoformat(),
                 app_existing['id']))
        else:
            aid = str(uuid.uuid4())
            q_run(conn,
                'INSERT INTO applications VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (aid, job_id, cid, score, ss, sem, ex,
                 ','.join(missing), status, explain,
                 datetime.utcnow().isoformat()))

        ranked.append({
            'id': cid,
            'filename': f.filename,
            'name': name,
            'email': email,
            'score': score,
            'skill_score': round(ss, 1),
            'semantic_score': round(sem, 1),
            'experience_score': round(ex, 1),
            'missing': missing,
            'status': status,
            'explanation': explain
        })

    conn.commit()
    conn.close()
    ranked.sort(key=lambda x: x['score'], reverse=True)
    audit('bulk_import', f'{job_id}:{len(ranked)}')
    return {
        'job_id': job_id,
        'count': len(ranked),
        'files_received': [x['filename'] for x in ranked],
        'ranked': ranked
    }
    for f in files:
        data = await f.read()
        try:
            text = extract_text(f.filename, data)
        except ValueError as e:
            conn.close()
            raise HTTPException(400, str(e))
        except Exception:
            conn.close()
            raise HTTPException(400, f'Could not read file: {f.filename}')

        name, email, phone, skills, education, experience = parse_candidate(text)
        cid = str(uuid.uuid4())
        q_run(conn,
              'INSERT INTO candidates VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              (cid, f.filename, name, email, phone, '',
               skills, education, experience, text,
               datetime.utcnow().isoformat()))

        cand = {'raw_text': text}
        score, ss, sem, ex, missing, explain = match(job, cand)
        status = (
            'shortlist' if score >= 70
            else 'review' if score >= 50
            else 'reject'
        )
        aid = str(uuid.uuid4())
        q_run(conn,
              'INSERT INTO applications VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              (aid, job_id, cid, score, ss, sem, ex,
               ','.join(missing), status, explain,
               datetime.utcnow().isoformat()))

        ranked.append({
            'id': cid, 'filename': f.filename, 'name': name,
            'email': email, 'score': score,
            'skill_score': round(ss, 1),
            'semantic_score': round(sem, 1),
            'experience_score': round(ex, 1),
            'missing': missing, 'status': status,
            'explanation': explain
        })

    conn.commit()
    conn.close()
    ranked.sort(key=lambda x: x['score'], reverse=True)
    audit('bulk_import', f'{job_id}:{len(ranked)}')
    return {
        'job_id': job_id,
        'count': len(ranked),
        'files_received': [x['filename'] for x in ranked],
        'ranked': ranked
    }


# ---------- API: Results (redirect to HTML if browser) ----------
@app.get('/api/jobs/{job_id}/results')
def results(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    job_id = clean(job_id)

    accept = request.headers.get('accept', '')
    if 'text/html' in accept:
        key = x_api_key or ''
        return RedirectResponse(
            url=f'/results/{job_id}?x_api_key={key}',
            status_code=307
        )

    conn = db()
    rows = q_all(conn,
                 '''SELECT a.*, c.name, c.email, c.filename
                    FROM applications a
                    JOIN candidates c ON c.id = a.candidate_id
                    WHERE a.job_id = %s
                    ORDER BY a.score DESC''',
                 (job_id,))
    conn.close()
    return rows


@app.get('/api/candidates/search')
def search(q: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn,
                 'SELECT id,name,email,filename,skills FROM candidates WHERE raw_text LIKE %s LIMIT 100',
                 ('%' + q + '%',))
    conn.close()
    return rows


# ---------- Shared CSS ----------
SHARED_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;min-height:100vh;overflow-x:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 50%,#bfdbfe 100%);
     color:#111;line-height:1.5;padding:0;display:flex;
     flex-direction:column;min-height:100vh}
.wrap{width:100%;max-width:100%;padding:16px;flex:1;
      display:flex;justify-content:center;align-items:flex-start;
      padding-top:max(16px,env(safe-area-inset-top,16px))}
@media(min-width:640px){.wrap{align-items:center;padding:24px}}
.card{background:#fff;border-radius:20px;padding:24px 20px;
      width:100%;max-width:520px;
      box-shadow:0 12px 40px rgba(30,64,175,.12),
                 0 2px 8px rgba(30,64,175,.06)}
.lang-switch{display:flex;justify-content:center;gap:6px;margin-bottom:20px}
.lang-switch button{padding:6px 14px;border:2px solid #e2e8f0;
                    background:#f8fafc;color:#64748b;border-radius:20px;
                    font-size:13px;font-weight:700;cursor:pointer;
                    -webkit-tap-highlight-color:transparent;transition:all .15s}
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
"""

LANG_JS = """
function setLang(l){
  document.body.setAttribute('data-lang', l);
  document.documentElement.setAttribute('lang', l);
  document.documentElement.setAttribute('dir', l==='ar'?'rtl':'ltr');
  try{localStorage.setItem('lang', l);}catch(e){}
  document.querySelectorAll('.lang-switch button').forEach(function(b){
    b.classList.toggle('active', b.dataset.lang === l);
  });
}
(function(){
  var saved = 'ar';
  try{ saved = localStorage.getItem('lang') || 'ar'; }catch(e){}
  setLang(saved);
})();
function langSwitchHTML(){
  return '<div class="lang-switch">' +
    '<button data-lang="ar" onclick="setLang(\\'ar\\')">🇹🇳 عربي</button>' +
    '<button data-lang="fr" onclick="setLang(\\'fr\\')">🇫🇷 Français</button>' +
    '<button data-lang="en" onclick="setLang(\\'en\\')">🇬🇧 English</button>' +
    '</div>';
}
"""

LANG_SWITCH_BLOCK = """
  <div class="lang-switch">
    <button data-lang="ar" onclick="setLang('ar')">🇹🇳 عربي</button>
    <button data-lang="fr" onclick="setLang('fr')">🇫🇷 Français</button>
    <button data-lang="en" onclick="setLang('en')">🇬🇧 English</button>
  </div>
"""


# ---------- Page: Home ----------
@app.get('/', response_class=HTMLResponse)
def root():
    html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>HireRank</title>
<style>
""" + SHARED_CSS + """
h1{font-size:32px;color:#1e40af;text-align:center;margin-bottom:6px;
   font-weight:900;letter-spacing:-.5px}
.brand-tag{text-align:center;color:#64748b;font-size:13px;
           margin-bottom:24px;font-weight:600}
.status{text-align:center;color:#059669;font-weight:700;
        font-size:14px;margin-bottom:24px;
        display:flex;align-items:center;justify-content:center;gap:6px}
.status::before{content:'';width:8px;height:8px;background:#10b981;
                border-radius:50%;box-shadow:0 0 0 3px rgba(16,185,129,.2)}
ul{list-style:none}
li{margin-bottom:10px}
li a{display:flex;align-items:center;justify-content:space-between;
     padding:16px 20px;background:#f8fafc;border:2px solid #e2e8f0;
     border-radius:14px;text-decoration:none;color:#1e293b;
     font-weight:700;font-size:16px;transition:all .15s}
li a:active{background:#dbeafe;border-color:#3b82f6;transform:scale(.98)}
li a.primary{background:linear-gradient(135deg,#2563eb,#1d4ed8);
             border:none;color:#fff;font-size:17px;
             box-shadow:0 4px 16px rgba(37,99,235,.35)}
li a.primary:active{background:#1e40af}
.icon{font-size:22px}
</style>
</head>
<body data-lang="ar">
<div class="wrap">
  <div class="card">
    """ + LANG_SWITCH_BLOCK + """
    <h1>⚡ HireRank</h1>
    <p class="brand-tag">
      <span class="lang-ar inline">فرز السير الذاتية بالذكاء الاصطناعي</span>
      <span class="lang-fr inline">Tri de CV par IA</span>
      <span class="lang-en inline">AI CV Ranking</span>
    </p>
    <p class="status">
      <span class="lang-ar inline">الخدمة تعمل</span>
      <span class="lang-fr inline">Service en ligne</span>
      <span class="lang-en inline">Service online</span>
    </p>
    <ul>
      <li><a class="primary" href="/upload">
        <span class="lang-ar inline">رفع السير الذاتية</span>
        <span class="lang-fr inline">Téléverser des CV</span>
        <span class="lang-en inline">Upload CVs</span>
        <span class="icon">📄</span>
      </a></li>
      <li><a href="/docs">
        <span class="lang-ar inline">واجهة API</span>
        <span class="lang-fr inline">API (Swagger)</span>
        <span class="lang-en inline">API (Swagger)</span>
        <span class="icon">📚</span>
      </a></li>
      <li><a href="/health">
        <span class="lang-ar inline">فحص الحالة</span>
        <span class="lang-fr inline">État du service</span>
        <span class="lang-en inline">Health check</span>
        <span class="icon">💚</span>
      </a></li>
    </ul>
  </div>
</div>
<script>
""" + LANG_JS + """
</script>
</body>
</html>"""
    return html


# ---------- Page: Upload ----------
@app.get('/upload', response_class=HTMLResponse)
def upload_page():
    html = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>HireRank — Upload</title>
<style>
""" + SHARED_CSS + """
h1{font-size:24px;color:#1e40af;text-align:center;margin-bottom:4px;
   font-weight:800}
.subtitle{text-align:center;color:#64748b;font-size:13px;
          margin-bottom:22px;font-weight:600}
.field{display:block;font-weight:700;font-size:14px;
       color:#374151;margin:16px 0 6px}
input[type=text],input[type=password]{
  width:100%;padding:14px;font-size:16px;
  border:2px solid #e2e8f0;border-radius:12px;
  background:#f8fafc;transition:border .15s;
  font-family:inherit}
input[type=text]:focus,input[type=password]:focus{
  outline:none;border-color:#3b82f6;background:#fff}
.file-btn{display:flex;align-items:center;justify-content:center;
          padding:22px 14px;background:#f0f9ff;
          border:2px dashed #60a5fa;border-radius:14px;
          font-size:16px;font-weight:800;color:#1e40af;
          cursor:pointer;user-select:none;
          -webkit-tap-highlight-color:transparent;transition:background .15s}
.file-btn:active{background:#dbeafe}
input[type=file]{display:none}
.hint{font-size:12px;color:#64748b;text-align:center;margin-top:8px}
.file-list{margin-top:14px;padding:12px;background:#f0fdf4;
           border:2px solid #86efac;border-radius:12px;display:none}
.file-list.show{display:block}
.file-list .title{font-size:13px;color:#166534;
                  font-weight:800;margin-bottom:10px}
.file-item{display:flex;justify-content:space-between;
           align-items:center;padding:8px 10px;
           background:#fff;border-radius:8px;margin-bottom:6px;
           font-size:13px;direction:ltr}
.file-item:last-child{margin-bottom:0}
.file-item .name{flex:1;overflow:hidden;text-overflow:ellipsis;
                 white-space:nowrap;color:#1e293b}
.file-item .remove{color:#dc2626;font-weight:800;cursor:pointer;
                   padding:0 8px;font-size:18px;line-height:1}
button.main{width:100%;padding:18px;font-size:17px;
            background:linear-gradient(135deg,#2563eb,#1d4ed8);
            color:#fff;border:none;border-radius:14px;
            font-weight:800;margin-top:20px;cursor:pointer;
            -webkit-tap-highlight-color:transparent;
            transition:transform .1s;
            box-shadow:0 4px 16px rgba(37,99,235,.3)}
button.main:active{transform:scale(.98)}
button.main:disabled{background:#94a3b8;cursor:not-allowed;
                     box-shadow:none}
pre{background:#0f172a;color:#10b981;padding:14px;
    border-radius:12px;font-size:12px;overflow-x:auto;
    max-height:300px;white-space:pre-wrap;word-break:break-all;
    margin-top:8px;direction:ltr;text-align:left;font-family:monospace}
</style>
</head>
<body data-lang="ar">
<div class="wrap">
  <div class="card">
    """ + LANG_SWITCH_BLOCK + """
    <h1>⚡ HireRank</h1>
    <p class="subtitle">
      <span class="lang-ar inline">رفع السير الذاتية</span>
      <span class="lang-fr inline">Téléverser des CV</span>
      <span class="lang-en inline">Upload CVs</span>
    </p>

    <label class="field">
      <span class="lang-ar inline">معرّف الوظيفة (Job ID)</span>
      <span class="lang-fr inline">Identifiant de l'offre</span>
      <span class="lang-en inline">Job ID</span>
    </label>
    <input id="job_id" type="text" placeholder="c9d62982-..."
           autocomplete="off" autocapitalize="off" spellcheck="false">

    <label class="field">
      <span class="lang-ar inline">مفتاح API</span>
      <span class="lang-fr inline">Clé API</span>
      <span class="lang-en inline">API Key</span>
    </label>
    <input id="api_key" type="password" placeholder="..."
           autocomplete="off">

    <label class="field">
      <span class="lang-ar inline">ملفات السير الذاتية (PDF, DOCX, TXT)</span>
      <span class="lang-fr inline">Fichiers CV (PDF, DOCX, TXT)</span>
      <span class="lang-en inline">CV files (PDF, DOCX, TXT)</span>
    </label>
    <label for="filePicker" class="file-btn">
      <span class="lang-ar inline">📎 اضغط لاختيار ملف</span>
      <span class="lang-fr inline">📎 Choisir un fichier</span>
      <span class="lang-en inline">📎 Choose a file</span>
    </label>
    <input id="filePicker" type="file">
    <p class="hint">
      <span class="lang-ar inline">يمكنك إضافة عدة ملفات — اضغط الزر عدة مرات</span>
      <span class="lang-fr inline">Plusieurs fichiers — cliquez plusieurs fois</span>
      <span class="lang-en inline">Multiple files — click multiple times</span>
    </p>

    <div class="file-list" id="fileList">
      <div class="title">
        <span class="lang-ar inline">✅ الملفات المختارة (<span id="count">0</span>)</span>
        <span class="lang-fr inline">✅ Fichiers sélectionnés (<span id="countF">0</span>)</span>
        <span class="lang-en inline">✅ Selected files (<span id="countE">0</span>)</span>
      </div>
      <div id="items"></div>
    </div>

    <button class="main" id="uploadBtn" onclick="doUpload()">
      <span class="lang-ar inline">🚀 ارفع وقيّم</span>
      <span class="lang-fr inline">🚀 Téléverser et évaluer</span>
      <span class="lang-en inline">🚀 Upload &amp; rank</span>
    </button>

    <label class="field">
      <span class="lang-ar inline">النتيجة</span>
      <span class="lang-fr inline">Résultat</span>
      <span class="lang-en inline">Result</span>
    </label>
    <pre id="result">—</pre>
  </div>
</div>

<script>
""" + LANG_JS + """
var pickedFiles = [];
var picker = document.getElementById('filePicker');
var fileListBox = document.getElementById('fileList');
var itemsBox = document.getElementById('items');

function updateCount(){
  var n = pickedFiles.length;
  document.getElementById('count').textContent = n;
  document.getElementById('countF').textContent = n;
  document.getElementById('countE').textContent = n;
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
      var nameEl = document.createElement('span');
      nameEl.className = 'name';
      nameEl.textContent = (idx+1) + '. ' + f.name;
      var rm = document.createElement('span');
      rm.className = 'remove';
      rm.textContent = '✕';
      rm.onclick = function(){ pickedFiles.splice(idx, 1); renderList(); };
      row.appendChild(nameEl);
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
      if(pickedFiles[j].name === f.name && pickedFiles[j].size === f.size){
        exists = true; break;
      }
    }
    if(!exists) pickedFiles.push(f);
  }
  picker.value = '';
  renderList();
});

function cleanInput(s){
  return (s || '').replace(/[\u200B-\u200D\uFEFF]/g, '').trim();
}

function msg(key){
  var L = document.body.getAttribute('data-lang') || 'ar';
  var M = {
    fill: {ar:'⚠️ املأ معرّف الوظيفة ومفتاح API',
           fr:'⚠️ Remplissez votre ID et la clé API  ',
           en:'⚠️ Fill Job ID and API Key'},
    pick: {ar:'⚠️ اختر ملفًا واحدًا على الأقل',
           fr:'⚠️ Choisissez au moins un fichier',
           en:'⚠️ Choose at least one file'},
    up:   {ar:'⏳ جارٍ رفع ', fr:'⏳ Téléversement de ', en:'⏳ Uploading '},
    up2:  {ar:' ملف...', fr:' fichier(s)...', en:' file(s)...'},
    ok:   {ar:'✅ تم رفع ', fr:'✅ ', en:'✅ '},
    ok2:  {ar:' ملف بنجاح!\\n⏳ جارٍ التحويل...',
           fr:' fichier(s) téléversé(s)!\\n⏳ Redirection...',
           en:' file(s) uploaded!\\n⏳ Redirecting...'},
    err:  {ar:'❌ خطأ: ', fr:'❌ Erreur: ', en:'❌ Error: '}
  };
  return M[key][L] || M[key].ar;
}

async function doUpload(){
  var jid = cleanInput(document.getElementById('job_id').value);
  var key = cleanInput(document.getElementById('api_key').value);
  var out = document.getElementById('result');
  var btn = document.getElementById('uploadBtn');

  if(!jid || !key){ out.textContent = msg('fill'); return; }
  if(pickedFiles.length === 0){ out.textContent = msg('pick'); return; }

  var fd = new FormData();
  for(var i=0;i<pickedFiles.length;i++) fd.append('files', pickedFiles[i]);

  btn.disabled = true;
  out.textContent = msg('up') + pickedFiles.length + msg('up2');

  try{
    var r = await fetch('/api/jobs/' + encodeURIComponent(jid) + '/candidates', {
      method: 'POST',
      headers: {'x-api-key': key},
      body: fd
    });
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
</script>
</body>
</html>"""
    return html


# ---------- Page: Results ----------
@app.get('/results/{job_id}', response_class=HTMLResponse)
def results_page(job_id: str, x_api_key: str | None = None):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, 'SELECT * FROM jobs WHERE id=%s', (job_id,))
    if not job:
        conn.close()
        return HTMLResponse(
            '<html><body style="font-family:sans-serif;text-align:center;padding:40px;background:#f3f4f6">'
            '<div style="max-width:400px;margin:auto;background:#fff;padding:30px;border-radius:16px">'
            '<h2 style="color:#dc2626">❌ Job not found / الوظيفة غير موجودة</h2>'
            '<p style="color:#6b7280;margin-top:10px">Check the Job ID / تحقق من المعرّف</p>'
            '<a href="/upload" style="display:inline-block;margin-top:20px;padding:12px 24px;'
            'background:#2563eb;color:#fff;text-decoration:none;border-radius:10px">'
            '← Upload / رفع</a></div></body></html>',
            status_code=404
        )

    rows = q_all(conn,
                 '''SELECT a.score, a.status, a.missing,
                           c.name, c.email, c.filename
                    FROM applications a
                    JOIN candidates c ON c.id = a.candidate_id
                    WHERE a.job_id = %s
                    ORDER BY a.score DESC''',
                 (job_id,))
    conn.close()

    rows_html = ''
    shortlist = review = reject = 0

    for i, r in enumerate(rows, 1):
        score = r['score'] or 0
        status = r['status'] or 'review'

        if status == 'shortlist':
            shortlist += 1
            color = '#16a34a'; bg = '#f0fdf4'; icon = '✅'
        elif status == 'review':
            review += 1
            color = '#f59e0b'; bg = '#fffbeb'; icon = '🤔'
        else:
            reject += 1
            color = '#dc2626'; bg = '#fef2f2'; icon = '❌'

        missing = (r['missing'] or '').strip()
        if missing:
            missing_html = (
                '<div class="missing">'
                '<span class="lang-ar inline">⚠️ ناقص: ' + missing.replace(',', '، ') + '</span>'
                '<span class="lang-fr inline">⚠️ Manquant: ' + missing.replace(',', ', ') + '</span>'
                '<span class="lang-en inline">⚠️ Missing: ' + missing.replace(',', ', ') + '</span>'
                '</div>'
            )
        else:
            missing_html = (
                '<div class="missing ok">'
                '<span class="lang-ar inline">✅ جميع المهارات موجودة</span>'
                '<span class="lang-fr inline">✅ Toutes les compétences présentes</span>'
                '<span class="lang-en inline">✅ All skills present</span>'
                '</div>'
            )

        name = r['name'] or '?'
        email = r['email'] or '—'
        filename = r['filename'] or '—'

        status_html = ''
        if status == 'shortlist':
            status_html = ('<span class="lang-ar inline">مؤهل — يوصى بمقابلته</span>'
                          '<span class="lang-fr inline">Qualifié — à convoquer</span>'
                          '<span class="lang-en inline">Qualified — recommend interview</span>')
        elif status == 'review':
            status_html = ('<span class="lang-ar inline">يحتاج مراجعة</span>'
                          '<span class="lang-fr inline">À examiner</span>'
                          '<span class="lang-en inline">Needs review</span>')
        else:
            status_html = ('<span class="lang-ar inline">مرفوض</span>'
                          '<span class="lang-fr inline">Rejeté</span>'
                          '<span class="lang-en inline">Rejected</span>')

        rows_html += f'''
        <div class="r-card" style="background:{bg};border-{("left" if True else "right")}:6px solid {color}">
          <div class="r-rank" style="background:{color}">#{i}</div>
          <div class="r-name">{icon} {name}</div>
          <div class="r-score-row">
            <span class="r-score" style="color:{color}">{score}</span>
            <span class="r-score-label">/ 100</span>
          </div>
          <div class="r-status" style="color:{color}">{status_html}</div>
          <div class="r-meta">📧 {email}</div>
          <div class="r-meta">📄 {filename}</div>
          {missing_html}
        </div>'''

    if not rows:
        rows_html = ('<div class="empty">'
                    '<span class="lang-ar inline">لا يوجد مرشحون بعد</span>'
                    '<span class="lang-fr inline">Aucun candidat pour le moment</span>'
                    '<span class="lang-en inline">No candidates yet</span>'
                    '</div>')

    title = job['title'] or '—'
    must = job['must_have'] or '—'
    key_q = x_api_key or ''

    html = f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>HireRank — Résultats</title>
<style>
''' + SHARED_CSS + f'''
.header{{background:#fff;border-radius:20px;padding:22px;
        margin-bottom:16px;box-shadow:0 12px 40px rgba(30,64,175,.12)}}
h1{{font-size:22px;color:#1e40af;text-align:center;margin-bottom:12px;
   font-weight:800}}
.job-title{{font-size:16px;color:#0f172a;font-weight:800;
           text-align:center;margin-bottom:6px}}
.must-have{{font-size:13px;color:#64748b;text-align:center;
           margin-bottom:18px;word-break:break-word}}
.stats{{display:flex;gap:8px}}
.stat{{flex:1;text-align:center;padding:12px 6px;
      border-radius:12px;font-size:12px;font-weight:800}}
.stat .num{{display:block;font-size:24px;margin-bottom:2px}}
.stat.green{{background:#dcfce7;color:#166534}}
.stat.yellow{{background:#fef3c7;color:#92400e}}
.stat.red{{background:#fee2e2;color:#991b1b}}
.r-card{{background:#fff;border-radius:16px;padding:18px;
        margin-bottom:12px;position:relative;
        box-shadow:0 2px 12px rgba(0,0,0,.06)}}
.r-rank{{position:absolute;top:14px;left:14px;color:#fff;
        font-weight:800;font-size:13px;padding:4px 12px;
        border-radius:20px;z-index:1}}
[dir="rtl"] .r-rank{{left:auto;right:14px}}
.r-name{{font-size:17px;font-weight:800;color:#0f172a;
        margin-bottom:8px;padding-{"right" if True else "left"}:55px}}
.r-score-row{{margin:8px 0}}
.r-score{{font-size:30px;font-weight:900}}
.r-score-label{{font-size:13px;color:#94a3b8;margin-{"left" if True else "right"}:4px}}
.r-status{{font-size:14px;font-weight:800;margin-bottom:10px}}
.r-meta{{font-size:13px;color:#475569;margin-top:4px;
        word-break:break-all;direction:ltr;text-align:{'left' if True else 'right'}}}
.missing{{font-size:13px;color:#991b1b;margin-top:10px;
         padding:10px 12px;background:#fff;border-radius:8px;
         border-{"right" if True else "left"}:3px solid #dc2626}}
.missing.ok{{color:#166534;border-{"right" if True else "left"}-color:#16a34a}}
.empty{{text-align:center;padding:60px 20px;color:#64748b;
       background:#fff;border-radius:16px;font-size:15px}}
.actions{{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}}
.btn{{flex:1;min-width:140px;text-align:center;padding:16px 20px;
     border-radius:14px;text-decoration:none;font-weight:800;
     font-size:15px;transition:transform .1s;
     -webkit-tap-highlight-color:transparent}}
.btn:active{{transform:scale(.97)}}
.btn.green{{background:linear-gradient(135deg,#16a34a,#15803d);color:#fff}}
.btn.blue{{background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff}}
</style>
</head>
<body data-lang="ar">
<div class="wrap">
  <div style="width:100%;max-width:600px">
    <div class="header">
      """ + LANG_SWITCH_BLOCK + """
      <h1>
        <span class="lang-ar inline">📊 نتائج الفرز</span>
        <span class="lang-fr inline">📊 Résultats du tri</span>
        <span class="lang-en inline">📊 Ranking Results</span>
      </h1>
      <div class="job-title">💼 {title}</div>
      <div class="must-have">🎯 {must}</div>
      <div class="stats">
        <div class="stat green"><span class="num">{shortlist}</span>
          <span class="lang-ar inline">مؤهل</span>
          <span class="lang-fr inline">Qualifiés</span>
          <span class="lang-en inline">Qualified</span>
        </div>
        <div class="stat yellow"><span class="num">{review}</span>
          <span class="lang-ar inline">مراجعة</span>
          <span class="lang-fr inline">À revoir</span>
          <span class="lang-en inline">Review</span>
        </div>
        <div class="stat red"><span class="num">{reject}</span>
          <span class="lang-ar inline">مرفوض</span>
          <span class="lang-fr inline">Rejetés</span>
          <span class="lang-en inline">Rejected</span>
        </div>
      </div>
    </div>

    {rows_html}

    <div class="actions">
      <a class="btn green" href="/api/jobs/{job_id}/export.csv?x_api_key={key_q}">
        <span class="lang-ar inline">📥 تحميل Excel</span>
        <span class="lang-fr inline">📥 Télécharger Excel</span>
        <span class="lang-en inline">📥 Download Excel</span>
      </a>
      <a class="btn blue" href="/upload">
        <span class="lang-ar inline">🔄 رفع ملفات جديدة</span>
        <span class="lang-fr inline">🔄 Nouveaux fichiers</span>
        <span class="lang-en inline">🔄 Upload new files</span>
      </a>
    </div>
  </div>
</div>
<script>
''' + LANG_JS + '''
</script>
</body>
</html>'''
    return html


# ---------- API: CSV Export ----------
@app.get('/api/jobs/{job_id}/export.csv')
def export_csv(job_id: str, x_api_key: str | None = None):
    auth(x_api_key)
    job_id = clean(job_id)

    conn = db()
    job = q_one(conn, 'SELECT title FROM jobs WHERE id=%s', (job_id,))
    if not job:
        conn.close()
        raise HTTPException(404, 'Job not found')

    rows = q_all(conn,
                 '''SELECT a.score, a.status, a.skill_score,
                           a.semantic_score, a.experience_score, a.missing,
                           c.name, c.email, c.phone, c.filename
                    FROM applications a
                    JOIN candidates c ON c.id = a.candidate_id
                    WHERE a.job_id = %s
                    ORDER BY a.score DESC''',
                 (job_id,))
    conn.close()

    def esc(v):
        if v is None:
            return ''
        s = str(v).replace('"', '""')
        if any(ch in s for ch in [',', '"', '\n']):
            return '"' + s + '"'
        return s

    lines = ['الترتيب,الاسم,البريد,الهاتف,الملف,الدرجة,المهارات,الدلالي,الخبرة,الحالة,الناقص']
    for i, r in enumerate(rows, 1):
        lines.append(','.join([
            str(i), esc(r['name']), esc(r['email']), esc(r['phone']),
            esc(r['filename']), str(r['score'] or 0),
            str(r['skill_score'] or 0), str(r['semantic_score'] or 0),
            str(r['experience_score'] or 0), esc(r['status']),
            esc(r['missing'])
        ]))

    csv = '\ufeff' + '\n'.join(lines)
    return HTMLResponse(
        content=csv,
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':
                 f'attachment; filename="hirerank_{job_id[:8]}.csv"'}
    )


# ---------- OpenAPI fix ----------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    for comp in schema.get('components', {}).get('schemas', {}).values():
        for prop in comp.get('properties', {}).values():
            if prop.get('contentMediaType') == 'application/octet-stream':
                del prop['contentMediaType']
                prop['format'] = 'binary'
            items = prop.get('items', {})
            if items.get('contentMediaType') == 'application/octet-stream':
                del items['contentMediaType']
                items['format'] = 'binary'
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
