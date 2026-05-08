"""
Concurrent browser session pool with load balancing,
health checks, and automatic recycling.
"""
import uuid
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime
from ..browser import BrowserSession
from ..schemas import SessionInfo, SessionStatus


@dataclass
class SessionPoolConfig:
    min_sessions: int = 2
    max_sessions: int = 10
    max_age_seconds: int = 300
    health_check_interval: int = 30
    headless: bool = True


class SessionPool:
    """
    Manages a pool of browser sessions for concurrent scraping.
    Features:
    - Auto-scaling (min/max sessions)
    - Health checks
    - Session recycling
    - Load balancing
    """

    def __init__(self, config: Optional[SessionPoolConfig] = None):
        self.config = config or SessionPoolConfig()
        self._sessions: Dict[str, BrowserSession] = {}
        self._info: Dict[str, SessionInfo] = {}
        self._lock = threading.RLock()
        self._shutdown = False

        self._health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_thread.start()

        for _ in range(self.config.min_sessions):
            self._create_session()

    def _create_session(self) -> str:
        session_id = str(uuid.uuid4())[:8]
        session = BrowserSession(headless=self.config.headless).start()

        now = datetime.utcnow().isoformat()
        info = SessionInfo(
            session_id=session_id,
            status=SessionStatus.IDLE,
            created_at=now,
            last_used=now,
        )

        with self._lock:
            self._sessions[session_id] = session
            self._info[session_id] = info

        return session_id

    def acquire(self) -> tuple[str, BrowserSession]:
        with self._lock:
            for sid, info in self._info.items():
                if info.status == SessionStatus.IDLE:
                    info.status = SessionStatus.BUSY
                    info.last_used = datetime.utcnow().isoformat()
                    return sid, self._sessions[sid]

            if len(self._sessions) < self.config.max_sessions:
                sid = self._create_session()
                self._info[sid].status = SessionStatus.BUSY
                return sid, self._sessions[sid]

        while True:
            time.sleep(0.5)
            with self._lock:
                for sid, info in self._info.items():
                    if info.status == SessionStatus.IDLE:
                        info.status = SessionStatus.BUSY
                        info.last_used = datetime.utcnow().isoformat()
                        return sid, self._sessions[sid]

    def release(self, session_id: str, error: bool = False):
        with self._lock:
            if session_id in self._info:
                self._info[session_id].status = SessionStatus.ERROR if error else SessionStatus.IDLE
                self._info[session_id].error_count += 1 if error else 0
                self._info[session_id].last_used = datetime.utcnow().isoformat()

    def get_info(self, session_id: str) -> Optional[SessionInfo]:
        with self._lock:
            return self._info.get(session_id)

    def list_sessions(self) -> List[SessionInfo]:
        with self._lock:
            return list(self._info.values())

    def _health_check_loop(self):
        while not self._shutdown:
            time.sleep(self.config.health_check_interval)

            with self._lock:
                now = time.time()
                to_remove = []

                for sid, info in self._info.items():
                    created = datetime.fromisoformat(info.created_at)
                    age = now - created.timestamp()

                    if age > self.config.max_age_seconds:
                        to_remove.append(sid)
                        continue

                    if info.error_count > 5:
                        to_remove.append(sid)
                        continue

                    try:
                        session = self._sessions[sid]
                        session.page.evaluate("1 + 1")
                    except Exception:
                        to_remove.append(sid)

                for sid in to_remove:
                    try:
                        self._sessions[sid].close()
                    except Exception:
                        pass
                    del self._sessions[sid]
                    del self._info[sid]

                while len(self._sessions) < self.config.min_sessions:
                    self._create_session()

    def shutdown(self):
        self._shutdown = True
        with self._lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    pass
            self._sessions.clear()
            self._info.clear()
