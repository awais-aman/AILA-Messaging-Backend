from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from .db import init_db, get_db
from .schemas import MessageCreate, MessageOut, ConversationsOut, ConversationMessages, MessageStatus
from .crud import create_message, get_messages_for_user, get_message


app = FastAPI(title="AILA Messaging Backend", version="0.1.0")


@app.on_event("startup")
async def _startup():
    await init_db()


@app.post("/messages", response_model=MessageOut, status_code=201)
async def post_messages(payload: MessageCreate, db: AsyncSession = Depends(get_db)):
    msg = await create_message(
        db,
        user_id=payload.user_id,
        conversation_id=payload.conversation_id,
        text_content=payload.text,
        client_message_id=payload.client_message_id,
    )
    return MessageOut(
        message_id=msg.id,
        user_id=msg.user_id,
        conversation_id=msg.conversation_id,
        client_message_id=msg.client_message_id,
        seq=msg.seq,
        text=msg.user_text,
        state=msg.state,
        assistant_text=msg.assistant_text,
        last_error=msg.last_error,
        created_at=msg.created_at,
        updated_at=msg.updated_at,
    )


@app.get("/conversations/{userId}", response_model=ConversationsOut)
async def get_conversations(
    userId: str,
    conversation_id: str | None = Query(None, description="Optional conversationId filter"),
    db: AsyncSession = Depends(get_db),
):
    rows = await get_messages_for_user(db, userId, conversation_id)
    grouped: dict[str, list[MessageOut]] = {}
    for m in rows:
        grouped.setdefault(m.conversation_id, []).append(
            MessageOut(
                message_id=m.id,
                user_id=m.user_id,
                conversation_id=m.conversation_id,
                client_message_id=m.client_message_id,
                seq=m.seq,
                text=m.user_text,
                state=m.state,
                assistant_text=m.assistant_text,
                last_error=m.last_error,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
        )

    conversations = [ConversationMessages(conversation_id=cid, messages=msgs) for cid, msgs in grouped.items()]
    # Deterministic ordering by conversation_id
    conversations.sort(key=lambda c: c.conversation_id)

    return ConversationsOut(user_id=userId, conversations=conversations)


@app.get("/messages/{messageId}/status", response_model=MessageStatus)
async def get_message_status(messageId: UUID, db: AsyncSession = Depends(get_db)):
    msg = await get_message(db, messageId)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    return MessageStatus(
        message_id=msg.id,
        state=msg.state,
        assistant_text=msg.assistant_text,
        last_error=msg.last_error,
        attempt_count=msg.attempt_count,
        processing_started_at=msg.processing_started_at,
        completed_at=msg.completed_at,
    )
