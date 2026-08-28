"""Source intake and discovery-review pages (docs/console-design.md §1, §7 Day 1-2).

Owns the /new (upload or public GitHub URL -> project) and
/p/{slug}/discover (IR review -> nativegate.yaml) screens. Everything else
(project/live page, SSE streaming, build runner, deploy) belongs to other
modules under console/.
"""

from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path

import sqlite3

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, deploy, jobs, ngate, orchestrator, sources
from ..auth import get_current_user
from ..csrf import get_csrf_token, set_csrf_cookie, verify_csrf
from ..timerange import BadTimeBound, normalize_bound, to_datetime_local


def _owned_project(slug: str, user: sqlite3.Row) -> sqlite3.Row:
    """Look up a project and enforce that `user` owns it.

    Every route keyed by `slug` must go through this rather than
    `db.get_project` directly — a bare lookup lets any authenticated user
    read or mutate any other user's project by guessing/enumerating slugs.
    """
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if project["owner_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="project not found")
    return project

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Siblings under console/data/, matching console/db.py's NGATE_CONSOLE_DB
# default of "console/data/console.db".
PROJECTS_DIR = BASE_DIR / "data" / "projects"

# The repo's shared native libraries (libraries/common-cpp, libraries/petro,
# ...) — `console/Dockerfile` COPYs this alongside `console/` and
# `tools/nativegate/` for exactly this lookup. Only entries with a
# CMakeLists.txt are real linkable libraries (mirrors nativegate's own
# `_validated_libraries` check, tools/nativegate/nativegate/cli.py); the
# other subdirectories of libraries/ (e.g. libraries/demo, libraries/geometry)
# are standalone example sources, not something a service can declare as a
# `libraries:` dependency.
REPO_LIBRARIES_DIR = BASE_DIR.parent / "libraries"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    """Lowercase, alphanumeric+underscores. Falls back to "project" if empty.

    Underscores, not hyphens: this slug becomes the `ngate create-service`
    name, which lands in a generated Python package import
    (`from .<name> import ...`) — a hyphen there is invalid Python syntax and
    `ngate generate` fails with "invalid syntax" deep in __init__.py.
    """
    base = _SLUG_RE.sub("_", raw.strip().lower()).strip("_")
    return base or "project"


def unique_slug(raw: str) -> str:
    base = slugify(raw)
    slug = base
    n = 2
    while db.get_project(slug) is not None:
        slug = f"{base}_{n}"
        n += 1
    return slug


def _repo_name_from_url(url: str) -> str:
    cleaned = url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    return cleaned.rsplit("/", 1)[-1] or "project"


@router.get("/projects")
def list_projects(request: Request, user: sqlite3.Row = Depends(get_current_user)):
    projects = db.list_projects(owner_id=user["id"])
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request, "projects.html", {"projects": projects, "csrf_token": token}
    )
    set_csrf_cookie(response, token)
    return response


@router.get("/new")
def new_project_form(request: Request):
    token = get_csrf_token(request)
    response = templates.TemplateResponse(request, "new.html", {"csrf_token": token})
    set_csrf_cookie(response, token)
    return response


