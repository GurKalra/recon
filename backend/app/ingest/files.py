"""Accept an uploaded file safely: size cap, real type (not just the extension), zip-bomb guard.

Nothing the user sends is trusted. The stored name is random; the original name is kept only as a
cleaned label. A file is staged on disk and only read by the pipeline once it has passed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ALLOWED = {".csv": "csv", ".xlsx": "xlsx"}
_CHUNK = 1 << 20
_BAD_MAGIC = (b"MZ", b"\x7fELF", b"%PDF", b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"\xd0\xcf\x11\xe0",
              b"PK\x03\x04", b"Rar!", b"\x1f\x8b", b"#!")
_MAX_ENTRIES = 5000
_MAX_RATIO = 1000            # uncompressed / compressed, for any large entry
_RATIO_FLOOR = 10 << 20      # ratios only matter once an entry is this big
_REQUIRED_XLSX = ("[Content_Types].xml", "xl/workbook.xml")


class UploadRejected(ValueError):
    """The file cannot be accepted; the message is safe to show the user."""


@dataclass(frozen=True)
class StagedFile:
    path: Path
    original_name: str
    kind: str          # csv | xlsx
    size: int
    sha256: str


def clean_name(name: str) -> str:
    """A label for the file: no folders, no odd characters, a sensible length."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", base)[:120].strip(" .")
    return base or "upload"


def _check_xlsx(path: Path, max_unzipped: int) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > _MAX_ENTRIES:
                raise UploadRejected("That Excel file has too many parts.")
            names = {i.filename for i in infos}
            if not all(r in names for r in _REQUIRED_XLSX):
                raise UploadRejected("That is not an Excel workbook.")
            if any(n.startswith(("/", "\\")) or ".." in n.split("/") for n in names):
                raise UploadRejected("That Excel file has unsafe contents.")
            if "xl/vbaProject.bin" in names:
                raise UploadRejected("Excel files with macros are not accepted. Save as .xlsx.")
            if sum(i.file_size for i in infos) > max_unzipped:
                raise UploadRejected("That Excel file expands to an unreasonable size.")
            for i in infos:
                ratio = i.file_size / max(i.compress_size, 1)
                if i.file_size > _RATIO_FLOOR and ratio > _MAX_RATIO:
                    raise UploadRejected("That Excel file looks like a decompression bomb.")
    except zipfile.BadZipFile as exc:
        raise UploadRejected("That is not a valid Excel (.xlsx) file.") from exc


def _check_csv(head: bytes) -> None:
    if not head.strip():
        raise UploadRejected("The file is empty.")
    if any(head.startswith(m) for m in _BAD_MAGIC):
        raise UploadRejected("That does not look like a CSV text file.")
    if b"\x00" in head:
        raise UploadRejected("That does not look like a CSV text file.")
    control = sum(1 for b in head if b < 9 or 13 < b < 32)
    if control > max(1, len(head) // 100):
        raise UploadRejected("That does not look like a CSV text file.")


def stage_upload(stream: BinaryIO, filename: str, dest_dir: Path, max_bytes: int,
                 max_unzipped: int = 1 << 30) -> StagedFile:
    """Copy `stream` into `dest_dir` under a random name, checking as we go."""
    original = clean_name(filename)
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED:
        raise UploadRejected("Only .csv and .xlsx files are accepted.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f"{uuid.uuid4().hex}.part"
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        with tmp.open("wb") as out:
            while chunk := stream.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(f"The file is larger than {max_bytes >> 20} MB.")
                if len(head) < 65536:
                    head += chunk[:65536 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise UploadRejected("The file is empty.")
        kind = ALLOWED[ext]
        if kind == "xlsx":
            if not head.startswith(b"PK\x03\x04"):
                raise UploadRejected("That is not a valid Excel (.xlsx) file.")
            _check_xlsx(tmp, max_unzipped)
        else:
            _check_csv(head)
        final = dest_dir / f"{uuid.uuid4().hex}{ext}"
        tmp.rename(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return StagedFile(final, original, kind, size, digest.hexdigest())