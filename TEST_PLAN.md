# Acceptance Test Plan
1. Create a job with 5 must-have skills.
2. Upload 10 mixed PDF/DOCX/TXT CVs in one request.
3. Verify all readable files are persisted in SQLite.
4. Verify ranked results contain score components and missing skills.
5. Verify status rules: shortlist >=70, review 50–69.9, reject <50.
6. Search candidate CVthèque by a skill/term.
7. Call the results endpoint with and without a valid API key.
8. Verify invalid file types return HTTP 400.
9. Verify unknown job returns HTTP 404.
10. Verify /health returns status=ok.
