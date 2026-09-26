from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import HTMLResponse
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
    conn = db()
    q_run(conn,
          'INSERT INTO audit(event,detail,created_at) VALUES(%s,%s,%s)',
          (event, detail, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def tokens(s):
    return set(re.findall(r'[\wÀ-ÿ+#.-]{2,}', (s or '').lower()))


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
    raise ValueError('PDF, DOCX and TXT are supported in this demo.')


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
            r'(\d{1,2})\s*(?:years?|ans|سنوات)',
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


@app.on_event('startup')
def startup():
    db().close()


@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return {'status': 'ok', 'service': 'Tunisia ATS', 'version': '1.0'}


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


@app.get('/api/candidates/search')
def search(q: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    conn = db()
    rows = q_all(conn,
                 'SELECT id,name,email,filename,skills FROM candidates WHERE raw_text LIKE %s LIMIT 100',
                 ('%' + q + '%',))
    conn.close()
    return rows


@app.get('/', response_class=HTMLResponse)
def root():
    return """
    <html><head><title>Tunisia ATS API</title></head>
    <body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px">
      <h1>Tunisia ATS API</h1>
      <p>Status: <b style="color:green">online</b></p>
      <ul>
        <li><a href="/upload">📄 رفع السير الذاتية</a></li>
        <li><a href="/docs">/docs</a> — Swagger UI</li>
        <li><a href="/openapi.json">/openapi.json</a></li>
        <li><a href="/health">/health</a></li>
      </ul>
    </body></html>
    """
@app.get('/upload', response_class=HTMLResponse)
def upload_page():
    return """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload CVs - Tunisia ATS</title>
<style>
  body{font-family:-apple-system,sans-serif;max-width:520px;margin:20px auto;padding:15px;background:#f9fafb;color:#111}
  h1{font-size:20px;color:#1e40af}
  label{display:block;margin-top:14px;font-weight:600;font-size:14px}
  input,button{width:100%;padding:12px;font-size:16px;box-sizing:border-box;margin:6px 0;border:1px solid #d1d5db;border-radius:8px;background:#fff}
  button{background:#2563eb;color:#fff;border:none;font-weight:600;margin-top:18px}
  button:active{background:#1e40af}
  pre{background:#111827;color:#10b981;padding:12px;overflow-x:auto;font-size:12px;border-radius:8px;max-height:400px}
  .hint{font-size:12px;color:#6b7280;margin-top:-4px}
</style>
</head>
<body>
<h1>📄 رفع السير الذاتية — Tunisia ATS</h1>

<label>معرّف الوظيفة (Job ID)</label>
<input id="job_id" placeholder="73e1aa36-..." autocomplete="off">

<label>مفتاح API</label>
<input id="api_key" type="password" placeholder="tunisia123" autocomplete="off">

<label>ملفات السير الذاتية (PDF, DOCX, TXT)</label>
<input id="files" type="file" multiple>
<p class="hint">يمكنك اختيار أكثر من ملف في نفس الوقت</p>

<button onclick="upload()">🚀 ارفع وقيّم</button>

<label>النتيجة</label>
<pre id="result">—</pre>

<script>
async function upload(){
  const jid=document.getElementById('job_id').value.trim();
  const key=document.getElementById('api_key').value.trim();
  const fi=document.getElementById('files');
  const out=document.getElementById('result');
  if(!jid||!key||!fi.files.length){out.textContent='⚠️ املأ كل الحقول واختر ملفًا واحدًا على الأقل';return;}
  const fd=new FormData();
  for(const f of fi.files) fd.append('files',f);
  out.textContent='⏳ جارٍ الرفع... الرجاء الانتظار';
  try{
    const r=await fetch('/api/jobs/'+jid+'/candidates',{method:'POST',headers:{'x-api-key':key},body:fd});
    const d=await r.json();
    out.textContent=JSON.stringify(d,null,2);
  }catch(e){out.textContent='❌ خطأ: '+e.message;}
}
</script>
</body>
</html>
    """
