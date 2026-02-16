from datetime import datetime
from typing import Optional, List, Dict
from uuid import UUID

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    user_id: str = Field(..., examples=["user_123"])
    conversation_id: str = Field(..., examples=["conv_abc"])
    text: str = Field(..., min_length=1, max_length=4000)
    client_message_id: Optional[str] = Field(None, description="Client idempotency key (UUID recommended)")


class MessageOut(BaseModel):
    message_id: UUID
    user_id: str
    conversation_id: str
    client_message_id: str
    seq: int
    text: str
    state: str
    assistant_text: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MessageStatus(BaseModel):
    message_id: UUID
    state: str
    assistant_text: Optional[str] = None
    last_error: Optional[str] = None
    attempt_count: int
    processing_started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ConversationMessages(BaseModel):
    conversation_id: str
    messages: List[MessageOut]


class ConversationsOut(BaseModel):
    user_id: str
    conversations: List[ConversationMessages]
