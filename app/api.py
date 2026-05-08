from __future__ import annotations

import asyncio
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import BrowserAgent
from .browser import BrowserSession
from .browser_fetch import fetch_with_stealth_browser
from .schemas import (
    APIResponse,
    ActionRequest,
    AgentRequest,
    AQLQueryRequest,
    ExtractRequest,
    QueryRequest,
    WorkflowRequest,
)
from .service import WebIntelService
from .orchestration.pool import SessionPool, SessionPoolConfig
from .orchestration.workflow import WorkflowExecutor, WorkflowStep

app = FastAPI(title="Open TinyFish / AgentQL-like API", version="5.0")

_TRUSTPILOT_FETCH_URL = (
    "https://www.trustpilot.com/api/consumersitesearch-api/businessunits/search"
)
_TRUSTPILOT_WARM_UP_URL = "https://www.trustpilot.com/"

# Serve the demo target page for local testing.
_STATIC_DIR = Path(__file__).parent / "static"

@app.get("/demo", response_class=HTMLResponse)
def demo_page():
    html_path = _STATIC_DIR / "demo_page.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Demo page not found")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Global session pool for concurrent operations (used for /sessions and /health).
_pool: SessionPool | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _model_to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, list):
        return [_model_to_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: _model_to_dict(v) for k, v in value.items()}
    return value


def get_pool() -> SessionPool:
    global _pool
    if _pool is None:
        _pool = SessionPool(
            SessionPoolConfig(
                min_sessions=_env_int("BROWSER_POOL_MIN", 1),
                max_sessions=_env_int("BROWSER_POOL_MAX", 4),
                max_age_seconds=_env_int("BROWSER_POOL_MAX_AGE", 900),
                headless=_env_bool("BROWSER_HEADLESS", True),
            )
        )
    return _pool


def get_service(use_llm: bool = True):
    """Create a fresh BrowserSession on the calling thread.

    Playwright's sync API uses greenlets pinned to the creating thread, so we
    cannot share sessions across uvicorn's threadpool.  Each request gets its
    own short-lived session which is closed after the request completes.
    """
    headless = _env_bool("BROWSER_HEADLESS", True)
    session = BrowserSession(headless=headless).start()
    service = WebIntelService(session, use_llm=use_llm)
    return None, service


# Playwright sync API cannot run inside an asyncio event loop.
# We use a thread pool to run all browser work in plain threads.
_browser_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pw")


