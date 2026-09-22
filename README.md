# Tunisia ATS Pro — Client Demo v1.0

A working MVP aligned to the recruitment request: CVthèque ingestion, bulk CV import, parsing, job creation, matching, multi-factor scoring, ranking, recruiter statuses, search, SQLite persistence and REST API.

## Run
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
Open `http://localhost:8000`.

## API
- `POST /api/jobs` create a job
- `POST /api/jobs/{job_id}/candidates` bulk CV import + ranking
- `GET /api/jobs/{job_id}/results` ranked candidates
- `GET /api/candidates/search?q=python` candidate search
- `GET /health`

Set `ATS_API_KEY` in production. If supplied, clients send `X-API-Key`.

## What is deliberately not claimed
This is a client-demo MVP, not a production hiring decision system. Scores are explainable matching signals, not a guarantee of candidate suitability. Human review remains required.

## Next production hardening
OCR for scanned CVs, object storage, background queues, tenant isolation, encryption, rate limits, consent/retention controls, stronger semantic embeddings/local LLM, integrations with job boards, audit export, automated tests and deployment.
