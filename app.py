from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pypdf import PdfReader
from docx import Document
import sqlite3, io, os, re, uuid, math, hashlib, json
from datetime import datetime

DB='ats.db'
API_KEY=os.getenv('ATS_API_KEY','demo-change-me')
app=FastAPI(title='Tunisia ATS Pro', version='1.0')

SCHEMA='''
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,title TEXT,description TEXT,must_have TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,filename TEXT,name TEXT,email TEXT,phone TEXT,location TEXT,skills TEXT,education TEXT,experience TEXT,raw_text TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY,job_id TEXT,candidate_id TEXT,score REAL,skill_score REAL,semantic_score REAL,experience_score REAL,missing TEXT,status TEXT,explanation TEXT,created_at TEXT,UNIQUE(job_id,candidate_id));
CREATE TABLE IF NOT EXISTS api_keys(id INTEGER PRIMARY KEY,key_hash TEXT UNIQUE,created_at TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT,detail TEXT,created_at TEXT);
'''

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.executescript(SCHEMA); return c

def audit(event,detail):
    c=db(); c.execute('INSERT INTO audit(event,detail,created_at) VALUES(?,?,?)',(event,detail,datetime.utcnow().isoformat())); c.commit(); c.close()

def tokens(s): return set(re.findall(r'[\wÀ-ÿ+#.-]{2,}',(s or '').lower()))

def extract_text(name,data):
    ext=os.path.splitext(name.lower())[1]
    if ext=='.pdf':
        r=PdfReader(io.BytesIO(data)); return '\n'.join((p.extract_text() or '') for p in r.pages)
    if ext=='.docx':
        d=Document(io.BytesIO(data)); return '\n'.join(p.text for p in d.paragraphs)
    if ext in ('.txt','.md'): return data.decode('utf-8','ignore')
    raise ValueError('PDF, DOCX and TXT are supported in this demo.')

def first(pattern,text):
    m=re.search(pattern,text,re.I|re.M); return m.group(1).strip() if m else ''

def parse_candidate(text):
    email=first(r'([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})',text)
    phone=first(r'((?:\+?216[ .-]?)?(?:\d[ .-]?){8})',text)
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    name=lines[0][:120] if lines else 'Unknown candidate'
    skills=[]
    common=['python','java','javascript','typescript','react','flutter','sql','excel','word','powerpoint','fastapi','docker','git','linux','ai','machine learning','recruitment','sales','marketing','accounting','mechanic','automotive','english','french','arabic']
    low=text.lower()
    for s in common:
        if s in low: skills.append(s)
    return name,email,phone,', '.join(skills),'', ''

def cosine(a,b):
    A=tokens(a); B=tokens(b)
    if not A or not B:return 0
    return len(A&B)/math.sqrt(len(A)*len(B))*100

def match(job,cand):
    jd=job['description']; must=tokens(job['must_have']); cv=tokens(cand['raw_text'])
    common=must & cv
    skill_score=100*len(common)/max(1,len(must)) if must else 100
    semantic=cosine(jd,cand['raw_text'])
    # experience proxy: explicit year mentions
    years=[int(x) for x in re.findall(r'(\d{1,2})\s*(?:years?|ans|سنوات)',cand['raw_text'],re.I)]
    exp=min(100,max(years)*10) if years else 50
    score=round(skill_score*.50+semantic*.35+exp*.15,1)
    missing=sorted(must-cv)
    explanation=f'{len(common)}/{len(must) if must else 0} must-have skills matched; semantic similarity {semantic:.1f}%; experience signal {exp:.0f}%.'
    return score,skill_score,semantic,exp,missing,explanation

class Job(BaseModel): title:str; description:str; must_have:str=''

def auth(x_api_key):
    if x_api_key is None: return
    if x_api_key != API_KEY: raise HTTPException(401,'Invalid API key')

@app@app.on_event('startup')
def startup():
    db().close()


@app.post('/api/jobs/{job_id}/candidates')
async def candidates(
    job_id: str,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)

    c = db()
    job = c.execute(
        'SELECT * FROM jobs WHERE id=?',
        (job_id,)
    ).fetchone()

    if not job:
        c.close()
        raise HTTPException(404, 'Job not found')

    ranked = []

    for f in files:
        data = await f.read()

        try:
            text = extract_text(f.filename, data)
        except ValueError as e:
            c.close()
            raise HTTPException(400, str(e))
        except Exception:
            c.close()
            raise HTTPException(
                400,
                f'Could not read file: {f.filename}'
            )

        name, email, phone, skills, education, experience = parse_candidate(text)

        cid = str(uuid.uuid4())

        c.execute(
            'INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (
                cid,
                f.filename,
                name,
                email,
                phone,
                '',
                skills,
                education,
                experience,
                text,
                datetime.utcnow().isoformat()
            )
        )

        cand = {'raw_text': text}

        score, ss, sem, ex, missing, explain = match(
            job,
            cand
        )

        status = (
            'shortlist'
            if score >= 70
            else 'review'
            if score >= 50
            else 'reject'
        )

        aid = str(uuid.uuid4())

        c.execute(
            'INSERT INTO applications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (
                aid,
                job_id,
                cid,
                score,
                ss,
                sem,
                ex,
                ','.join(missing),
                status,
                explain,
                datetime.utcnow().isoformat()
            )
        )

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

    c.commit()
    c.close()

    ranked.sort(
        key=lambda x: x['score'],
        reverse=True
    )

    audit(
        'bulk_import',
        f'{job_id}:{len(ranked)}'
    )

    return {
        'job_id': job_id,
        'count': len(ranked),
        'files_received': [
            x['filename'] for x in ranked
        ],
        'ranked': ranked
    }
    c.commit(); c.close(); ranked.sort(key=lambda x:x['score'],reverse=True); audit('bulk_import',f'{job_id}:{len(ranked)}'); return {'job_id':job_id,'count':len(ranked),'files_received':[x['filename'] for x in ranked],'ranked':ranked}

@app.get('/api/jobs/{job_id}/results')
def results(job_id:str,x_api_key:str|None=Header(default=None)):
    auth(x_api_key); c=db(); rows=c.execute('''SELECT a.*,c.name,c.email,c.filename FROM applications a JOIN candidates c ON c.id=a.candidate_id WHERE a.job_id=? ORDER BY a.score DESC''',(job_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.get('/api/candidates/search')
def search(q:str,x_api_key:str|None=Header(default=None)):
    auth(x_api_key); c=db(); rows=c.execute('SELECT id,name,email,filename,skills FROM candidates WHERE raw_text LIKE ? LIMIT 100',('%'+q+'%',)).fetchall(); c.close(); return [dict(r) for r in rows]
