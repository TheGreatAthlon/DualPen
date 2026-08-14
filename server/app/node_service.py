import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import docstore
from server.app.models import Node


class NodeNotFoundError(Exception):
    pass


class InvalidParentError(Exception):
    pass


class CycleError(Exception):
    pass


class NotAFolderError(Exception):
    pass


class NotEmptyError(Exception):
    pass


async def _get_node(db: AsyncSession, node_id: str) -> Node:
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if node is None:
        raise NodeNotFoundError(f"node '{node_id}' not found")
    return node


async def _validate_parent(db: AsyncSession, parent_id: str | None) -> None:
    if parent_id is None:
        return
    result = await db.execute(select(Node).where(Node.id == parent_id))
    parent = result.scalar_one_or_none()
    if parent is None:
        raise InvalidParentError(f"parent '{parent_id}' does not exist")
    if parent.kind != "folder":
        raise InvalidParentError("parent must be a folder")


async def list_tree(db: AsyncSession) -> list[Node]:
    result = await db.execute(select(Node).order_by(Node.name))
    return list(result.scalars().all())


async def get_node(db: AsyncSession, node_id: str) -> Node:
    return await _get_node(db, node_id)


async def list_children(db: AsyncSession, parent_id: str | None) -> list[Node]:
    result = await db.execute(select(Node).where(Node.parent_id == parent_id).order_by(Node.name))
    return list(result.scalars().all())


async def create_folder(db: AsyncSession, name: str, parent_id: str | None) -> Node:
    await _validate_parent(db, parent_id)
    node = Node(name=name, parent_id=parent_id, kind="folder", blob_path=None)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


async def create_document(db: AsyncSession, name: str, parent_id: str | None) -> Node:
    await _validate_parent(db, parent_id)
    blob_path = f"{uuid.uuid4()}.blob"
    node = Node(name=name, parent_id=parent_id, kind="document", blob_path=blob_path)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    docstore.write_document(blob_path, "")
    return node


async def get_document_node(db: AsyncSession, node_id: str) -> Node:
    node = await _get_node(db, node_id)
    if node.kind != "document":
        raise NodeNotFoundError(f"node '{node_id}' is not a document")
    return node


async def get_document_content(db: AsyncSession, node_id: str) -> str:
    node = await get_document_node(db, node_id)
    return docstore.read_document(node.blob_path)


async def set_document_content(db: AsyncSession, node_id: str, content: str) -> Node:
    node = await _get_node(db, node_id)
    if node.kind != "document":
        raise NodeNotFoundError(f"node '{node_id}' is not a document")
    docstore.write_document(node.blob_path, content)
    node.updated_at = datetime.datetime.now(datetime.timezone.utc)
    await db.commit()
    await db.refresh(node)
    return node


async def _would_create_cycle(db: AsyncSession, node_id: str, new_parent_id: str) -> bool:
    current_id: str | None = new_parent_id
    while current_id is not None:
        if current_id == node_id:
            return True
        result = await db.execute(select(Node.parent_id).where(Node.id == current_id))
        row = result.scalar_one_or_none()
        current_id = row
    return False


async def update_node(
    db: AsyncSession,
    node_id: str,
    name: str | None,
    parent_id: str | None,
    clear_parent: bool,
) -> Node:
    node = await _get_node(db, node_id)

    if name is not None:
        node.name = name

    if clear_parent:
        node.parent_id = None
    elif parent_id is not None:
        if parent_id == node_id:
            raise CycleError("a node cannot be its own parent")
        await _validate_parent(db, parent_id)
        if node.kind == "folder" and await _would_create_cycle(db, node_id, parent_id):
            raise CycleError("move would create a cycle")
        node.parent_id = parent_id

    await db.commit()
    await db.refresh(node)
    return node


async def delete_node(db: AsyncSession, node_id: str) -> None:
    node = await _get_node(db, node_id)

    if node.kind != "folder":
        raise NotAFolderError("only folders can be permanently deleted")

    result = await db.execute(select(Node.id).where(Node.parent_id == node_id).limit(1))
    if result.scalar_one_or_none() is not None:
        raise NotEmptyError("folder is not empty")

    await db.delete(node)
    await db.commit()
