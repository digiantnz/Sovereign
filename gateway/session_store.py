from dataclasses import dataclass, field
from typing import Optional, Any

HISTORY_MAX = 6  # turns kept in session for context window


@dataclass
class Session:
    chat_id: int
    awaiting_confirmation: bool = False
    awaiting_confidence_ack: bool = False
    awaiting_security_confirmation: bool = False
    pending_delegation: Optional[dict] = None
    pending_input: Optional[str] = None
    # Conversation history — list of {user, assistant} dicts, newest last
    history: list = field(default_factory=list)
    # Chunk assembly buffer — holds parts of a large paste split by Telegram
    message_buffer: list = field(default_factory=list)
    flush_task: Optional[Any] = None  # asyncio.Task; Any to avoid import here

    def push_turn(self, user: str, assistant: str) -> None:
        self.history.append({"user": user, "assistant": assistant})
        if len(self.history) > HISTORY_MAX:
            self.history.pop(0)


class SessionStore:
    def __init__(self):
        self._sessions: dict[int, Session] = {}

    def get_or_create(self, chat_id: int) -> Session:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = Session(chat_id=chat_id)
        return self._sessions[chat_id]

    def clear(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)
