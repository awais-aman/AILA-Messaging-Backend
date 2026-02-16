import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Conversation, Message


async def create_message(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    text_content: str,
    client_message_id: Optional[str] = None,
) -> Message:
    # Idempotency: client_message_id is the natural idempotency key.
    # If not provided, generate one (still stable for this request only).
    client_message_id = client_message_id or str(uuid.uuid4())

    msg: Message | None = None
    created = False

    async with session.begin():
        # If already exists (duplicate submission), return existing record.
        existing = await session.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.client_message_id == client_message_id,
            )
        )
        msg = existing.scalar_one_or_none()
        if msg:
            return msg

        # Ensure conversation row exists
        await session.execute(
            text(
                """
                INSERT INTO conversations (user_id, conversation_id, next_seq, created_at, updated_at)
                VALUES (:user_id, :conversation_id, 1, now(), now())
                ON CONFLICT (user_id, conversation_id) DO NOTHING
                """
            ),
            {"user_id": user_id, "conversation_id": conversation_id},
        )

        # Lock conversation row to allocate a strict, monotonic seq (send-order)
        res = await session.execute(
            text(
                """
                SELECT next_seq FROM conversations
                WHERE user_id = :user_id AND conversation_id = :conversation_id
                FOR UPDATE
                """
            ),
            {"user_id": user_id, "conversation_id": conversation_id},
        )
        next_seq = res.scalar_one()
        seq = int(next_seq)

        await session.execute(
            text(
                """
                UPDATE conversations
                SET next_seq = next_seq + 1, updated_at = now()
                WHERE user_id = :user_id AND conversation_id = :conversation_id
                """
            ),
            {"user_id": user_id, "conversation_id": conversation_id},
        )

        msg = Message(
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            client_message_id=client_message_id,
            seq=seq,
            user_text=text_content,
            assistant_text=None,
            state="RECEIVED",  # UI can treat as PENDING
            attempt_count=0,
            available_at=datetime.utcnow(),
            processing_started_at=None,
            processing_deadline_at=None,
            completed_at=None,
            last_error=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(msg)
        created = True

    if msg is None:
        raise RuntimeError("Message creation failed")

    if created:
        # Refresh to ensure PK and timestamps are there
        await session.refresh(msg)
    return msg


async def get_message(session: AsyncSession, message_id: uuid.UUID) -> Optional[Message]:
    res = await session.execute(select(Message).where(Message.id == message_id))
    return res.scalar_one_or_none()


async def get_messages_for_user(session: AsyncSession, user_id: str, conversation_id: Optional[str] = None) -> List[Message]:
    stmt = select(Message).where(Message.user_id == user_id)
    if conversation_id:
        stmt = stmt.where(Message.conversation_id == conversation_id)
    stmt = stmt.order_by(Message.conversation_id.asc(), Message.seq.asc())
    res = await session.execute(stmt)
    return list(res.scalars().all())
