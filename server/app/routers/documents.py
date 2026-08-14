import urllib.parse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from server.app import chat_service, import_export_service, node_service
from server.app.auth import get_current_user
from server.app.db import get_db
from server.app.schemas import (
    ChatMessageOut,
    CreateDocumentRequest,
    CreateFolderRequest,
    DocumentContentIn,
    DocumentContentOut,
    ImportZipResultOut,
    NodeOut,
    UpdateNodeRequest,
)

router = APIRouter(tags=["documents"], dependencies=[Depends(get_current_user)])


@router.get("/tree", response_model=list[NodeOut])
async def get_tree(db: AsyncSession = Depends(get_db)):
    return await node_service.list_tree(db)


@router.post("/folders", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_folder(payload: CreateFolderRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await node_service.create_folder(db, payload.name, payload.parent_id)
    except node_service.InvalidParentError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/documents", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_document(payload: CreateDocumentRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await node_service.create_document(db, payload.name, payload.parent_id)
    except node_service.InvalidParentError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/documents/{node_id}/content", response_model=DocumentContentOut)
async def get_document_content(node_id: str, db: AsyncSession = Depends(get_db)):
    try:
        content = await node_service.get_document_content(db, node_id)
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return DocumentContentOut(content=content)


@router.put("/documents/{node_id}/content", response_model=NodeOut)
async def put_document_content(node_id: str, payload: DocumentContentIn, db: AsyncSession = Depends(get_db)):
    try:
        return await node_service.set_document_content(db, node_id, payload.content)
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def update_node(node_id: str, payload: UpdateNodeRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await node_service.update_node(
            db, node_id, payload.name, payload.parent_id, payload.clear_parent
        )
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except node_service.InvalidParentError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except node_service.CycleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: str, db: AsyncSession = Depends(get_db)):
    try:
        await node_service.delete_node(db, node_id)
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except node_service.NotAFolderError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except node_service.NotEmptyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/documents/{node_id}/chat", response_model=list[ChatMessageOut])
async def get_document_chat(
    node_id: str,
    limit: int = Query(default=chat_service.DEFAULT_LIST_LIMIT, ge=1, le=chat_service.MAX_LIST_LIMIT),
    before_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    try:
        await node_service.get_document_node(db, node_id)
    except node_service.NodeNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    try:
        return await chat_service.list_messages(db, node_id, limit=limit, before_id=before_id)
    except chat_service.MessageNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/import-zip", response_model=ImportZipResultOut, status_code=status.HTTP_201_CREATED)
async def import_zip(
    file: UploadFile = File(...),
    parent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no file provided")
    zip_bytes = await file.read()
    try:
        root, skipped = await import_export_service.import_zip(db, zip_bytes, file.filename, parent_id)
    except import_export_service.InvalidZipError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except node_service.InvalidParentError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return ImportZipResultOut(root=root, skipped=skipped)


@router.get("/export-zip")
async def export_zip(node_id: str | None = None, db: AsyncSession = Depends(get_db)):
    if node_id is not None:
        try:
            node = await node_service.get_node(db, node_id)
        except node_service.NodeNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        download_name = f"{node.name}.zip"
    else:
        download_name = "documents.zip"

    zip_bytes = await import_export_service.export_subtree_zip(db, node_id)
    # RFC 5987 filename* so names with non-ASCII/special characters survive
    # the Content-Disposition header; browsers that don't support filename*
    # fall back to the plain filename after the semicolon.
    encoded_name = urllib.parse.quote(download_name)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"{download_name}\"; filename*=UTF-8''{encoded_name}"},
    )
