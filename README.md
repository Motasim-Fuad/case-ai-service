# Case AI Service (FastAPI) — Document Intelligence + RAG

AI Case Assistant-এর **AI ও document processing service**। NestJS backend এই service-কে call করে; user-এর সাথে সরাসরি কথা বলে না।

> User file upload করলে → NestJS S3-তে রাখে → এই service-কে জানায় → এখানে OCR + AI extraction + vector indexing হয় → structured result (তারিখ, amount, party, deadline, summary…) NestJS-এ ফেরত যায় → Timeline তৈরি হয়।

| কী কাজ করে | কিভাবে |
|---|---|
| Text PDF, scanned PDF, mixed PDF, JPG/PNG/TIFF, DOCX, TXT | file-এর bytes দেখে type ধরে, **page ধরে ধরে** text/OCR ঠিক করে |
| Structured extraction | OpenAI Structured Outputs + Pydantic validation, প্রতিটা তথ্যের সাথে page + verbatim quote |
| Summary + date/amount/party/deadline/request | `DocumentExtractionResult` schema (`docs/openapi.json`) |
| RAG / semantic search | Qdrant, সবসময় `case_id` দিয়ে filter |
| Async job | Celery + Redis, retry, resume, webhook + polling |

পুরো design বুঝতে: **`docs/ARCHITECTURE.md`**। NestJS/Next.js developer-দের জন্য: **`docs/INTEGRATION_GUIDE.md`**।

---

## 1) সবচেয়ে সহজ: কোনো account/Docker ছাড়া চালানো (২ মিনিট)

Python 3.11+ লাগবে।

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

