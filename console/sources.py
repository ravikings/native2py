"""Untrusted upload/clone handling for the console app.

No project dependencies (no console.db, no console.app) — deliberately
standalone so untrusted-input handling can be reasoned about (and tested) in
isolation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

MAX_EXTRACTED_BYTES = 50 * 1024 * 1024  # 50MB
MAX_FILE_COUNT = 2000

_SINGLE_FILE_EXTENSIONS = {
    ".f90", ".f", ".f77", ".for",
    ".cpp", ".hpp", ".h", ".cc", ".cxx", ".hh",
    # Fortran INCLUDE files (e.g. `INCLUDE 'GRID.INC'`) — a legacy fixed-form
    # routine that needs one fails to compile if it's silently dropped, and
    # unlike a missing .cpp (caught at discovery, see pages.py's
    # header-without-impl warning), a missing .INC only surfaces as a raw
    # compiler error deep in the build log.
    ".inc",
}

_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/(?P<org>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def _safe_dest(dest_dir: Path, member_name: str) -> Path:
    """Resolve member_name under dest_dir, raising ValueError on traversal."""
    dest_dir_resolved = dest_dir.resolve()
    candidate = (dest_dir_resolved / member_name).resolve()
    try:
        candidate.relative_to(dest_dir_resolved)
    except ValueError:
        raise ValueError(f"Refusing to extract path outside destination: {member_name}") from None
    return candidate


def _unpack_zip(archive_path: Path, dest_dir: Path) -> None:
    total_size = 0
    file_count = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            # Reject symlinks: zip stores unix mode in the high bits of
            # external_attr; a symlink has S_IFLNK (0o120000) there.
            mode = (info.external_attr >> 16) & 0xFFFF
            import stat

            if stat.S_ISLNK(mode):
                raise ValueError(f"Refusing to extract symlink: {info.filename}")
            if info.is_dir():
                continue

            target = _safe_dest(dest_dir, info.filename)

            file_count += 1
            if file_count > MAX_FILE_COUNT:
                raise ValueError(f"Archive contains too many files (> {MAX_FILE_COUNT})")

            total_size += info.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError(f"Extracted archive exceeds size cap ({MAX_EXTRACTED_BYTES} bytes)")

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _unpack_tar(archive_path: Path, dest_dir: Path) -> None:
    total_size = 0
    file_count = 0
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Refusing to extract symlink/hardlink: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                # devices, fifos, etc: refuse.
                raise ValueError(f"Refusing to extract non-regular file: {member.name}")

            target = _safe_dest(dest_dir, member.name)

            file_count += 1
            if file_count > MAX_FILE_COUNT:
                raise ValueError(f"Archive contains too many files (> {MAX_FILE_COUNT})")

            total_size += member.size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError(f"Extracted archive exceeds size cap ({MAX_EXTRACTED_BYTES} bytes)")

            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f"Could not read archive member: {member.name}")
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def unpack_uploads(files: list[tuple[str, bytes]], dest_dir: Path) -> Path:
    """Write several loose files (e.g. a header + its matching .cpp) into
    dest_dir, or a mix including at most one archive.

    A header-only upload compiles but fails at import with a confusing
    dlopen "symbol not found" error (see console/routes/pages.py's discovery
    warning for this) — multi-select lets the browser file picker submit the
    header and implementation together instead of requiring a zip. Each file
    goes through the same per-file checks as `unpack_upload` (path-traversal,
    symlink, extension allowlist); this adds only an aggregate size cap,
    since N individually-capped files could otherwise sum past the intended
    total upload limit.
    """
    total = sum(len(data) for _, data in files)
    if total > MAX_EXTRACTED_BYTES:
        raise ValueError(f"Upload exceeds size cap ({MAX_EXTRACTED_BYTES} bytes)")
    for filename, data in files:
        unpack_upload(data, filename, dest_dir)
    return Path(dest_dir)


def unpack_folder_upload(files: list[tuple[str, bytes]], dest_dir: Path) -> Path:
    """Write a browser folder selection (`<input webkitdirectory>`) into dest_dir.

    Each file's name arrives as a browser-supplied relative path (e.g.
    "geometry/geometry.hpp", using the folder the user picked as root).
    Every file is flattened to its bare basename directly under dest_dir,
    NOT written at its nested relative path — `ngate generate` collects a
    service's native sources with `native_dir.iterdir()`
    (tools/nativegate/nativegate/cli.py, ~line 1609), which is a single
    non-recursive directory listing. A header and its paired .cpp landing at
    "native/<picked-folder-name>/geometry.hpp" instead of "native/geometry.hpp"
    are invisible to it — `ngate generate` reports "no implementation files"
    even though the .cpp really is there, one directory too deep. Preserving
    the folder-picker's directory structure would be preserving a layout
    nativegate can't actually consume, so flattening here is what makes a
    folder upload behave the same as multi-selecting the same files
    (console/sources.py's `unpack_uploads`) rather than mysteriously failing.

    A genuine basename collision (two same-named files in different
    subdirectories) raises rather than silently overwriting one — that would
    be exactly the "some of my files went missing" failure mode this is
    meant to avoid. Files with an extension outside
    `_SINGLE_FILE_EXTENSIONS` are silently skipped — a real checkout brings
    along `.git/`, build artifacts, READMEs, etc. that nativegate has no use
    for and that would otherwise blow past the size/file-count caps on an
    otherwise-small source tree.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    total_size = 0
    file_count = 0
    seen_basenames: dict[str, str] = {}
    for relpath, data in files:
        if Path(relpath).suffix.lower() not in _SINGLE_FILE_EXTENSIONS:
            continue

        basename = Path(relpath).name
        if basename in seen_basenames and seen_basenames[basename] != relpath:
            raise ValueError(
                f"Folder has two files named {basename!r} in different subdirectories "
                f"({seen_basenames[basename]!r} and {relpath!r}) — nativegate only sees "
                "a flat native/ directory, so both would collide. Rename one, or upload "
                "a .zip/.tar.gz instead if the folder structure matters."
            )
        seen_basenames[basename] = relpath

        target = _safe_dest(dest_dir, basename)

        file_count += 1
        if file_count > MAX_FILE_COUNT:
            raise ValueError(f"Folder contains too many source files (> {MAX_FILE_COUNT})")

        total_size += len(data)
        if total_size > MAX_EXTRACTED_BYTES:
            raise ValueError(f"Folder upload exceeds size cap ({MAX_EXTRACTED_BYTES} bytes)")

        target.write_bytes(data)

    if file_count == 0:
        raise ValueError(
            "No recognizable C++/Fortran source files found in the selected folder "
            f"(looked for {sorted(_SINGLE_FILE_EXTENSIONS)})."
        )

    return dest_dir


def unpack_upload(data: bytes, filename: str, dest_dir: Path) -> Path:
    """Extract an uploaded zip/tar.gz/tgz, or write a single source file.

    Strict path-traversal protection for archives: rejects members that
    resolve outside dest_dir, rejects symlinks, caps total extracted size at
    50MB, and caps file count. Returns dest_dir.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    lower = filename.lower()
    suffix = Path(filename).suffix.lower()

    if len(data) > MAX_EXTRACTED_BYTES:
        raise ValueError(f"Upload exceeds size cap ({MAX_EXTRACTED_BYTES} bytes)")

    if lower.endswith(".zip"):
        tmp = dest_dir / ".__upload.zip"
        tmp.write_bytes(data)
        try:
            _unpack_zip(tmp, dest_dir)
        finally:
            tmp.unlink(missing_ok=True)
        return dest_dir

    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        tmp = dest_dir / ".__upload.tar.gz"
        tmp.write_bytes(data)
        try:
            _unpack_tar(tmp, dest_dir)
        finally:
            tmp.unlink(missing_ok=True)
        return dest_dir

    if suffix in _SINGLE_FILE_EXTENSIONS:
        basename = Path(filename).name
        if not basename or basename in {".", ".."}:
            raise ValueError(f"Invalid filename: {filename!r}")
        target = dest_dir / basename
        # Guard against a crafted basename like "..\\..\\etc" on odd platforms.
        if target.resolve().parent != dest_dir.resolve():
            raise ValueError(f"Invalid filename: {filename!r}")
        target.write_bytes(data)
        return dest_dir

    raise ValueError(
        f"Unsupported upload type for {filename!r}: expected .zip, .tar.gz/.tgz, "
        f"or one of {sorted(_SINGLE_FILE_EXTENSIONS)}"
    )


def clone_public_repo(url: str, dest_dir: Path) -> Path:
    """Clone a public https://github.com/<org>/<repo> URL into dest_dir.

    Rejects anything that isn't a plain github.com https URL (git@, local
    paths, other hosts, query strings/credentials embedded in the URL, etc.)
    to prevent SSRF-via-arbitrary-clone. Raises ValueError on invalid URL or
    clone failure.
    """
    match = _GITHUB_URL_RE.match(url.strip())
    if not match:
        raise ValueError(
            f"Invalid repo URL: {url!r}. Expected https://github.com/<org>/<repo>"
        )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git clone timed out after 60s: {exc}") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"git executable not found: {exc}") from exc

    if proc.returncode != 0:
        raise ValueError(f"git clone failed for {url!r}: {proc.stderr.strip()}")

    return dest_dir
