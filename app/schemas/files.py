"""API contracts for the file manager (Phase 3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    """A directory entry; ``path`` is always relative to the managed root."""

    name: str
    path: str
    is_dir: bool
    size: int
    modified: datetime | None = None
    mode: str


class FileListing(BaseModel):
    root: str
    path: str
    entries: list[FileEntry]


class SearchResult(BaseModel):
    root: str
    query: str
    matches: list[FileEntry]


class MkdirRequest(BaseModel):
    root: str
    path: str = ""
    name: str = Field(min_length=1, max_length=255)


class RenameRequest(BaseModel):
    root: str
    path: str
    new_name: str = Field(min_length=1, max_length=255)


class DeleteRequest(BaseModel):
    root: str
    path: str
    recursive: bool = False