@router.post("/new", dependencies=[Depends(verify_csrf)])
async def create_project(
    request: Request,
    name: str = Form(""),
    repo_url: str = Form(""),
    upload: list[UploadFile] = [],
    folder_upload: list[UploadFile] = [],
    owner: sqlite3.Row = Depends(get_current_user),
):
    # A bare `<input type=file multiple>` with nothing chosen still submits
    # one UploadFile with an empty filename — filter those out rather than
    # treating them as a real (empty) upload. The `folder_upload` input is
    # also `:disabled` client-side whenever it isn't the active mode
    # (console/templates/new.html), so only one of the two should ever carry
    # files, but filter both defensively rather than trust that.
    upload = [f for f in upload if f.filename]
    folder_upload = [f for f in folder_upload if f.filename]

    if db.count_projects_for_owner(owner["id"]) >= db.MAX_PROJECTS_PER_USER:
        token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "new.html",
            {
                "error": (
                    f"Project limit reached ({db.MAX_PROJECTS_PER_USER} per "
                    "user). Delete an existing project first."
                ),
                "csrf_token": token,
            },
            status_code=400,
        )
        set_csrf_cookie(response, token)
        return response

    has_upload = bool(upload)
    has_folder = bool(folder_upload)
    has_repo = bool(repo_url.strip())

    if sum([has_upload, has_folder, has_repo]) != 1:
        token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "new.html",
            {
                "error": "Provide exactly one of: file upload, folder upload, or a public GitHub URL.",
                "csrf_token": token,
            },
            status_code=400,
        )
        set_csrf_cookie(response, token)
        return response

    if has_upload:
        display_name = name.strip() or Path(upload[0].filename).stem
    elif has_folder:
        # webkitdirectory filenames carry the picked folder as the first path
        # segment ("geometry/geometry.hpp") — use that as the default name.
        display_name = name.strip() or Path(folder_upload[0].filename).parts[0]
    else:
        display_name = name.strip() or _repo_name_from_url(repo_url)

    slug = unique_slug(display_name)
    workspace = PROJECTS_DIR / slug
    # Stage the raw upload/clone separately from `ngate create-service`'s
    # scaffold — `ngate generate <name>` resolves `services/<name>/` relative
    # to its cwd (tools/nativegate/nativegate/cli.py, SERVICES_DIR), so the
    # workspace root, not this staging dir, is what later steps use as cwd.
    staging_dir = workspace / "_upload"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        if has_upload:
            files = [(f.filename, await f.read()) for f in upload]
            sources.unpack_uploads(files, staging_dir)
        elif has_folder:
            files = [(f.filename, await f.read()) for f in folder_upload]
            sources.unpack_folder_upload(files, staging_dir)
        else:
            sources.clone_public_repo(repo_url.strip(), staging_dir)
    except ValueError as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request, "new.html", {"error": str(exc), "csrf_token": token}, status_code=400
        )
        set_csrf_cookie(response, token)
        return response

    detect_result = ngate.detect(staging_dir)
    language = None
    if detect_result.ok:
        # Best-effort: `ngate detect` output is plain text ("language: fortran"
        # or similar); pull a bare word off the first line that looks like a
        # known language, else leave it for the discover step to sort out.
        text = (detect_result.stdout or "").strip().lower()
        for lang in ("fortran", "cpp", "c++", "c"):
            if lang in text:
                language = "cpp" if lang == "c++" else lang
                break
    language = language or "cpp"

    service_dir = workspace / "services" / slug
    create_result = ngate.run(
        "create-service", slug, "--language", language, "--force", cwd=workspace
    )
    if not create_result.ok:
        token = get_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "new.html",
            {
                "error": f"ngate create-service failed: {create_result.stderr or create_result.stdout}",
                "csrf_token": token,
            },
            status_code=400,
        )
        set_csrf_cookie(response, token)
        return response

    # Overwrite the scaffold's empty native/ with what was actually uploaded.
    native_dir = service_dir / "native"
    for item in staging_dir.rglob("*"):
        if item.is_file():
            rel = item.relative_to(staging_dir)
            dest = native_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(item.read_bytes())

    project_id = db.create_project(slug, owner["id"], display_name, language)
    db.update_project_status(slug, "detected", language)

    return RedirectResponse(url=f"/p/{slug}/discover", status_code=303)


_CPP_EXTS = {".hpp", ".hh", ".h", ".cpp", ".cc", ".cxx"}
_FORTRAN_EXTS = {".f90", ".f", ".for", ".f77"}
_FORTRAN_ROUTINE_RE = re.compile(
    r"^\s*(?:recursive\s+)?(?:[a-zA-Z_]\w*\s+)*?"
    r"(?:subroutine|function)\s+(\w+)",
    re.IGNORECASE,
)


def _find_native_files(native_dir: Path, language: str | None = None) -> list[Path]:
    """Every file `ngate inspect` should be pointed at, one at a time.

    `ngate inspect` takes a single file, not a directory — but a real
    library is rarely one file. `libraries/petro/` alone has eight separate
    Fortran routine files plus a five-header C++ library; inspecting only
    "whichever file sorts first" silently hides everything else (and, worse,
    for a mixed-language folder can pick a file in the WRONG language
    entirely — flash.f over the intended petro_api.f90 wrapper, or a C++
    header despite the project being detected as Fortran). This returns
    every matching-language candidate so the caller can inspect and merge
    all of them: headers only for C++ (inspect targets interfaces, same
    convention as `ngate suggest`), every Fortran source file for Fortran
    (a Fortran file has no separate header/impl split).

    `language` (the project's already-detected language, from `ngate detect`
    at upload time) restricts candidates to that language's extensions;
    without it, both families are considered.
    """
    candidates = sorted(native_dir.rglob("*"))
    if language in ("cpp", "c++"):
        return [p for p in candidates if p.suffix.lower() in (".hpp", ".hh", ".h")]
    if language == "fortran":
        return [p for p in candidates if p.suffix.lower() in _FORTRAN_EXTS]

    headers = [p for p in candidates if p.suffix.lower() in (".hpp", ".hh", ".h")]
    fortran = [p for p in candidates if p.suffix.lower() in _FORTRAN_EXTS]
    return headers + fortran


