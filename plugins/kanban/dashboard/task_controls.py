"""Validated request contracts for existing-card control routes."""
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class GoalConfigBody(BaseModel):
    goal_mode: StrictBool | None = None
    goal_max_turns: StrictInt | None = Field(default=None, ge=0, le=2**63 - 1)


class NotifySubscriptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: StrictStr = Field(min_length=1)
    chat_id: StrictStr = Field(min_length=1)
    thread_id: StrictStr | None
    user_id: StrictStr | None
    chat_type: Literal["dm", "group", "channel", "thread"]
    notifier_profile: StrictStr = Field(min_length=1)
    delivery_mode: Literal["notify", "notify+wake", "wake"]


SUBSCRIPTION_FIELDS = (
    "task_id", "platform", "chat_id", "thread_id", "user_id", "chat_type",
    "notifier_profile", "delivery_mode", "created_at", "last_event_id",
)


def require_control_write_origin(request: Request) -> None:
    """Cookie-authenticated controls must not accept cross-origin browser writes.

    Dashboard auth remains authoritative. Non-browser authenticated API clients
    omit Origin. Compare exact origin (including port), not just a trusted hostname.
    This is scoped to the new controls; it does not relax any existing middleware.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid control write Origin") from None
    expected = urlsplit(str(request.base_url))
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc)):
        raise HTTPException(status_code=403, detail="Cross-origin control write denied")
