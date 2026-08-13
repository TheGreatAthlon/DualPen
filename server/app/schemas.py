import datetime
import re

from pydantic import BaseModel, ConfigDict, field_validator

# Reject characters Windows/most filesystems forbid in a single path segment,
# so names stay portable if documents are ever exported to real files.
_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def validate_node_name(name: str) -> str:
    stripped = name.strip(" .")
    if not stripped:
        raise ValueError("name cannot be empty")
    if _INVALID_NAME_CHARS.search(name):
        raise ValueError('name cannot contain \\ / : * ? " < > | or control characters')
    return stripped


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    is_admin: bool
    is_active: bool
    created_at: datetime.datetime


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    initial_password: str


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    new_password: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_id: str | None
    name: str
    kind: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: str | None = None

    _validate_name = field_validator("name")(validate_node_name)


class CreateDocumentRequest(BaseModel):
    name: str
    parent_id: str | None = None

    _validate_name = field_validator("name")(validate_node_name)


class UpdateNodeRequest(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    clear_parent: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_node_name(value)


class DocumentContentOut(BaseModel):
    content: str


class DocumentContentIn(BaseModel):
    content: str


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    doc_id: str
    user_id: int
    display_name: str
    body: str
    sent_at: datetime.datetime


class ImportZipResultOut(BaseModel):
    root: NodeOut
    skipped: list[str]
