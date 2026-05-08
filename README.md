# Open TinyFish / AgentQL-like Browser Agent

A fully open-source Python + Playwright browser automation service that can open live web pages, inspect DOM elements, click, type, scroll, extract structured data, and run small autonomous browser workflows.

It supports:

- **Groq-hosted open models** through an OpenAI-compatible HTTP client.
- **Multiple Groq API keys** with local RPM/TPM/RPD/TPD tracking, failover on `429`, and `retry-after`/rate-limit header handling.
- **Ollama local/cloud models** as a fallback or primary provider.
- **AgentQL-like queries** for structured extraction and element lookup.
- **Browser-backed raw fetch passthrough** for dynamic URLs and query parameters via `/fetch`.
- **FastAPI server** endpoints for `/agent`, `/extract`, `/query`, `/aql`, `/workflow`, and `/action`.
- **Playwright browser control** for live websites.

> Important: Groq rate limits are usually organization/project scoped. Multiple keys from the same Groq project may share the same upstream quota. The included client still rotates keys and tracks each key locally, but it cannot magically increase a shared provider quota.

---

## 1. Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium

# Optional alternative if your machine already has Chromium:
# export CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

cp .env.example .env
```

Edit `.env`:

```env
LLM_PROVIDER=auto
GROQ_API_KEYS=your_first_groq_key,your_second_groq_key
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_RPM=30
GROQ_TPM=6000
GROQ_RPD=14400
GROQ_TPD=500000

# Optional local fallback
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

For Ollama only:

```bash
ollama pull llama3.2
# .env
LLM_PROVIDER=ollama
```

---

## 2. Run the API

```bash
python run_api.py
```

Open docs:

```text
http://localhost:8000/docs
```

Health checks:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/llm/status
```

---

## 3. Autonomous browser agent

The `/agent` endpoint opens a URL, snapshots the live page, asks Groq/Ollama for the next browser action, performs the action, and repeats until extraction or completion.

```bash
curl -X POST http://localhost:8000/agent \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "task": "extract the page title and visible navigation links",
    "max_steps": 5,
    "schema": {
      "title": "string",
      "links": [{"text": "string", "url": "string"}]
    },
    "use_llm": true
  }'
```

Allowed autonomous actions are intentionally small and safe:

```text
click, type, scroll, wait, extract, done
```

Potentially destructive actions such as purchase, payment, delete account, password change, money transfer, etc. are blocked by the agent loop.

---

## 4. Direct actions

Click something by natural language:

```bash
curl -X POST http://localhost:8000/action \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "action": "click",
    "target": "More information",
    "use_llm": true
  }'
```

Type into an input:

```bash
curl -X POST http://localhost:8000/action \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.google.com/search?q=playwright",
    "action": "type",
    "target": "search input",
    "value": "open source browser automation",
    "use_llm": true
  }'
```

Find an element without clicking:

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "query": "main documentation link",
    "use_llm": true
  }'
```

---

## 5. Structured extraction

Schema-based extraction:

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "schema": {
      "page_title": "string",
      "main_heading": "string",
      "links": [{"text": "string", "href": "string"}]
    },
    "use_llm": true
  }'
```

Simple AQL-like query:

```bash
curl -X POST http://localhost:8000/aql \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://boards.greenhouse.io/embed/job_board?for=airbyte",
    "aql": "jobs[] { title, company, location, salary }",
    "use_llm": true
  }'
```

---

## 6. Workflow API

For deterministic multi-step workflows:

```bash
curl -X POST http://localhost:8000/workflow \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "steps": [
      {"name": "open", "action": "open", "target": "https://example.com", "checkpoint": true},
      {"name": "find_more", "action": "find", "target": "More information"},
      {"name": "extract", "action": "extract"}
    ],
    "use_llm": true
  }'
```

Supported workflow actions:

```text
open, click, type, scroll, wait, extract, screenshot, find, aql
```

---

## 7. Raw browser fetch

Use `/fetch` when you want the Trustpilot upstream response body returned without TinyFish reshaping it.
Only the query-string parameters are dynamic; the backend keeps the Trustpilot endpoint fixed.

```bash
curl "http://localhost:8000/fetch?country=US&page=1&pageSize=100&query=openai"
```

Any of those parameters can change per request:

```bash
curl "http://localhost:8000/fetch?country=GB&page=2&pageSize=20&query=notion"
```

Optional query parameters:

- `timeout_ms`: navigation timeout for warm-up and fetch.
- `wait_until`: Playwright navigation wait condition.

---

## 8. CLI usage

```bash
python -m app.main \
  --headless \
  --url "https://example.com" \
  --task "extract the title and all obvious links" \
  --schema '{"title":"string","links":[{"text":"string","url":"string"}]}'
```

---

## 9. Groq multi-key rate-limit client

The single file `app/llm/groq_client.py` handles Groq requests.

What it does:

- Loads keys from `GROQ_API_KEYS`, `GROQ_API_KEY`, and `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, etc.
- Estimates prompt + output tokens before sending a request.
- Tracks local request and token usage per minute and per UTC day.
- Rotates to the next available key when one key is locally exhausted.
- On Groq `429`, reads `retry-after`, `x-ratelimit-reset-tokens`, and `x-ratelimit-reset-requests`, cools down that key, then tries another key.
- Reads Groq rate-limit headers and updates local TPM/RPD limits when headers are available.
- Exposes status at `/llm/status` without leaking full API keys.

Use directly in Python:

```python
from app.llm.groq_client import GroqRouterClient

client = GroqRouterClient(api_keys=["your_first_groq_key", "your_second_groq_key"], model="llama-3.3-70b-versatile")
response = client.chat([
    {"role": "system", "content": "Return concise JSON."},
    {"role": "user", "content": "Extract the title from: <h1>Hello</h1>"},
])
print(response.content)
print(client.status())
```

---

## 10. Project layout

```text
app/
  agent.py              Autonomous browser task loop
  api.py                FastAPI endpoints
  browser.py            Playwright session wrapper
  dom.py                DOM snapshot + temporary selector annotation
  service.py            Main orchestration service
  llm/
    groq_client.py      Multi-key Groq client and rate limiter
    semantic.py         Groq/Ollama semantic matching and extraction
  parser/aql_parser.py  Small AQL-like parser
  orchestration/        Session pool and workflow executor
  robust/               Retry, healing, state recovery
  adapters/             Site-specific extraction adapters
```

---

## 10. Notes and limitations

- This is not the proprietary AgentQL implementation. It is an open-source approximation built on Playwright plus Groq/Ollama.
- It works best on normal websites with visible text, accessible labels, buttons, links, and inputs.
- Some websites use bot protection, CAPTCHAs, aggressive dynamic rendering, or terms that prohibit automation. Respect each website’s terms and robots/access policies.
- For production, add persistent job queues, auth, audit logs, domain allowlists, proxy/session configuration, and stronger schema validation.
