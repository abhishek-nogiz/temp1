"""
TinyFish2 End-to-End Demo Test
Run the API first:  python run_api.py
Then:               python test_demo.py
"""
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
DEMO = f"{BASE}/demo"
TIMEOUT = 90.0  # LLM + Playwright can be slow

PASS = 0
FAIL = 0


def _log(icon, name, detail=""):
    print(f"  {icon}  {name}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"      {line}")


def _pass(name, detail=""):
    global PASS
    PASS += 1
    _log("[PASS]", name, detail)


def _fail(name, detail=""):
    global FAIL
    FAIL += 1
    _log("[FAIL]", name, detail)


def check_health():
    print("\n-- Health Check --")
    try:
        r = httpx.get(f"{BASE}/health", timeout=10)
        data = r.json()
        _pass(f"API is up - v{data.get('version')}, LLM={data.get('llm_available')}")
        return True
    except Exception as e:
        _fail(f"API unreachable: {e}")
        return False


def check_demo_page():
    print("\n-- Demo Page --")
    try:
        r = httpx.get(DEMO, timeout=10)
        if r.status_code == 200 and "TechHire" in r.text:
            _pass(f"Demo page served ({len(r.text)} bytes)")
        else:
            _fail(f"Demo page returned {r.status_code}")
    except Exception as e:
        _fail(f"Demo page error: {e}")


def test_query_login_button():
    print("\n-- Semantic Query: 'login button' --")
    try:
        r = httpx.post(f"{BASE}/query", json={
            "url": DEMO,
            "query": "login button",
            "use_llm": True,
        }, timeout=TIMEOUT)
        data = r.json()
        if data.get("success") and data.get("data", {}).get("found"):
            el = data["data"]["element"]
            _pass(f"Found: '{el.get('text', '')[:80]}' (tag={el.get('tag')}, score={el.get('score')})")
        else:
            _fail(f"Not found - {json.dumps(data, indent=2)[:300]}")
    except Exception as e:
        _fail(f"Error: {e}")


def test_query_search_input():
    print("\n-- Semantic Query: 'search input' --")
    try:
        r = httpx.post(f"{BASE}/query", json={
            "url": DEMO,
            "query": "search input",
            "use_llm": True,
        }, timeout=TIMEOUT)
        data = r.json()
        if data.get("success") and data.get("data", {}).get("found"):
            el = data["data"]["element"]
            _pass(f"Found: '{el.get('text', '')[:80]}' (tag={el.get('tag')})")
        else:
            _fail(f"Not found - {json.dumps(data, indent=2)[:300]}")
    except Exception as e:
        _fail(f"Error: {e}")


def test_extract_jobs():
    print("\n-- Structured Extract: job listings --")
    try:
        r = httpx.post(f"{BASE}/extract", json={
            "url": DEMO,
            "schema": {
                "jobs": [{"title": "string", "company": "string", "location": "string", "salary": "string"}]
            },
            "use_llm": True,
        }, timeout=TIMEOUT)
        data = r.json()
        if data.get("success"):
            jobs = data.get("data", {}).get("jobs", [])
            if jobs:
                _pass(f"Extracted {len(jobs)} jobs")
                for j in jobs[:3]:
                    print(f"      > {j.get('title', '?')} @ {j.get('company', '?')} - {j.get('salary', '?')}")
            else:
                _pass(f"Extract succeeded but no 'jobs' key - keys: {list(data.get('data', {}).keys())[:10]}")
        else:
            _fail(f"Extract failed: {data.get('error', '')[:200]}")
    except Exception as e:
        _fail(f"Error: {e}")


def test_action_type():
    print("\n-- Action: type into search --")
    try:
        r = httpx.post(f"{BASE}/action", json={
            "url": DEMO,
            "action": "type",
            "target": "search input",
            "value": "python developer",
            "use_llm": True,
        }, timeout=TIMEOUT)
        data = r.json()
        if data.get("success"):
            _pass(f"Typed 'python developer' into: {data.get('data', {}).get('into', '?')}")
        else:
            _fail(f"Type failed: {data.get('error', '')[:200]}")
    except Exception as e:
        _fail(f"Error: {e}")


def test_action_click():
    print("\n-- Action: click 'Apply Now' --")
    try:
        r = httpx.post(f"{BASE}/action", json={
            "url": DEMO,
            "action": "click",
            "target": "Apply Now",
            "use_llm": True,
        }, timeout=TIMEOUT)
        data = r.json()
        if data.get("success"):
            result = data.get("data", {})
            _pass(f"Clicked: '{result.get('clicked', '?')}' -> {result.get('url', '?')[:80]}")
        else:
            _fail(f"Click failed: {data.get('error', '')[:200]}")
    except Exception as e:
        _fail(f"Error: {e}")


def test_agent():
    print("\n-- Agent: extract all job titles --")
    try:
        r = httpx.post(f"{BASE}/agent", json={
            "url": DEMO,
            "task": "extract all visible job listing titles and their companies",
            "max_steps": 4,
            "schema": {
                "jobs": [{"title": "string", "company": "string"}]
            },
            "use_llm": True,
        }, timeout=TIMEOUT * 2)
        data = r.json()
        if data.get("success"):
            steps = data.get("data", {}).get("steps", [])
            jobs = data.get("data", {}).get("data", {}).get("jobs", [])
            _pass(f"Agent completed in {len(steps)} step(s), extracted {len(jobs)} jobs")
            for j in (jobs or [])[:3]:
                print(f"      > {j.get('title', '?')} @ {j.get('company', '?')}")
        else:
            _fail(f"Agent failed: {data.get('error', '')[:200]}")
    except Exception as e:
        _fail(f"Error: {e}")


def main():
    print("=" * 60)
    print("  [TinyFish2] End-to-End Demo Test")
    print("=" * 60)

    if not check_health():
        print("\n[STOP] API is not running. Start it first:\n   python run_api.py\n")
        sys.exit(1)

    check_demo_page()
    test_query_login_button()
    test_query_search_input()
    test_action_type()
    test_action_click()
    test_extract_jobs()
    test_agent()

    print("\n" + "=" * 60)
    print(f"  Results: {PASS} passed, {FAIL} failed")
    print("=" * 60 + "\n")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