python -m scripts.demo_local samples/sample_insurance_letter.pdf --ask "When are the photographs due?"
python -m pytest -q                  # সব test (৭০+)
```

`demo_local` পুরো pipeline in-memory চালায় (fake Redis + in-memory Qdrant)। এটা **MOCK_AI** mode: OpenAI ছাড়া regex দিয়ে কাজ চালায়, শুধু flow যাচাইয়ের জন্য। আসল AI-র মান বুঝতে §3 দেখুন।

## 2) Docker দিয়ে পুরো stack (API + Worker + Redis + Qdrant)

```bash
cp .env.example .env                 # প্রথমবার এটাই যথেষ্ট (MOCK_AI=true, local storage)
docker compose up --build
```

- Swagger UI: http://localhost:8000/docs → **Authorize** চাপুন → `dev-internal-key`
- Health: http://localhost:8000/health (Redis + Qdrant check)
- Qdrant dashboard: http://localhost:6333/dashboard

**Swagger থেকে test করার নিয়ম**
1. `POST /api/v1/dev/process-upload` → একটা file দিন (যেমন `samples/sample_insurance_letter.pdf`), `case_id` দিন → `job_id` পাবেন
2. `GET /api/v1/documents/jobs/{job_id}` → `status`, `progress`, `result`
3. `GET /api/v1/dev/webhook-echo` → NestJS আসলে যে payload পাবে সেটা এখানে দেখুন
4. `POST /api/v1/case/{case_id}/query` → RAG প্রশ্ন
5. VS Code-এ `requests.http` (REST Client extension) খুলে এক ক্লিকে সব call করা যায়

Hot reload: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`
Worker বাড়াতে: `docker compose up --scale worker=3` · Celery monitor: `docker compose --profile monitoring up` (http://localhost:5555)

## 3) আসল AI দিয়ে চালানো (Live mode)

`.env` এ:

```ini
MOCK_AI=false
OPENAI_API_KEY=sk-...
OPENAI_EXTRACTION_MODEL=gpt-4.1-mini     # Structured Outputs support করে এমন যেকোনো current model
# scanned PDF/ছবির OCR এর জন্য AWS লাগবে:
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
```

তারপর `python -m scripts.demo_local <আপনার file> --live` অথবা docker restart। Text PDF/DOCX/TXT-এর জন্য AWS লাগে না, শুধু scanned PDF ও ছবির জন্য।

> Model নামগুলো OpenAI-র current তালিকা থেকে যাচাই করে নিন (`.env`-এ বদলানো যায়, code বদলাতে হয় না)। Embedding model বদলালে Qdrant collection নতুন করতে হবে (`QDRANT_COLLECTION` বদলান), নইলে service নিজেই পরিষ্কার error দেবে।

### Production-এ S3 ব্যবহার

`STORAGE_BACKEND=s3`, `S3_BUCKET_NAME=...` এবং `INTERNAL_API_KEY` (লম্বা random secret: production-এ default থাকলে service start-ই হবে না), `CALLBACK_ALLOWED_HOSTS=<NestJS host>`, `ENVIRONMENT=production` (dev endpoints তখন আপনাআপনি বন্ধ)।

**দরকারি AWS IAM permission** (client-এর account-এ user/role):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::YOUR_BUCKET/*" },
    { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::YOUR_BUCKET" },
    { "Effect": "Allow",
      "Action": ["textract:DetectDocumentText", "textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"],
      "Resource": "*" }
  ]
}
```
Bucket-এ SSE-KMS থাকলে `kms:Decrypt`-ও লাগবে। এই service S3-তে **কিছু লেখে না**, শুধু পড়ে।

## 4) VS Code-এ debug

`.vscode/launch.json` এ তৈরি আছে: **API (uvicorn --reload)**, **Celery worker**, **Demo script**, **Pytest**। শুধু Redis + Qdrant Docker-এ চালাতে চাইলে:

```bash
docker compose up redis qdrant -d
# .env এ REDIS_URL/QDRANT_URL localhost রাখুন, তারপর VS Code থেকে API + Worker চালান
```
Windows-এ Celery-র `prefork` pool চলে না, তাই launch config-এ `--pool=solo` দেওয়া আছে।

## 5) API এক নজরে (সব `X-Internal-Api-Key` header লাগে, `/health` ছাড়া)

| Method | Path | কাজ |
|---|---|---|
| POST | `/api/v1/documents/process` | Job queue-তে দেয় → `202 {job_id}` |
| GET | `/api/v1/documents/jobs/{job_id}` | Status/progress/result (webhook miss হলে fallback) |
| DELETE | `/api/v1/documents/{document_id}/index` | Document মুছলে তার vectors মুছুন |
| POST | `/api/v1/case/{case_id}/query` | RAG: প্রশ্ন → উত্তর + source page |
| POST | `/api/v1/case/{case_id}/search` | Semantic search (LLM ছাড়া) |
| DELETE | `/api/v1/case/{case_id}/index` | Case মুছলে সব vectors মুছুন |
| GET | `/health` | Redis + Qdrant check |
| — | `/api/v1/dev/*` | শুধু development: file upload, webhook echo, page text দেখা |

সম্পূর্ণ contract: `docs/openapi.json` (NestJS developer এটা দিয়ে TypeScript client generate করতে পারবে) ও `docs/examples/*.json`।

## 6) Folder structure

```
app/
  main.py                    FastAPI app factory
  api/routes/                documents, query (RAG), health, dev
  core/                      config, logging, errors, security
  models/                    api.py (HTTP/webhook), extraction.py (AI schema)
  services/
    pipeline.py              পুরো flow (fetch → pages → index || extract → webhook)
    page_extractor.py        text vs scanned, PyMuPDF/pdfplumber/OCR routing
    textract.py              AWS Textract (sync + async)
    extraction.py            windows → GPT → grounding → merge/dedupe
    chunking.py, embeddings.py, vector_store.py (Qdrant), rag.py
    storage.py (S3/local), file_sniffer.py, webhook.py, llm.py
  jobs/job_store.py          Redis: job state, stage cache, per-document lock
  workers/                   Celery app + task (retry policy)
tests/                       70+ tests
scripts/                     demo_local.py, make_samples.py
docs/                        ARCHITECTURE.md, INTEGRATION_GUIDE.md, openapi.json, examples/
```

## 7) সমস্যা হলে

| সমস্যা | সমাধান |
|---|---|
| `NO_READABLE_TEXT ... OCR_UNAVAILABLE` | Scanned file, কিন্তু AWS key নেই/ভুল → `.env`-এ AWS key + IAM permission |
| `LLM_CONFIG_ERROR` | `OPENAI_API_KEY` বা model নাম ভুল |
| `VECTOR_DIM_MISMATCH` | Embedding model বদলেছেন → নতুন `QDRANT_COLLECTION` দিন |
| Job `queued` থেকে নড়ছে না | Worker চলছে না: `docker compose logs worker` |
| Webhook `callback_status: failed` | NestJS URL/API key ঠিক আছে কিনা দেখুন; result Redis-এ আছে, `GET /jobs/{id}` দিয়ে নেওয়া যাবে |
| Log পড়তে কষ্ট | `.env`: `LOG_JSON=false` |
