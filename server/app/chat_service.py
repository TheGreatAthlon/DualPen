from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.models import ChatMessage

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_BODY_LENGTH = 2000


class MessageNotFoundError(Exception):
    pass


async def create_message(
    db: AsyncSession, doc_id: str, user_id: int, display_name: str, body: str
) -> ChatMessage:
    message = ChatMessage(doc_id=doc_id, user_id=user_id, display_name=display_name, body=body)
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def list_messages(
    db: AsyncSession, doc_id: str, limit: int = DEFAULT_LIST_LIMIT, before_id: str | None = None
) -> list[ChatMessage]:
    """Most recent `limit` messages before `before_id` (or overall, if omitted),
    returned oldest-first so callers can render them directly into a log."""
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    query = select(ChatMessage).where(ChatMessage.doc_id == doc_id)
    if before_id is not None:
        cursor = await db.execute(select(ChatMessage.sent_at).where(ChatMessage.id == before_id))
        cursor_sent_at = cursor.scalar_one_or_none()
        if cursor_sent_at is None:
            raise MessageNotFoundError(f"message '{before_id}' not found")
        query = query.where(ChatMessage.sent_at < cursor_sent_at)

    query = query.order_by(ChatMessage.sent_at.desc()).limit(limit)
    result = await db.execute(query)
    messages = list(result.scalars().all())
    messages.reverse()
    return messages
