# Invoice Processor UI

Modern React control center for the Python-based invoice automation found in `main.py`.  
The UI mirrors the visual language from the provided design reference and surfaces the same execution flow: Gmail ingestion, Bedrock enrichment, Drive uploads, and Excel reconciliation.

## 1. Backend API

Expose the existing Python workflow via FastAPI without modifying `main.py`.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Optional environment variables:

| Variable         | Default  | Purpose                                       |
|------------------|----------|-----------------------------------------------|
| `API_HOST`       | 0.0.0.0  | Override the host FastAPI binds to            |
| `API_PORT`       | 8000     | Override the API port                         |
| `API_RELOAD`     | false    | Enable Hot Reload for development             |
| `CORS_ALLOW_ORIGINS` | *  | Comma-separated list for allowed CORS origins |

## 2. Frontend application

```bash
cd frontend
npm install
npm run dev
```

Set the API base URL for the UI with Vite environment variables:

- Create `.env.local` inside `frontend/`
- Add `VITE_API_BASE_URL=http://localhost:8000`

## 3. Available commands

| Location  | Command             | Description                               |
|-----------|---------------------|-------------------------------------------|
| Frontend  | `npm run dev`       | Start React dev server (default :5173)    |
| Frontend  | `npm run build`     | Production build                          |
| Frontend  | `npm run preview`   | Preview production build                  |
| Backend   | `uvicorn api_server:app` | Launch API in front of `main.py`    |

## 4. Workflow from the UI

1. **Run Full Flow** – triggers `process_invoices()` from `main.py` via the API.
2. **Sync Master Sheet** – triggers `match_master_sheet_with_excel()` for reconciliation.
3. Live status cards summarise unread emails, invoices processed, and Excel updates.
4. Activity timeline and log viewer provide real-time progress and diagnostics.
5. Toast notifications surface success and failure states for faster feedback.

The UI stays read-only with respect to configuration; environment and credential management continue to reside in `.env` and the existing Python scripts.

