import asyncio
import os
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db import Base  # uses same models metadata
from app import models  # noqa: F401

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/aila")

# Simulation settings (match assignment defaults)
MIN_DELAY_SECONDS = int(os.getenv("MIN_DELAY_SECONDS", "5"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", str(4 * 60)))
FAIL_RATE = float(os.getenv("FAIL_RATE", "0.2"))

# Reliability settings
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "1.0"))
CLAIM_BATCH = int(os.getenv("CLAIM_BATCH", "10"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
PROCESSING_TIMEOUT_SECONDS = int(os.getenv("PROCESSING_TIMEOUT_SECONDS", str(5 * 60)))  # lease per attempt
BACKOFF_BASE_SECONDS = int(os.getenv("BACKOFF_BASE_SECONDS", "2"))
BACKOFF_MAX_SECONDS = int(os.getenv("BACKOFF_MAX_SECONDS", "120"))


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@dataclass
class Job:
    id: uuid.UUID
    user_text: str
    attempt_count: int


def now_utc() -> datetime:
    return datetime.utcnow()


def backoff_seconds(attempt: int) -> int:
    # attempt is already incremented when claimed; attempt=1 => 2s, attempt=2 => 4s ...
    secs = BACKOFF_BASE_SECONDS ** max(1, attempt)
    return int(min(secs, BACKOFF_MAX_SECONDS))


async def reap_expired_jobs():
    # If a worker crashes mid-processing, the row may be stuck in PROCESSING.
    # We convert expired leases into TIMEOUT (and optionally requeue, capped by MAX_ATTEMPTS).
    async with SessionLocal() as session:
        async with session.begin():
            # Mark timed-out PROCESSING rows as TIMEOUT
            await session.execute(
                text(
                    """
                    UPDATE messages
                    SET state = 'TIMEOUT',
                        last_error = COALESCE(last_error, 'processing lease expired'),
                        updated_at = now()
                    WHERE state = 'PROCESSING'
                      AND processing_deadline_at IS NOT NULL
                      AND processing_deadline_at < now()
                    """
                )
            )

            # Requeue TIMEOUT rows if retries remain
            await session.execute(
                text(
                    """
                    UPDATE messages
                    SET state = 'RECEIVED',
                        available_at = now() + make_interval(secs => :delay),
                        updated_at = now()
                    WHERE state = 'TIMEOUT'
                      AND attempt_count < :max_attempts
                    """
                ),
                {"delay": BACKOFF_BASE_SECONDS, "max_attempts": MAX_ATTEMPTS},
            )


async def claim_jobs() -> List[Job]:
    async with SessionLocal() as session:
        async with session.begin():
            # Claim RECEIVED jobs that are available, in send-order within a conversation (seq),
            # but we can process across conversations/users concurrently.
            res = await session.execute(
                text(
                    """
                    WITH cte AS (
                        SELECT id
                        FROM messages
                        WHERE state = 'RECEIVED'
                          AND available_at <= now()
                        ORDER BY created_at ASC
                        LIMIT :batch
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE messages
                    SET state = 'PROCESSING',
                        attempt_count = attempt_count + 1,
                        processing_started_at = now(),
                        processing_deadline_at = now() + make_interval(secs => :lease_secs),
                        updated_at = now()
                    WHERE id IN (SELECT id FROM cte)
                    RETURNING id, user_text, attempt_count
                    """
                ),
                {"batch": CLAIM_BATCH, "lease_secs": PROCESSING_TIMEOUT_SECONDS},
            )
            rows = res.mappings().all()
            return [Job(id=row["id"], user_text=row["user_text"], attempt_count=row["attempt_count"]) for row in rows]


async def mark_completed(job: Job, assistant_text: str):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE messages
                    SET state = 'COMPLETED',
                        assistant_text = :assistant_text,
                        completed_at = now(),
                        updated_at = now(),
                        last_error = NULL
                    WHERE id = :id
                      AND state = 'PROCESSING'
                    """
                ),
                {"id": job.id, "assistant_text": assistant_text},
            )


async def send_to_dead_letter(job: Job, reason: str):
    async with SessionLocal() as session:
        async with session.begin():
            # Capture message metadata for debugging
            row = (
                await session.execute(
                    text(
                        """
                        SELECT user_id, conversation_id, seq
                        FROM messages
                        WHERE id = :id
                        """
                    ),
                    {"id": job.id},
                )
            ).mappings().first()
            if row:
                await session.execute(
                    text(
                        """
                        INSERT INTO dead_letters (id, message_id, user_id, conversation_id, seq, reason, created_at)
                        VALUES (:id, :message_id, :user_id, :conversation_id, :seq, :reason, now())
                        """
                    ),
                    {
                        "message_id": job.id,
                        "user_id": row["user_id"],
                        "conversation_id": row["conversation_id"],
                        "seq": row["seq"],
                        "id": uuid.uuid4(),
                        "reason": reason,
                    },
                )


async def mark_failed(job: Job, err: str):
    retry = job.attempt_count < MAX_ATTEMPTS
    async with SessionLocal() as session:
        async with session.begin():
            if retry:
                delay = backoff_seconds(job.attempt_count)
                await session.execute(
                    text(
                        """
                        UPDATE messages
                        SET state = 'RECEIVED',
                            available_at = now() + make_interval(secs => :delay),
                            last_error = :err,
                            updated_at = now()
                        WHERE id = :id
                          AND state = 'PROCESSING'
                        """
                    ),
                    {"id": job.id, "delay": delay, "err": err},
                )
            else:
                await session.execute(
                    text(
                        """
                        UPDATE messages
                        SET state = 'FAILED',
                            last_error = :err,
                            updated_at = now()
                        WHERE id = :id
                          AND state = 'PROCESSING'
                        """
                    ),
                    {"id": job.id, "err": err},
                )
    if not retry:
        await send_to_dead_letter(job, err)


async def process_job(job: Job, sem: asyncio.Semaphore):
    async with sem:
        delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        await asyncio.sleep(delay)
        if random.random() < FAIL_RATE:
            await mark_failed(job, f"simulated failure after {delay}s (attempt {job.attempt_count})")
        else:
            # A placeholder "AI" response. In real integration, call your LLM here.
            await mark_completed(job, f"[AI] Response (attempt {job.attempt_count}): {job.user_text}")


async def ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def main():
    await ensure_schema()
    sem = asyncio.Semaphore(CONCURRENCY)
    while True:
        await reap_expired_jobs()
        jobs = await claim_jobs()
        if not jobs:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        tasks = [asyncio.create_task(process_job(j, sem)) for j in jobs]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