_INCLUDE_RE = re.compile(r'#\s*include\s*[<"]([^">]+)[>"]')


def _detect_libraries(native_dir: Path, language: str | None) -> list[str]:
    """Which `libraries/<name>` a project's uploaded sources actually need.

    nativegate's `libraries:` is config-declared, not auto-discovered (see
    `_validated_libraries` in tools/nativegate/nativegate/cli.py) — it just
    trusts `nativegate.yaml` and expects `libraries/<name>` to already exist
    next to `services/`. An uploaded project has no `nativegate.yaml` of its
    own to declare that, so this fills the gap: if an uploaded source
    `#include`s a header that only exists inside one of the repo's real
    shared libraries, that library must be what it meant. Only C++ has a
    `libraries:` story in nativegate today (see cli.py: fortran always gets
    `libraries=[]`), so this is a no-op for Fortran uploads.
    """
    if language not in ("cpp", "c++") or not REPO_LIBRARIES_DIR.is_dir():
        return []

    included = set()
    own_headers = set()
    for path in native_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in (".hpp", ".hh", ".h"):
            own_headers.add(path.name)
        if path.suffix.lower() in _CPP_EXTS:
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for match in _INCLUDE_RE.finditer(text):
                included.add(Path(match.group(1)).name)

    # An `#include "x.hpp"` naming a header the project itself already ships
    # resolves to that local file, not a same-named header in some shared
    # library — matching on bare filename alone would otherwise misdetect a
    # dependency on a library that just happens to share a header's name.
    included -= own_headers

    if not included:
        return []

    detected = []
    for lib_dir in sorted(REPO_LIBRARIES_DIR.iterdir()):
        if not lib_dir.is_dir() or not (lib_dir / "CMakeLists.txt").is_file():
            continue
        lib_headers = {
            p.name for p in lib_dir.rglob("*") if p.suffix.lower() in (".hpp", ".hh", ".h")
        }
        if included & lib_headers:
            detected.append(lib_dir.name)
    return detected


def _other_language_files(native_dir: Path, language: str | None) -> list[Path]:
    """Files under native_dir that belong to the OTHER language family.

    Purely informational — surfaced on the discover screen so a mixed-folder
    upload (fortran/ + cpp/ both present) is visible rather than silently
    narrowed down with no explanation for why half the uploaded files never
    show up as discovered functions.
    """
    if language not in ("cpp", "c++", "fortran"):
        return []
    other = _FORTRAN_EXTS if language in ("cpp", "c++") else _CPP_EXTS
    return sorted(p for p in native_dir.rglob("*") if p.suffix.lower() in other)


def _guess_fortran_routines(path: Path) -> list[str]:
    """Regex fallback: `ngate inspect` requires --function for Fortran, so we
    need candidate names before we can call it. Best-effort only — this is
    exactly the kind of guess docs/console-design.md §1 says must be shown to
    the user rather than trusted silently, and the real signature/intent info
    still comes from `ngate inspect`, not this regex.
    """
    names: list[str] = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return names
    for line in text.splitlines():
        m = _FORTRAN_ROUTINE_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


