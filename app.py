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
app = FastAPI(title='Tunisia ATS Pro', version='2.0')

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
    """إزالة المسافات والرموز الخفية"""
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


# ---------- Health ----------
@app.api_route('/health', methods=['GET', 'HEAD'])
def health():
    return {'status': 'ok', 'service': 'Tunisia ATS', 'version': '2.0'}


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
    job_id = clean(job_id)
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


# ---------- Page: Home ----------
@app.get('/', response_class=HTMLResponse)
def root():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tunisia ATS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%);
       min-height:100vh;display:flex;justify-content:center;align-items:center;
       padding:16px}
  .card{background:#fff;border-radius:20px;padding:30px 24px;
        max-width:440px;width:100%;box-shadow:0 10px 40px rgba(30,64,175,.15)}
  h1{font-size:26px;color:#1e40af;text-align:center;margin-bottom:6px}
  .status{text-align:center;color:#059669;font-weight:600;
          font-size:14px;margin-bottom:24px}
  .status::before{content:'●';margin-left:6px;font-size:12px}
  ul{list-style:none}
  li{margin-bottom:12px}
  li a{display:flex;align-items:center;justify-content:space-between;
       padding:16px 20px;background:#f8fafc;border:2px solid #e2e8f0;
       border-radius:14px;text-decoration:none;color:#1e293b;
       font-weight:600;font-size:16px;transition:all .15s}
  li a:active{background:#dbeafe;border-color:#3b82f6;transform:scale(.98)}
  li a.primary{background:linear-gradient(135deg,#2563eb,#1d4ed8);
               border:none;color:#fff;font-size:17px}
  li a.primary:active{background:#1e40af}
  .icon{font-size:22px}
</style>
</head>
<body>
<div class="card">
  <h1>🇹🇳 Tunisia ATS</h1>
  <p class="status">الخدمة تعمل</p>
  <ul>
    <li><a class="primary" href="/upload">
      <span>رفع السير الذاتية</span><span class="icon">📄</span>
    </a></li>
    <li><a href="/docs">
      <span>واجهة API (Swagger)</span><span class="icon">📚</span>
    </a></li>
    <li><a href="/health">
      <span>فحص الحالة</span><span class="icon">💚</span>
    </a></li>
    <li><a href="/openapi.json">
      <span>OpenAPI</span><span class="icon">📋</span>
    </a></li>
  </ul>
</div>
</body>
</html>
    """


# ---------- Page: Upload ----------
@app.get('/upload', response_class=HTMLResponse)
def upload_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>رفع السير الذاتية — Tunisia ATS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%);
       min-height:100vh;padding:16px;display:flex;justify-content:center}
  .wrap{background:#fff;border-radius:20px;padding:24px 20px;
        max-width:480px;width:100%;margin:auto;
        box-shadow:0 10px 40px rgba(30,64,175,.15)}
  h1{font-size:20px;color:#1e40af;text-align:center;margin-bottom:24px;
     line-height:1.4}
  .field{display:block;font-weight:600;font-size:14px;
         color:#374151;margin:16px 0 6px}
  input[type=text],input[type=password]{
    width:100%;padding:14px;font-size:16px;
    border:2px solid #e2e8f0;border-radius:12px;
    background:#f8fafc;transition:border .15s}
  input[type=text]:focus,input[type=password]:focus{
    outline:none;border-color:#3b82f6;background:#fff}
  .file-btn{display:flex;align-items:center;justify-content:center;
            padding:22px 14px;background:#f0f9ff;
            border:2px dashed #60a5fa;border-radius:14px;
            font-size:16px;font-weight:700;color:#1e40af;
            cursor:pointer;user-select:none;
            -webkit-tap-highlight-color:transparent;
            transition:background .15s}
  .file-btn:active{background:#dbeafe}
  input[type=file]{display:none}
  .hint{font-size:12px;color:#64748b;text-align:center;margin-top:8px}
  .file-list{margin-top:14px;padding:12px;background:#f0fdf4;
             border:2px solid #86efac;border-radius:12px;display:none}
  .file-list.show{display:block}
  .file-list .title{font-size:13px;color:#166534;
                    font-weight:700;margin-bottom:10px}
  .file-item{display:flex;justify-content:space-between;
             align-items:center;padding:8px 10px;
             background:#fff;border-radius:8px;margin-bottom:6px;
             font-size:13px;direction:ltr}
  .file-item:last-child{margin-bottom:0}
  .file-item .name{flex:1;overflow:hidden;text-overflow:ellipsis;
                   white-space:nowrap;color:#1e293b}
  .file-item .remove{color:#dc2626;font-weight:700;cursor:pointer;
                     padding:0 8px;font-size:18px;line-height:1}
  button.main{width:100%;padding:18px;font-size:17px;
              background:linear-gradient(135deg,#2563eb,#1d4ed8);
              color:#fff;border:none;border-radius:14px;
              font-weight:700;margin-top:20px;cursor:pointer;
              -webkit-tap-highlight-color:transparent;
              transition:transform .1s}
  button.main:active{transform:scale(.98)}
  button.main:disabled{background:#94a3b8;cursor:not-allowed}
  pre{background:#0f172a;color:#10b981;padding:14px;
      border-radius:12px;font-size:12px;overflow-x:auto;
      max-height:300px;white-space:pre-wrap;word-break:break-all;
      margin-top:8px;direction:ltr;text-align:left}
</style>
</head>
<body>
<div class="wrap">
  <h1>📄 رفع السير الذاتية<br><span style="font-size:15px;color:#64748b">Tunisia ATS</span></h1>

  <label class="field">معرّف الوظيفة (Job ID)</label>
  <input id="job_id" type="text" placeholder="c9d62982-45b2-..."
         autocomplete="off" autocapitalize="off" spellcheck="false">

  <label class="field">مفتاح API</label>
  <input id="api_key" type="password" placeholder="أدخل المفتاح" autocomplete="off">

  <label class="field">ملفات السير الذاتية (PDF, DOCX, TXT)</label>
  <label for="filePicker" class="file-btn">📎 اضغط لاختيار ملف</label>
  <input id="filePicker" type="file">
  <p class="hint">يمكنك إضافة عدة ملفات — اضغط الزر عدة مرات</p>

  <div class="file-list" id="fileList">
    <div class="title">✅ الملفات المختارة (<span id="count">0</span>)</div>
    <div id="items"></div>
  </div>

  <button class="main" id="uploadBtn" onclick="doUpload()">🚀 ارفع وقيّم</button>

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

async function doUpload(){
  var jid = cleanInput(document.getElementById('job_id').value);
  var key = cleanInput(document.getElementById('api_key').value);
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
  out.textContent = '⏳ جارٍ رفع ' + pickedFiles.length + ' ملف...';

  try{
    var r = await fetch('/api/jobs/' + encodeURIComponent(jid) + '/candidates', {
      method: 'POST',
      headers: {'x-api-key': key},
      body: fd
    });
    var d = await r.json();
    if(d.count > 0){
      out.textContent = '✅ تم رفع ' + d.count + ' ملف بنجاح!\\n⏳ جارٍ التحويل إلى صفحة النتائج...';
      setTimeout(function(){
        window.location.href = '/results/' + encodeURIComponent(jid) + '?x_api_key=' + encodeURIComponent(key);
      }, 1200);
    } else {
      out.textContent = JSON.stringify(d, null, 2);
      btn.disabled = false;
    }
  }catch(e){
    out.textContent = '❌ خطأ: ' + e.message;
    btn.disabled = false;
  }
}
</script>
</body>
</html>
    """


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
            '<h2 style="color:#dc2626">❌ الوظيفة غير موجودة</h2>'
            '<p style="color:#6b7280;margin-top:10px">تحقق من معرّف الوظيفة</p>'
            '<a href="/upload" style="display:inline-block;margin-top:20px;padding:12px 24px;'
            'background:#2563eb;color:#fff;text-decoration:none;border-radius:10px">'
            '🔄 العودة للرفع</a></div></body></html>',
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
            label = 'مؤهل — يوصى بمقابلته'
        elif status == 'review':
            review += 1
            color = '#f59e0b'; bg = '#fffbeb'; icon = '🤔'
            label = 'يحتاج مراجعة'
        else:
            reject += 1
            color = '#dc2626'; bg = '#fef2f2'; icon = '❌'
            label = 'مرفوض'

        missing = (r['missing'] or '').strip()
        if missing:
            missing_html = '<div class="missing">⚠️ ناقص: ' + missing.replace(',', '، ') + '</div>'
        else:
            missing_html = '<div class="missing ok">✅ جميع المهارات موجودة</div>'

        name = r['name'] or 'بدون اسم'
        email = r['email'] or '—'
        filename = r['filename'] or '—'

        rows_html += f'''
        <div class="card" style="background:{bg};border-right:6px solid {color}">
          <div class="rank" style="background:{color}">#{i}</div>
          <div class="name">{icon} {name}</div>
          <div class="score-row">
            <span class="score" style="color:{color}">{score}</span>
            <span class="score-label">/ 100</span>
          </div>
          <div class="status" style="color:{color}">{label}</div>
          <div class="meta">📧 {email}</div>
          <div class="meta">📄 {filename}</div>
          {missing_html}
        </div>'''

    if not rows:
        rows_html = '<div class="empty">لا يوجد مرشحون بعد لهذه الوظيفة</div>'

    title = job['title'] or 'وظيفة'
    must = job['must_have'] or '—'
    key_q = x_api_key or ''

    html = f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>النتائج — {title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%);
       min-height:100vh;padding:16px;line-height:1.5}}
  .wrap{{max-width:600px;margin:0 auto}}
  .header{{background:#fff;border-radius:20px;padding:22px;
          margin-bottom:16px;box-shadow:0 6px 24px rgba(30,64,175,.1)}}
  h1{{font-size:22px;color:#1e40af;text-align:center;margin-bottom:14px}}
  .job-title{{font-size:16px;color:#1e293b;font-weight:700;
             text-align:center;margin-bottom:6px}}
  .must-have{{font-size:13px;color:#64748b;text-align:center;
             margin-bottom:18px}}
  .stats{{display:flex;gap:8px}}
  .stat{{flex:1;text-align:center;padding:12px 6px;
        border-radius:12px;font-size:12px;font-weight:700}}
  .stat .num{{display:block;font-size:24px;margin-bottom:2px}}
  .stat.green{{background:#dcfce7;color:#166534}}
  .stat.yellow{{background:#fef3c7;color:#92400e}}
  .stat.red{{background:#fee2e2;color:#991b1b}}
  .card{{background:#fff;border-radius:16px;padding:18px;
        margin-bottom:12px;position:relative;
        box-shadow:0 2px 8px rgba(0,0,0,.05)}}
  .rank{{position:absolute;top:14px;left:14px;color:#fff;
        font-weight:800;font-size:13px;padding:4px 12px;
        border-radius:20px}}
  .name{{font-size:17px;font-weight:800;color:#0f172a;
        padding-left:55px;margin-bottom:8px}}
  .score-row{{margin:8px 0}}
  .score{{font-size:28px;font-weight:900}}
  .score-label{{font-size:13px;color:#94a3b8;margin-right:4px}}
  .status{{font-size:14px;font-weight:700;margin-bottom:10px}}
  .meta{{font-size:13px;color:#475569;margin-top:4px;
        word-break:break-all;direction:ltr;text-align:right}}
  .missing{{font-size:13px;color:#991b1b;margin-top:10px;
           padding:10px 12px;background:#fff;border-radius:8px;
           border-right:3px solid #dc2626}}
  .missing.ok{{color:#166534;border-right-color:#16a34a}}
  .empty{{text-align:center;padding:60px 20px;color:#64748b;
         background:#fff;border-radius:16px;font-size:15px}}
  .actions{{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}}
  .btn{{flex:1;min-width:140px;text-align:center;padding:16px 20px;
       border-radius:14px;text-decoration:none;font-weight:700;
       font-size:15px;transition:transform .1s;
       -webkit-tap-highlight-color:transparent}}
  .btn:active{{transform:scale(.97)}}
  .btn.green{{background:linear-gradient(135deg,#16a34a,#15803d);color:#fff}}
  .btn.blue{{background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>📊 نتائج فرز السير الذاتية</h1>
    <div class="job-title">💼 {title}</div>
    <div class="must-have">🎯 المهارات المطلوبة: {must}</div>
    <div class="stats">
      <div class="stat green"><span class="num">{shortlist}</span>مؤهل</div>
      <div class="stat yellow"><span class="num">{review}</span>مراجعة</div>
      <div class="stat red"><span class="num">{reject}</span>مرفوض</div>
    </div>
  </div>

  {rows_html}

  <div class="actions">
    <a class="btn green" href="/api/jobs/{job_id}/export.csv?x_api_key={key_q}">
      📥 تحميل Excel
    </a>
    <a class="btn blue" href="/upload">
      🔄 رفع ملفات جديدة
    </a>
  </div>
</div>
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
            str(i),
            esc(r['name']),
            esc(r['email']),
            esc(r['phone']),
            esc(r['filename']),
            str(r['score'] or 0),
            str(r['skill_score'] or 0),
            str(r['semantic_score'] or 0),
            str(r['experience_score'] or 0),
            esc(r['status']),
            esc(r['missing']),
        ]))

    csv = '\ufeff' + '\n'.join(lines)
    return HTMLResponse(
        content=csv,
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition':
                f'attachment; filename="results_{job_id[:8]}.csv"'
        }
    )


# ---------- OpenAPI fix for Swagger file upload ----------
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
