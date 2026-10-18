# Deployment and local run

This repository is a local single-user development application. Install Python 3.13 and Node.js/npm, create `.venv`, install `apps/api/requirements-dev.txt`, run `npm ci` in `apps/web`, then start with `scripts/papertrail.cmd start`. `scripts/papertrail.cmd doctor` performs local environment checks. The launcher uses loopback ports and the checked-in public config.

The app creates its SQLite database and runtime files only under ignored `var/`. A clean checkout starts with an empty public manifest and no PDF corpus. UI, API health checks and Mock tests work without provider credentials. To enable live indexing/QA, set separate embedding and Chat API keys in a local `.env`; do not expose them in command history, logs, screenshots or commits.

Optional OCR dependencies are intentionally not included in this release build. The default parser uses PyMuPDF; use the project's documented pinned OCR setup only after reviewing its model/resource requirements.
