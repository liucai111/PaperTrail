# PaperTrail

PaperTrail is a local-first academic paper reading and evidence workbench built with Vue 3, FastAPI, PyMuPDF and PDF.js. This public release is an empty-corpus demo: no private PDFs, databases, upload files, questions, Gold answers, qrels, or raw model traces are included.

## Quick start (Windows)

Requirements: Python 3.13 and Node.js/npm. From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r apps\api\requirements-dev.txt
Push-Location apps\web
npm.cmd ci
Pop-Location
Copy-Item .env.example .env
scripts\papertrail.cmd doctor
scripts\papertrail.cmd start
```

The local API uses SQLite under ignored `var/`; the public demo manifest contains no bundled papers. The UI and API can start without model credentials. Live PDF indexing and RAG calls require the user to provide separate `PAPERTRAIL_EMBEDDING_API_KEY` and `PAPERTRAIL_LLM_API_KEY` values in `.env`. Never commit credentials or personal PDFs.

## Mock and build checks

```powershell
Push-Location apps\web
npm.cmd run build
node --test tests\answerMarkdown.test.mjs tests\answerPresentation.test.mjs tests\pdfReader.test.mjs tests\workspacePreferences.test.mjs
Pop-Location
$env:PYTHONPATH = 'apps/api'
python -m pytest -q tests\test_provider_configuration.py tests\test_user_upload.py
node --test tests\qa_submission.test.mjs tests\upload_submission.test.mjs
```

The backend tests use local temporary databases and mocked providers/synthetic PDFs; they do not require API keys or call external model services. See [deployment](docs/deployment.md), [architecture](docs/architecture.md), and [public aggregate experiment results](docs/public-experiment-results.md).

## Security and data boundaries

- `.env`, `var/`, SQLite databases, uploads, PDFs, runtime logs and evaluation data are ignored by Git.
- Gold, qrels, questions and raw experiments are not part of this release. The results page reads only aggregate JSON under `public-data/`.
- `local-hash-v1` is a lexical baseline, not a semantic embedding model. Hosted embedding and Chat services are separately configured.
- LLM Judge values are automated scores, not human evaluation; protocols differ between stages. See the public experiment summary for limitations.

## License

No project license is included in this prepared release. Unless the owner adds a license, publication does not grant reuse rights.
