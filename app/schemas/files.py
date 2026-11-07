"""API contracts for the file manager (Phase 3; locations and transfers Phase 16)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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


class FileLocation(BaseModel):
    """A place the Files page can open: a NAS root or a plugged-in USB drive."""

    root: str
    label: str
    kind: Literal["nas", "removable"]
    writable: bool
    fstype: str | None = None
    device: str | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None


class TransferItem(BaseModel):
    root: str
    path: str = Field(min_length=1, max_length=4096)


class TransferCreate(BaseModel):
    sources: list[TransferItem] = Field(min_length=1, max_length=1000)
    dest_root: str
    dest_path: str = Field(default="", max_length=4096)
    mode: Literal["copy", "move"] = "copy"
    conflict: Literal["keep_both", "skip", "replace"] = "keep_both"


class TransferProblem(BaseModel):
    path: str
    message: str


class TransferOut(BaseModel):
    id: int
    user_id: int | None
    mode: str
    sources: list[TransferItem]
    dest_root: str
    dest_path: str
    conflict: str
    state: str
    total_bytes: int
    done_bytes: int
    total_files: int
    done_files: int
    skipped_files: int
    current_name: str | None
    message: str | None
    errors: list[TransferProblem]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
