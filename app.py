from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import HTMLResponse
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
app = FastAPI(title='Tunisia ATS Pro', version='1.0')

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
CREATE TABLE IF NOT EXISTS api_keys(
    id SERIAL PRIMARY KEY, key_hash TEXT UNIQUE, created_at TEXT);
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
        'marketing', 'accounting', 'mechanic', 'automotive',
        'english', 'french', 'arabic'
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
    jd = job['description']
    must = tokens(job['must_have'])
    cv = tokens(cand['raw_text'])
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
        f'semantic similarity {semantic:.1f}%; '
        f'experience signal {exp:.0f}%.'
    )
    return score, skill_score, semantic, exp, missing, explanation


class Job(BaseModel):
    title: str
    description: str
    must_have: str = ''


def auth(x_api_key):
    if x_api_key is None:
        return
    if x_api_key != API_KEY:
        raise HTTPException(401, 'Invalid API key')


# ---------- Lifecycle ----------
@app.on_event('startup')
def startup():
    try:
        db().close()
    except Exception:
        pass


# ---------- Health ----------
@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return {'status': 'ok', 'service': 'Tunisia ATS', 'version': '1.0'}


# ---------- Jobs ----------
@app.post('/api/jobs')
def create_job(job: Job, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    jid = str(uuid.uuid4())
    conn = db()
    q_run(conn,
          'INSERT INTO jobs VALUES(%s,%s,%s,%s,%s)',
          (jid, job.title, job.description, job.must_have,
           datetime.utcnow().isoformat()))
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


# ---------- Candidates upload + ranking ----------
@app.post('/api/jobs/{job_id}/candidates')
async def candidates(
    job_id: str,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)

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


# ---------- Results ----------
@app.get('/api/jobs/{job_id}/results')
def results(job_id: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
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


# ---------- Search ----------
@app.get('/api/candidates/search')
def search(q: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn,
                 'SELECT id,name,email,filename,skills FROM candidates WHERE raw_text LIKE %s LIMIT 100',
                 ('%' + q + '%',))
    conn.close()
    return rows


# ---------- Root ----------
@app.get('/', response_class=HTMLResponse)
def root():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tunisia ATS API</title>
<style>
  body{font-family:-apple-system,sans-serif;max-width:520px;margin:40px auto;padding:0 20px;background:#f9fafb;color:#111}
  h1{color:#1e40af;text-align:center}
  .status{text-align:center;font-size:18px;margin:20px 0}
  .status b{color:#059669}
  ul{list-style:none;padding:0;margin-top:30px}
  li{margin:12px 0}
  li a{
    display:block;padding:14px;background:#fff;border:1px solid #d1d5db;
    border-radius:10px;text-decoration:none;color:#1e40af;font-weight:600;
    font-size:16px;text-align:center;
  }
  li a:active{background:#f3f4f6}
</style>
</head>
<body>
  <h1>Tunisia ATS API</h1>
  <p class="status">الحالة: <b>يعمل</b></p>
  <ul>
    <li><a href="/upload">📄 رفع السير الذاتية</a></li>
    <li><a href="/docs">📚 Swagger UI</a></li>
    <li><a href="/health">💚 فحص الحالة</a></li>
    <li><a href="/openapi.json">📋 OpenAPI</a></li>
  </ul>
</body>
</html>
    """


# ---------- Upload page (mobile-friendly) ----------
@app.get('/upload', response_class=HTMLResponse)
def upload_page():
    return """
<!DOCTYPE html>
<html lang="ar">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<title>رفع السير الذاتية - Tunisia ATS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{width:100%;overflow-x:hidden}
  body{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:#f3f4f6;color:#111;
    padding:16px;
    min-height:100vh;
    display:flex;
    justify-content:center;
  }
  .wrap{
    width:100%;
    max-width:480px;
    background:#fff;
    border-radius:16px;
    padding:20px;
    box-shadow:0 2px 10px rgba(0,0,0,.06);
    margin:auto;
  }
  h1{
    font-size:20px;color:#1e40af;text-align:center;
    margin-bottom:20px;font-weight:700;
  }
  .field{
    display:block;margin-top:16px;margin-bottom:6px;
    font-weight:600;font-size:14px;color:#374151;
    text-align:right;
  }
  input[type=text],input[type=password]{
    width:100%;padding:12px;font-size:16px;
    border:1px solid #d1d5db;border-radius:10px;
    background:#fff;text-align:right;
    direction:ltr;
  }
  input[type=text]:focus,input[type=password]:focus{
    outline:none;border-color:#2563eb;
  }
  .file-btn{
    display:flex;align-items:center;justify-content:center;
    width:100%;padding:22px 14px;
    background:#eff6ff;border:2px dashed #93c5fd;
    border-radius:12px;
    font-size:16px;font-weight:600;color:#1e40af;
    cursor:pointer;margin-top:4px;
    user-select:none;
    -webkit-tap-highlight-color:transparent;
    transition:background .15s;
  }
  .file-btn:active{background:#dbeafe}
  input[type=file]{
    position:absolute;
    width:1px;height:1px;
    opacity:0;
    pointer-events:none;
  }
  .hint{
    font-size:12px;color:#6b7280;
    margin-top:8px;text-align:center;
  }
  .file-list{
    margin-top:12px;
    border:1px solid #e5e7eb;
    border-radius:10px;
    padding:10px;
    background:#f9fafb;
    display:none;
  }
  .file-list.show{display:block}
  .file-list .title{
    font-size:13px;font-weight:600;color:#059669;
    margin-bottom:8px;text-align:right;
  }
  .file-item{
    display:flex;align-items:center;justify-content:space-between;
    padding:8px 10px;background:#fff;
    border-radius:8px;margin-bottom:6px;
    font-size:13px;
    direction:ltr;
  }
  .file-item:last-child{margin-bottom:0}
  .file-item .name{
    flex:1;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;
  }
  .file-item .remove{
    color:#dc2626;font-weight:700;
    cursor:pointer;padding:4px 10px;
    font-size:18px;line-height:1;
    -webkit-tap-highlight-color:transparent;
  }
  button.main{
    width:100%;padding:16px;font-size:17px;
    background:#2563eb;color:#fff;
    border:none;border-radius:12px;
    font-weight:700;margin-top:20px;
    cursor:pointer;
    -webkit-tap-highlight-color:transparent;
    transition:background .15s;
  }
  button.main:active{background:#1e40af}
  button.main:disabled{
    background:#9ca3af;cursor:not-allowed;
  }
  pre{
    background:#111827;color:#10b981;
    padding:12px;border-radius:10px;
    font-size:12px;
    overflow-x:auto;
    max-height:400px;
    white-space:pre-wrap;
    word-break:break-all;
    margin-top:8px;
    direction:ltr;text-align:left;
  }
</style>
</head>
<body>
<div class="wrap">

  <h1>📄 رفع السير الذاتية<br>Tunisia ATS</h1>

  <label class="field" for="job_id">معرّف الوظيفة (Job ID)</label>
  <input id="job_id" type="text" placeholder="c9d62982-45b2-..." autocomplete="off" autocapitalize="off" spellcheck="false">

  <label class="field" for="api_key">مفتاح API</label>
  <input id="api_key" type="password" placeholder="أدخل المفتاح" autocomplete="off">

  <label class="field">ملفات السير الذاتية (PDF, DOCX, TXT)</label>

  <label for="filePicker" class="file-btn">
    📎 اضغط لاختيار ملف
  </label>
  <input id="filePicker" type="file">

  <p class="hint">يمكنك إضافة عدة ملفات — اضغط الزر عدة مرات</p>

  <div class="file-list" id="fileList">
    <div class="title">✅ الملفات المختارة (<span id="count">0</span>)</div>
    <div id="items"></div>
  </div>

  <button class="main" id="uploadBtn" onclick="doUpload()">
    🚀 ارفع وقيّم
  </button>

  <label class="field">النتيجة</label>
  <pre id="result">—</pre>

</div>

<script>
var pickedFiles = [];
var picker = document.getElementById('filePicker');
var fileListBox = document.getElementById('fileList');
var itemsBox = document.getElementById('items');
var countSpan = document.getElementById('count');

function renderList(){
  itemsBox.innerHTML = '';
  countSpan.textContent = pickedFiles.length;

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
      rm.onclick = function(){
        pickedFiles.splice(idx, 1);
        renderList();
      };

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

async function doUpload(){
  var jid = document.getElementById('job_id').value.trim();
  var key = document.getElementById('api_key').value.trim();
  var out = document.getElementById('result');
  var btn = document.getElementById('uploadBtn');

  if(!jid || !key){
    out.textContent = '⚠️ املأ معرّف الوظيفة ومفتاح API';
    return;
  }
  if(pickedFiles.length === 0){
    out.textContent = '⚠️ اختر ملفًا واحدًا على الأقل';
    return;
  }

  var fd = new FormData();
  for(var i=0;i<pickedFiles.length;i++){
    fd.append('files', pickedFiles[i]);
  }

  btn.disabled = true;
  out.textContent = '⏳ جارٍ رفع ' + pickedFiles.length + ' ملف... الرجاء الانتظار';

  try{
    var r = await fetch('/api/jobs/' + jid + '/candidates', {
      method: 'POST',
      headers: {'x-api-key': key},
      body: fd
    });
    var d = await r.json();
    out.textContent = JSON.stringify(d, null, 2);
  }catch(e){
    out.textContent = '❌ خطأ: ' + e.message;
  }finally{
    btn.disabled = false;
  }
}
</script>
</body>
</html>
    """


# ---------- OpenAPI fix for Swagger UI file upload ----------
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

# ---------- OpenAPI fix for Swagger UI file upload ----------
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