async def _run_in_thread(fn):
    """Run a sync callable in a dedicated thread (outside asyncio loop)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_browser_executor, fn)


@app.post("/extract", response_model=APIResponse)
async def extract_data(request: ExtractRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            snapshot = service.open_page(request.url)

            if request.extraction_schema:
                data = service.extract_structured(schema=request.extraction_schema)
                return APIResponse(success=True, data=data, llm_used=service.use_llm)

            items = service.list_items()
            result = {
                "url": snapshot.url,
                "title": snapshot.title,
                "items": [_model_to_dict(item) for item in items[: request.max_items]],
            }

            if items:
                try:
                    service.open_item(0)
                    detail = service.extract_detail()
                    result["detail"] = _model_to_dict(detail)
                    service.go_back()
                except Exception as exc:
                    result["detail_error"] = str(exc)

            return APIResponse(success=True, data=result, llm_used=service.use_llm)
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.post("/query", response_model=APIResponse)
async def query_page(request: QueryRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            service.open_page(request.url)
            element = service.find_element(request.query)

            if element:
                return APIResponse(
                    success=True,
                    data={
                        "found": True,
                        "element": _model_to_dict(element),
                    },
                    llm_used=service.use_llm,
                )
            return APIResponse(success=True, data={"found": False}, llm_used=service.use_llm)
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.post("/aql", response_model=APIResponse)
async def aql_query(request: AQLQueryRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            service.open_page(request.url)
            result = service.query_aql(request.aql)
            return APIResponse(success=True, data=result, llm_used=service.use_llm)
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.post("/agent", response_model=APIResponse)
async def run_agent(request: AgentRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            agent = BrowserAgent(service)
            result = agent.run(
                url=request.url,
                task=request.task,
                max_steps=request.max_steps,
                schema=request.extraction_schema,
                screenshot_on_finish=request.screenshot,
            )
            return APIResponse(
                success=bool(result.get("success")),
                data=result,
                error=result.get("error"),
                screenshot=result.get("screenshot"),
                llm_used=service.use_llm,
            )
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.post("/workflow", response_model=APIResponse)
async def run_workflow(request: WorkflowRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            executor = WorkflowExecutor(service)
            steps = [WorkflowStep(**s) for s in request.steps]
            safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", request.url)[:80]
            result = executor.execute(
                workflow_id=f"wf_{safe_id}_{int(time.time())}",
                steps=steps,
                resume=False,
            )
            return APIResponse(
                success=result.success,
                data=_model_to_dict(result.__dict__),
                llm_used=service.use_llm,
            )
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.post("/fetch")
@app.get("/fetch")
async def fetch_url(
    request: Request,
    timeout_ms: int = Query(default=30000, ge=1, description="Navigation timeout in milliseconds."),
    wait_until: str = Query(default="domcontentloaded", description="Playwright navigation wait condition."),
):
    reserved_keys = {"timeout_ms", "wait_until"}
    params: dict[str, str | list[str]] = {}
    for key in request.query_params.keys():
        if key in reserved_keys:
            continue
        values = request.query_params.getlist(key)
        params[key] = values if len(values) > 1 else values[0]

    def _work():
        return fetch_with_stealth_browser(
            url=_TRUSTPILOT_FETCH_URL,
            params=params,
            warm_up_url=_TRUSTPILOT_WARM_UP_URL,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
        )

    try:
        result = await _run_in_thread(_work)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
    )


@app.post("/action", response_model=APIResponse)
async def perform_action(request: ActionRequest):
    def _work():
        service = None
        try:
            _, service = get_service(use_llm=request.use_llm)
            service.open_page(request.url)

            if request.action == "click":
                result = service.click_element(request.target or "")
                return APIResponse(success=True, data=result, llm_used=service.use_llm)

            if request.action == "type":
                result = service.type_text(request.target or "", request.value or "")
                return APIResponse(success=True, data=result, llm_used=service.use_llm)

            if request.action == "scroll":
                service.session.human_like_scroll(int(request.value or 800))
                return APIResponse(success=True, data={"scrolled": True}, llm_used=service.use_llm)

            if request.action == "extract":
                return APIResponse(
                    success=True,
                    data=service.extract_structured(),
                    llm_used=service.use_llm,
                )

            return APIResponse(success=False, error=f"Unknown action: {request.action}")
        except Exception as exc:
            return APIResponse(success=False, error=str(exc))
        finally:
            if service:
                try: service.session.close()
                except Exception: pass

    return await _run_in_thread(_work)


@app.get("/screenshot")
def get_screenshot(path: str = "screenshot.png"):
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Screenshot not found")


@app.get("/llm/status")
def llm_status():
    from .llm import groq_available, llm_available, ollama_available
    from .llm.groq_client import get_default_groq_client

    status = {
        "llm_available": llm_available(),
        "groq_available": groq_available(),
        "ollama_available": ollama_available(),
    }
    try:
        status["groq"] = get_default_groq_client().status()
    except Exception as exc:
        status["groq_error"] = str(exc)
    return status


@app.get("/health")
def health_check():
    from .llm import llm_available

    return {
        "status": "ok",
        "version": "5.0",
        "llm_available": llm_available(),
        "pool_sessions": len(_pool.list_sessions()) if _pool else 0,
    }


@app.get("/sessions")
def list_sessions():
    return get_pool().list_sessions()


@app.on_event("shutdown")
def shutdown():
    global _pool
    if _pool:
        _pool.shutdown()
        _pool = None
