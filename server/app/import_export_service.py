import io
import zipfile

from sqlalchemy.ext.asyncio import AsyncSession

from server.app import docstore, node_service
from server.app.models import Node
from server.app.schemas import validate_node_name


class InvalidZipError(Exception):
    pass


def _split_and_validate_path(zip_path: str) -> list[str] | None:
    """Splits a zip entry's internal path into validated segments, or
    returns None if any segment is invalid/empty (e.g. `..`, a name with
    forbidden characters) - such entries are skipped rather than failing
    the whole import, since a zip commonly has junk entries (._DS_Store,
    __MACOSX/, etc.) alongside the real content."""
    raw_segments = [s for s in zip_path.replace("\\", "/").split("/") if s]
    if not raw_segments:
        return None
    segments = []
    for raw in raw_segments:
        try:
            segments.append(validate_node_name(raw))
        except ValueError:
            return None
    return segments


async def import_zip(
    db: AsyncSession, zip_bytes: bytes, zip_filename: str, parent_id: str | None
) -> tuple[Node, list[str]]:
    """Extracts a zip archive into a new subfolder named after the zip file
    (per the project requirement: everything coming in stays organized under
    one clearly-labeled folder), recreating the archive's internal folder
    structure as Node rows. Text entries become documents; entries that
    aren't valid UTF-8 text are skipped and returned in the second tuple
    element (their original zip paths) rather than failing the whole import.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise InvalidZipError("not a valid zip file") from e

    root_name = zip_filename[:-4] if zip_filename.lower().endswith(".zip") else zip_filename
    try:
        root_name = validate_node_name(root_name)
    except ValueError:
        root_name = "Imported"

    root = await node_service.create_folder(db, root_name, parent_id)

    # Maps a validated folder-path tuple (relative to the zip root) to the
    # Node id already created for it, so multiple files under the same
    # subfolder share one folder node instead of creating duplicates.
    folder_ids: dict[tuple[str, ...], str] = {(): root.id}

    async def _ensure_folder(path: tuple[str, ...]) -> str:
        if path in folder_ids:
            return folder_ids[path]
        parent = await _ensure_folder(path[:-1])
        folder = await node_service.create_folder(db, path[-1], parent)
        folder_ids[path] = folder.id
        return folder.id

    skipped: list[str] = []

    for info in zf.infolist():
        if info.is_dir():
            segments = _split_and_validate_path(info.filename)
            if segments is None:
                continue
            await _ensure_folder(tuple(segments))
            continue

        segments = _split_and_validate_path(info.filename)
        if segments is None:
            skipped.append(info.filename)
            continue

        raw_bytes = zf.read(info)
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(info.filename)
            continue

        folder_path = tuple(segments[:-1])
        file_name = segments[-1]
        parent_folder_id = await _ensure_folder(folder_path)

        document = await node_service.create_document(db, file_name, parent_folder_id)
        if content:
            await node_service.set_document_content(db, document.id, content)

    return root, skipped


async def export_subtree_zip(db: AsyncSession, node_id: str | None) -> bytes:
    """Builds a zip of a node's subtree (or the entire tree, if node_id is
    None), preserving folder structure. The root node/tree's own top-level
    folders become top-level zip entries (no extra wrapping folder, since
    the caller already knows what they exported and the zip's own filename
    carries that context)."""
    if node_id is None:
        roots = await node_service.list_children(db, None)
    else:
        roots = [await node_service.get_node(db, node_id)]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for root in roots:
            await _write_node(db, zf, root, "")

    return buffer.getvalue()


async def _write_node(db: AsyncSession, zf: zipfile.ZipFile, node: Node, prefix: str) -> None:
    zip_path = f"{prefix}{node.name}"
    if node.kind == "folder":
        zf.writestr(f"{zip_path}/", "")
        children = await node_service.list_children(db, node.id)
        for child in children:
            await _write_node(db, zf, child, f"{zip_path}/")
    else:
        content = docstore.read_document(node.blob_path) if node.blob_path else ""
        zf.writestr(zip_path, content)
