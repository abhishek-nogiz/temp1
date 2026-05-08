"""
State management for long workflows: save/restore cookies,
localStorage, sessionStorage, and scroll position.
"""
import json
import os
from typing import Dict, Any, Optional
from datetime import datetime


class StateManager:
    """
    Manages browser state for workflow persistence:
    - Cookies
    - localStorage
    - sessionStorage
    - Scroll position
    """

    def __init__(self, state_dir: str = ".states"):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def _state_path(self, workflow_id: str) -> str:
        return os.path.join(self.state_dir, f"{workflow_id}.json")

    def save(self, workflow_id: str, page, metadata: Optional[Dict] = None) -> str:
        state = {
            "workflow_id": workflow_id,
            "timestamp": datetime.utcnow().isoformat(),
            "url": page.url,
            "cookies": [],
            "local_storage": {},
            "session_storage": {},
            "scroll_position": 0,
            "metadata": metadata or {},
        }

        try:
            state["scroll_position"] = page.evaluate("window.scrollY")
        except Exception:
            pass

        try:
            state["local_storage"] = page.evaluate(
                "() => { let d = {}; for (let k of Object.keys(localStorage)) { d[k] = localStorage.getItem(k); } return d; }"
            )
            state["session_storage"] = page.evaluate(
                "() => { let d = {}; for (let k of Object.keys(sessionStorage)) { d[k] = sessionStorage.getItem(k); } return d; }"
            )
        except Exception:
            pass

        path = self._state_path(workflow_id)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

        return path

    def load(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        path = self._state_path(workflow_id)
        if not os.path.exists(path):
            return None

        with open(path, "r") as f:
            return json.load(f)

    def restore(self, workflow_id: str, page, context):
        state = self.load(workflow_id)
        if not state:
            return False

        if state.get("cookies"):
            context.add_cookies(state["cookies"])

        try:
            for k, v in state.get("local_storage", {}).items():
                page.evaluate(f"localStorage.setItem('{k}', '{v}')")
            for k, v in state.get("session_storage", {}).items():
                page.evaluate(f"sessionStorage.setItem('{k}', '{v}')")
        except Exception:
            pass

        if state.get("scroll_position"):
            try:
                page.evaluate(f"window.scrollTo(0, {state['scroll_position']})")
            except Exception:
                pass

        return True

    def list_workflows(self) -> list:
        files = os.listdir(self.state_dir)
        return [f.replace(".json", "") for f in files if f.endswith(".json")]

    def delete(self, workflow_id: str):
        path = self._state_path(workflow_id)
        if os.path.exists(path):
            os.remove(path)