@router.get("/p/{slug}/discover")
def discover(request: Request, slug: str, user: sqlite3.Row = Depends(get_current_user)):
    project = _owned_project(slug, user)

    workspace = PROJECTS_DIR / slug
    native_dir = workspace / "services" / slug / "native"

    functions: list[dict] = []
    unresolved: list[dict] = []
    ir_error: str | None = None

    other_lang_files = _other_language_files(native_dir, project["language"])
    if other_lang_files:
        other_label = "Fortran" if project["language"] in ("cpp", "c++") else "C++"
        names = ", ".join(p.name for p in other_lang_files[:8])
        more = f" (+{len(other_lang_files) - 8} more)" if len(other_lang_files) > 8 else ""
        unresolved.append(
            {
                "text": (
                    f"{len(other_lang_files)} {other_label} file(s) were also found in "
                    f"this upload ({names}{more}) but are not part of this project — it "
                    f"was detected as {project['language']}, and a service is "
                    "single-language. Upload the other language's files as a separate "
                    "project if you need to expose both."
                )
            }
        )

    targets = _find_native_files(native_dir, project["language"])
    if not targets:
        ir_error = f"No recognizable C++/Fortran source found under {native_dir}."
    else:
        seen_names: set[str] = set()
        for target in targets:
            fn_names = (
                _guess_fortran_routines(target)
                if target.suffix.lower() in _FORTRAN_EXTS
                else None
            )
            try:
                ir = ngate.inspect_ir(target, functions=fn_names)
            except RuntimeError as exc:
                unresolved.append({"text": f"{target.name}: {exc}"})
                continue

            # A header with declared-but-not-defined methods compiles fine
            # but fails at *import* time with a confusing "symbol not found"
            # dlopen error, because pybind11 binds a method whose body was
            # never linked in (see tools/nativegate/nativegate/cli.py's own
            # comment on this). `ngate quickstart` auto-discovers a header's
            # sibling .cpp; this console's manual create-service+generate
            # path does not, so warn here rather than let it surface as a
            # build failure three steps later.
            if target.suffix.lower() in (".hpp", ".hh", ".h") and ir.get("classes"):
                sibling_exts = (".cpp", ".cc", ".cxx")
                has_impl = any(
                    (target.parent / f"{target.stem}{ext}").exists() for ext in sibling_exts
                )
                if not has_impl:
                    unresolved.append(
                        {
                            "text": (
                                f"{target.name} declares classes but no matching "
                                f"{target.stem}.cpp/.cc/.cxx was found alongside it. "
                                "The extension will still build, but importing it will "
                                "fail with a 'symbol not found' error at runtime unless "
                                "the implementation file is uploaded too."
                            )
                        }
                    )

            file_fns = list(ir.get("functions", []))
            for cls in ir.get("classes", []):
                for method in cls.get("methods", []):
                    file_fns.append({**method, "name": f"{cls['name']}.{method['name']}"})

            for fn in file_fns:
                # A Fortran routine name is unique per project (no
                # namespacing), but the same free function can otherwise
                # legitimately appear if two headers both got matched by a
                # loose `--function` guess — skip an exact duplicate name
                # rather than showing the same checkbox twice.
                name = fn.get("name")
                dedup_key = f"{target.name}:{name}" if project["language"] == "cpp" else name
                if dedup_key in seen_names:
                    continue
                seen_names.add(dedup_key)

                functions.append(
                    {
                        "name": name,
                        "parameters": fn.get("parameters", []),
                        "returns": fn.get("returns"),
                        "doc": fn.get("doc"),
                        # Best-effort: richer IR (line numbers / source
                        # snippets) is not emitted by `ngate inspect`'s
                        # current plain-text output (see console/ngate.py's
                        # module docstring), so we can only show the
                        # inferred signature clearly for now.
                        "source_line": fn.get("source_line"),
                        "source_snippet": fn.get("source_snippet"),
                        "source_file": target.name,
                    }
                )

            # Anything `ngate inspect` flagged as a free-text note
            # (diagnostics, skipped symbols) surfaces as "unresolved", tagged
            # with which file it came from now that there can be several.
            for note in ir.get("notes", []):
                unresolved.append({"text": f"{target.name}: {note}"})

        if not functions and not ir_error:
            ir_error = (
                f"Found {len(targets)} {project['language'] or ''} source file(s) but "
                "none exposed any functions/classes ngate could parse."
            )

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "discover.html",
        {
            "project": project,
            "functions": functions,
            "unresolved": unresolved,
            "ir_error": ir_error,
            "csrf_token": token,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/p/{slug}/discover", dependencies=[Depends(verify_csrf)])
async def submit_discover(
    request: Request, slug: str, user: sqlite3.Row = Depends(get_current_user)
):
    project = _owned_project(slug, user)

    form = await request.form()
    selected = [v for k, v in form.multi_items() if k == "function"]

    # `expose:` takes whole class names (exposing every method on the class)
    # and bare free-function names — NOT per-method qualified names — see
    # nativegate/config.py's `ExposeConfig.is_exposed`. The discovery table
    # shows individual methods as "Class.method" (console/routes/pages.py's
    # `discover()` flattens class methods this way for display), so checking
    # one method here opts the whole class in; there is no per-method
    # exposure granularity in the current nativegate.yaml schema.
    classes: list[str] = []
    functions: list[str] = []
    for name in selected:
        if "." in name:
            cls = name.split(".", 1)[0]
            if cls not in classes:
                classes.append(cls)
        else:
            functions.append(name)

    workspace = PROJECTS_DIR / slug
    service_dir = workspace / "services" / slug
    native_dir = service_dir / "native"

    manifest = {
        "name": project["slug"],
        "language": project["language"],
        "expose": {"classes": classes, "functions": functions},
    }

    # `ngate build` resolves `libraries:` entries relative to its cwd
    # (workspace, per the comment below) — copy each detected library in
    # from the repo's own libraries/ before declaring it, so the build
    # actually finds it there instead of just failing later with the same
    # "no such file" error the declaration was meant to prevent.
    detected_libraries = _detect_libraries(native_dir, project["language"])
    if detected_libraries:
        libraries_dir = workspace / "libraries"
        for lib_name in detected_libraries:
            dest = libraries_dir / lib_name
            # Always re-copy rather than skip when dest already exists — a
            # rediscover after the repo's library changed (or after a
            # previous copytree was interrupted) must pick up the current
            # sources, not silently build against a stale/partial copy.
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(REPO_LIBRARIES_DIR / lib_name, dest)
        manifest["libraries"] = detected_libraries

    manifest_path = service_dir / "nativegate.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    db.update_project_status(slug, "configured")
    build_id = db.create_build(project["id"])

    # `ngate generate/build <name>` resolve `services/<name>/` relative to
    # cwd, so the build runner's cwd is the workspace root, not service_dir.
    threading.Thread(
        target=jobs.run_build,
        args=(slug, build_id, workspace),
        daemon=True,
    ).start()

    return RedirectResponse(url=f"/p/{slug}/build/{build_id}", status_code=303)


CALLS_PAGE_SIZE = 100
CALLS_MAX_PAGE_SIZE = 500


@router.get("/p/{slug}/calls")
def calls_page(
    request: Request,
    slug: str,
    build_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    limit: int = CALLS_PAGE_SIZE,
    offset: int = 0,
    user: sqlite3.Row = Depends(get_current_user),
):
    project = _owned_project(slug, user)
    service = deploy.service_status(slug)

    # Clamp rather than 400: these are user-editable query params on a
    # read-only view, and an absurd limit is a denial-of-service on our own
    # SQLite connection, not a user error worth an error page.
    limit = max(1, min(limit, CALLS_MAX_PAGE_SIZE))
    offset = max(0, offset)

    # A build filter naming a build that isn't this project's would otherwise
    # render an empty table as if the build simply served no calls.
    if build_id is not None:
        build = db.get_build(build_id)
        if build is None or build["project_id"] != project["id"]:
            raise HTTPException(status_code=404, detail="build not found")

    since = (since or "").strip() or None
    until = (until or "").strip() or None
    kind = (kind or "").strip().lower() or None
    if kind not in (None, "rest", "mcp", "mcp_http"):
        kind = None
    q = (q or "").strip() or None

    # Same normalization the evidence pack uses, shared rather than
    # duplicated: passing a bare `until` through unextended made this page
    # under-report the last day of a range while the signed export for the
    # identical range included it.
    #
    # A typo in a date field flags the filter and shows the unfiltered page.
    # Failing the request outright would take the build filter, the
    # pagination and the history itself down with it.
    filter_error = None
    try:
        since_norm = normalize_bound(since, end_of_day=False)
        until_norm = normalize_bound(until, end_of_day=True)
    except BadTimeBound as exc:
        filter_error = str(exc)
        since_norm = until_norm = None
        since = until = None

    filters = {
        "since": since_norm,
        "until": until_norm,
        "build_id": build_id,
        "kind": kind,
        "q": q,
    }
    calls_rows = db.get_service_calls(project["id"], limit=limit, offset=offset, **filters)
    total = db.count_service_calls(project["id"], **filters)
    # Identical query when nothing is filtered, and this is the page's hot path.
    any_filter = any(v is not None for v in filters.values())
    unfiltered_total = db.count_service_calls(project["id"]) if any_filter else total

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "calls.html",
        {
            "project": project,
            "service": service,
            "calls": calls_rows,
            "builds": db.list_builds(project["id"]),
            "filter_build_id": build_id,
            "filter_since": since,
            "filter_until": until,
            # Distinct from filter_since/filter_until: those are the raw
            # strings (used in the "Filtered ... from X" banner text), these
            # are reshaped for the datetime-local input's value attribute,
            # which rejects any form normalize_bound accepts but it can't
            # render (a bare date, a space separator, trailing seconds).
            "filter_since_input": to_datetime_local(since),
            "filter_until_input": to_datetime_local(until),
            # The live SSE rows are matched client-side against these, which
            # must be the *normalized* bounds the server queried with — the
            # raw text would let a live row be accepted where a reload would
            # reject it, so the pane and the page would disagree.
            "filter_since_norm": since_norm,
            "filter_until_norm": until_norm,
            "filter_error": filter_error,
            "filter_kind": kind,
            "filter_q": q,
            "filter_active": bool(build_id or since or until or kind or q),
            "total": total,
            "unfiltered_total": unfiltered_total,
            "limit": limit,
            "offset": offset,
            "csrf_token": token,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.get("/p/{slug}/build/{build_id}")
def build_page(
    request: Request,
    slug: str,
    build_id: int,
    user: sqlite3.Row = Depends(get_current_user),
):
    project = _owned_project(slug, user)
    build = db.get_build(build_id)
    if build is None or build["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="build not found")
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "build.html",
        {"project": project, "build_id": build_id, "build": build, "csrf_token": token},
    )
    set_csrf_cookie(response, token)
    return response


@router.get("/p/{slug}")
def project_page(
    request: Request, slug: str, user: sqlite3.Row = Depends(get_current_user)
):
    project = _owned_project(slug, user)
    service = deploy.service_status(slug)
    builds = db.list_builds(project["id"])
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "project.html",
        {"project": project, "service": service, "builds": builds, "csrf_token": token},
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/p/{slug}/delete", dependencies=[Depends(verify_csrf)])
def delete_project(slug: str, user: sqlite3.Row = Depends(get_current_user)):
    """Tear down everything a project accumulated: container, image, on-disk
    workspace, and DB rows — this is the cleanup path for the container/image
    buildup that comes from repeated upload/build cycles during testing.
    """
    project = _owned_project(slug, user)

    # Best-effort: a stuck container/image shouldn't block deleting the
    # project record — the user asked to clean up, so make forward progress
    # even if docker itself is in a weird state.
    try:
        deploy.stop_service(slug)
    except RuntimeError:
        pass
    deploy.remove_image(slug)

    workspace = PROJECTS_DIR / slug
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)

    orchestrator.forget(slug)
    db.delete_project(project["id"])

    return RedirectResponse(url="/projects", status_code=303)


@router.post("/p/{slug}/rebuild", dependencies=[Depends(verify_csrf)])
def rebuild_project(slug: str, user: sqlite3.Row = Depends(get_current_user)):
    """Re-run the build pipeline against the project's existing
    `nativegate.yaml` — no need to redo discovery/function-selection.

    This is the "unstuck" path for a failed build: many failures (docker not
    running, a transient network blip during `pip wheel`, a toolchain that
    got fixed in between attempts) have nothing to do with the function
    selection, so making a retry require walking back through discovery
    would be a dead end for exactly the situation where a user needs a way
    forward the most.
    """
    project = _owned_project(slug, user)
    workspace = PROJECTS_DIR / slug
    manifest_path = workspace / "services" / slug / "nativegate.yaml"
    if not manifest_path.exists():
        raise HTTPException(
            status_code=400,
            detail="No configuration to rebuild from yet — run discovery first.",
        )

    build_id = db.create_build(project["id"])
    threading.Thread(
        target=jobs.run_build,
        args=(slug, build_id, workspace),
        daemon=True,
    ).start()

    return RedirectResponse(url=f"/p/{slug}/build/{build_id}", status_code=303)
