"""Long-lived GUI sessions: terminal tabs, live captures and pcap downloads.

Each one holds a process (and a pty or tcpdump) on a lab host for as long
as the browser keeps it open. The registry counts them per user and in
total, so one browser cannot open hundreds of them, and lets the web server
end a user's sessions when the user is removed, rotated or downgraded or
logs out, and when the GUI shuts down.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .auth import User

TERMINAL = "terminal"  # terminal tabs, including Logs
CAPTURE = "capture"    # live capture tabs and pcap downloads

MAX_USER_SESSIONS = 16  # terminal tabs per user
MAX_SESSIONS = 64       # terminal tabs in all
MAX_USER_CAPTURES = 4   # captures per user
MAX_CAPTURES = 8        # captures in all

NOUNS = {TERMINAL: "terminal sessions", CAPTURE: "packet captures"}

# Ends a session: (websocket close code, reason)
EndSession = Callable[[int, str], Awaitable[Any]]


class SessionLimitError(Exception):
    """Too many sessions open; the message says which limit."""


@dataclass(eq=False)
class OpenSession:
    kind: str
    user: User
    request: Any                      # the aiohttp request it came from
    end: Optional[EndSession] = None  # set once there is something to end
    ending: bool = False


class SessionRegistry:
    def __init__(self, max_user_sessions: int = MAX_USER_SESSIONS,
                 max_sessions: int = MAX_SESSIONS,
                 max_user_captures: int = MAX_USER_CAPTURES,
                 max_captures: int = MAX_CAPTURES):
        self.limits = {TERMINAL: (max_user_sessions, max_sessions),
                       CAPTURE: (max_user_captures, max_captures)}
        self._open: list[OpenSession] = []

    def __iter__(self):
        return iter(list(self._open))

    def __len__(self) -> int:
        return len(self._open)

    def count(self, kind: str, user: Optional[str] = None) -> int:
        return sum(1 for s in self._open
                   if s.kind == kind and (user is None or s.user.name == user))

    def open(self, kind: str, user: User, request: Any,
             end: Optional[EndSession] = None) -> OpenSession:
        """Register a new session, or raise SessionLimitError."""
        per_user, total = self.limits[kind]
        noun = NOUNS[kind]
        if self.count(kind) >= total:
            raise SessionLimitError(
                f"The GUI already has {total} open {noun} (the limit); close some first")
        if self.count(kind, user.name) >= per_user:
            raise SessionLimitError(
                f"You already have {per_user} open {noun} (the limit); close some first")
        session = OpenSession(kind, user, request, end)
        self._open.append(session)
        return session

    def close(self, session: OpenSession) -> None:
        try:
            self._open.remove(session)
        except ValueError:
            pass
