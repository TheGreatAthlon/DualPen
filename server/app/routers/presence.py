from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import node_service
from server.app.auth import get_current_user
from server.app.db import get_db
from server.app.routers.sync import get_open_docs_by_user
from server.app.schemas import PresenceEntry

router = APIRouter(tags=["presence"], dependencies=[Depends(get_current_user)])


@router.get("/presence", response_model=list[PresenceEntry])
async def get_presence(db: AsyncSession = Depends(get_db)):
    entries: list[PresenceEntry] = []
    for user_id, (doc_id, display_name) in get_open_docs_by_user().items():
        try:
            node = await node_service.get_document_node(db, doc_id)
        except node_service.NodeNotFoundError:
            # Document was deleted out from under an open connection - omit
            # it rather than fail the whole roster for everyone else.
            continue
        entries.append(
            PresenceEntry(user_id=user_id, display_name=display_name, doc_id=doc_id, doc_name=node.name)
        )
    return entries
