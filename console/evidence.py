"""Signed evidence packs: an exportable, offline-verifiable record of the
calls this console recorded for one deployed service over a date range.

The pack exists to be *forwarded*. Its reader is a compliance owner who has
no account on this console, no access to this host, and no reason to trust
it — so everything needed to check the document has to travel inside the
document (or beside it, as the standalone verifier this module also ships).

Canonicalization
----------------
The signature covers exactly::

    json.dumps(doc_without_signature, sort_keys=True,
               separators=(",", ":"), ensure_ascii=False).encode("utf-8")

where ``doc_without_signature`` is the pack with its top-level ``signature``
key removed and nothing else changed. Sorted keys at every level, no
insignificant whitespace, non-ASCII left as real UTF-8 characters. This rule
is restated inside every pack (``canonicalization``) because a verifier that
cannot reproduce these bytes exactly cannot verify anything, and the rule is
not recoverable from the signature itself.

Key handling
------------
One Ed25519 keypair per console, generated on first use and stored beside
the SQLite DB (so the ``console-data`` volume carries it across container
rebuilds). It is never regenerated while the private key file exists:
rotating the key would silently invalidate every pack ever issued, and a
compliance record that stops verifying later is worse than no record.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PACK_FORMAT = "nativegate-evidence/v1"

CANONICALIZATION_RULE = (
    "Remove the top-level \"signature\" key, then serialize the remaining "
    "document as JSON with sorted keys at every level, separators ',' and "
    "':' (no insignificant whitespace), and non-ASCII characters left "
    "unescaped; encode that string as UTF-8. The signature is Ed25519 over "
    "exactly those bytes, and public_key is the raw 32-byte Ed25519 public "
    "key, base64-encoded."
)

ATTESTATION_CLAIMS = [
    "These are the service calls this Nativegate console recorded for the "
    "named project within the stated time range, exactly as stored in its "
    "database at generated_at.",
    "The document has not been altered since it was signed: any change to "
    "any byte of any field other than the signature itself invalidates the "
    "signature.",
    "It was signed by the long-lived Ed25519 key of the console that "
    "produced it, so packs bearing the same public_key came from the same "
    "console.",
]

ATTESTATION_DISCLAIMERS = [
    "It does NOT prove the identity of any caller. The console records what "
    "the service reported; it performs no authentication of callers on the "
    "service's behalf.",
    "It does NOT prove completeness. Calls that never reached the service, "
    "or that the service did not log, or that were made against another "
    "deployment of the same code, cannot appear here.",
    "It does NOT prove the recorded build actually produced any particular "
    "downstream number; it records which build_id and image_digest the "
    "service reported for each call.",
    "It does NOT establish who operates the signing console. Binding "
    "public_key to an organization is out of band — confirm the key "
    "fingerprint with the sender through a channel you already trust.",
    "A valid signature therefore attests to the integrity and origin of "
    "this record, not to the truth of the underlying events.",
]

# The console's own key files, siblings of console.db so the `console-data`
# volume persists them (a key that dies with the container would invalidate
# every previously issued pack on the next rebuild).
PRIVATE_KEY_FILENAME = "evidence_signing_key.pem"
PUBLIC_KEY_FILENAME = "evidence_signing_key.pub"

# Paging size for pulling the full matching range out of the DB. A pack must
# contain every matching row, not the first page.
_PAGE_SIZE = 500


def _key_dir() -> Path:
    from console import db

    return db.get_db_path().resolve().parent


def load_or_create_key(key_dir: Path | None = None) -> Ed25519PrivateKey:
    """Return this console's signing key, generating it only if absent."""
    directory = Path(key_dir) if key_dir is not None else _key_dir()
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / PRIVATE_KEY_FILENAME

    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError(
                f"{private_path} is not an Ed25519 private key. Refusing to "
                "replace it — move it aside by hand if you truly intend to "
                "retire it, understanding that packs signed with it can no "
                "longer be traced to this console."
            )
        return key

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Create with 0600 already in place rather than chmod-ing after the
    # write: a world-readable window, however brief, is enough for a
    # co-tenant process to copy a key that can then forge every future pack.
    try:
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost a race with a concurrent first use — two requests can both find
        # the key absent (the page rendering the public key while a download
        # signs a pack, say). O_EXCL is what makes that safe: the loser adopts
        # the winner's key rather than 500-ing, and neither overwrites, so a
        # single console never ends up with two identities.
        return load_or_create_key(directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)

    (directory / PUBLIC_KEY_FILENAME).write_text(public_key_b64(key.public_key()) + "\n")
    return key


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """Serialize a pack for signing/verification. See module docstring."""
    unsigned = {k: v for k, v in doc.items() if k != "signature"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def summarize_builds(calls: list[dict]) -> list[dict]:
    """Which binaries served this period, at a glance.

    Grouped on the (build_id, image_digest) pair rather than build_id alone:
    a build id that maps to two different digests is precisely the anomaly a
    reviewer needs to see, and collapsing on id would hide it.
    """
    counts: dict[tuple[Any, Any], int] = {}
    for call in calls:
        key = (call.get("build_id"), call.get("image_digest"))
        counts[key] = counts.get(key, 0) + 1
    summary = [
        {"build_id": build_id, "image_digest": digest, "call_count": n}
        for (build_id, digest), n in counts.items()
    ]
    summary.sort(key=lambda item: (item["build_id"] is None, item["build_id"] or 0))
    return summary


def collect_calls(
    db_module,
    project_id: int,
    *,
    since: str | None,
    until: str | None,
    build_id: int | None,
) -> list[dict]:
    """Every matching row, paged out of the DB rather than one capped page.

    Deduplicated by row id across pages. OFFSET paging is not stable against
    a concurrent writer, and there is always one here — the tailer keeps
    inserting newest-first rows while an export runs. A row arriving mid-walk
    shifts every later page down by one, so an already-collected row comes
    back on the next page and lands in the pack twice, inflating a
    *cryptographically signed* call_count. A duplicate is the one error this
    document must not contain, since its whole purpose is to be counted on.
    """
    calls: list[dict] = []
    seen: set = set()
    offset = 0
    while True:
        page = db_module.get_service_calls(
            project_id,
            since=since,
            until=until,
            build_id=build_id,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        if not page:
            break
        for row in page:
            key = row.get("id")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            calls.append(row)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return calls


def build_pack(
    *,
    project_slug: str,
    project_name: str,
    calls: list[dict],
    since: str | None,
    until: str | None,
    build_id: int | None,
    private_key: Ed25519PrivateKey,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble and sign one evidence pack."""
    doc: dict[str, Any] = {
        "format": PACK_FORMAT,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": {"slug": project_slug, "name": project_name},
        "range": {"since": since, "until": until},
        "filter": {"build_id": build_id},
        "attestation": {
            "summary": (
                "Signed record of the calls this Nativegate console recorded "
                "for one deployed service. Read what_this_proves and "
                "what_this_does_not_prove before relying on it."
            ),
            "what_this_proves": ATTESTATION_CLAIMS,
            "what_this_does_not_prove": ATTESTATION_DISCLAIMERS,
        },
        "canonicalization": {
            "algorithm": "Ed25519",
            "rule": CANONICALIZATION_RULE,
            "reference_python": (
                'json.dumps({k: v for k, v in pack.items() if k != "signature"}, '
                "sort_keys=True, separators=(',', ':'), "
                'ensure_ascii=False).encode("utf-8")'
            ),
        },
        "verification": {
            "how": (
                "Run the standalone verifier shipped with this pack: "
                "`python3 verify_evidence.py <this-file>`. It needs Python 3.8+ "
                "and the `cryptography` package (`pip install cryptography`), "
                "no network access, and nothing from the console that produced "
                "the pack."
            ),
            "key_trust": (
                "The verifier confirms the pack was signed by the key in "
                "public_key. Confirming that key belongs to whoever you think "
                "it does is a separate step: compare public_key against a copy "
                "you obtained through a channel you already trust."
            ),
        },
        "call_count": len(calls),
        "builds": summarize_builds(calls),
        "calls": calls,
        "public_key": public_key_b64(private_key.public_key()),
    }
    doc["signature"] = base64.b64encode(private_key.sign(canonical_bytes(doc))).decode("ascii")
    return doc


def verify_pack(doc: dict[str, Any]) -> tuple[bool, str]:
    """Check a pack's self-signature. Mirrors the standalone verifier."""
    from cryptography.exceptions import InvalidSignature

    # The standalone verifier rejects a foreign format outright. Without the
    # same check here the two disagree on the same document — and the one the
    # recipient runs is the strict one, so the console would be the lenient
    # voice about a document it issued.
    if doc.get("format") != PACK_FORMAT:
        return False, f"not a {PACK_FORMAT} document"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(doc["public_key"], validate=True)
        )
        signature = base64.b64decode(doc["signature"], validate=True)
    except Exception as exc:
        return False, f"malformed pack: {exc}"
    try:
        public_key.verify(signature, canonical_bytes(doc))
    except InvalidSignature:
        return False, "signature does NOT match the document contents"
    return True, "signature valid"


_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def pack_filename(slug: str, since: str | None, until: str | None) -> str:
    # `since`/`until` reach here straight from a form field, and the result is
    # interpolated into a quoted Content-Disposition value. A bare `"` would
    # end that quoted string early (spoofing the saved filename) and CR/LF
    # would split the header outright, so everything outside the safe set is
    # dropped rather than escaped — a mangled filename is a fine outcome for
    # input that should never have contained those characters.
    span = f"{since or 'all'}_{until or 'now'}".replace(":", "").replace(" ", "T")
    return _UNSAFE_IN_FILENAME.sub("", f"evidence-{slug}-{span}.json")[:180]


# --- standalone verifier --------------------------------------------------
#
# Shipped as a separate downloadable file rather than as a mode of the
# console CLI, and duplicated here as a source string rather than imported:
# the recipient has no repo checkout and no console, so the verifier has to
# be a single file they can save next to the pack and run. It deliberately
# has no imports beyond the stdlib and `cryptography`, prints plain English,
# and exits non-zero on failure so it also works in a CI check.

VERIFIER_SOURCE = r'''#!/usr/bin/env python3
"""Verify a Nativegate signed evidence pack (format nativegate-evidence/v1).

Usage:
    pip install cryptography          # one time
    python3 verify_evidence.py evidence-<project>-<range>.json

Prints what the pack covers and whether its signature is intact. Exit code
0 means the signature is valid, 1 means it is not (or the file is unusable).

No network access, no database, no console access required.
"""

import base64
import json
import sys

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    sys.exit("This verifier needs the 'cryptography' package. Run: pip install cryptography")


def canonical_bytes(pack):
    """The exact bytes the signature covers. Must match the pack's
    'canonicalization' field byte for byte."""
    unsigned = {k: v for k, v in pack.items() if k != "signature"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def main(argv):
    if len(argv) != 2:
        sys.exit("Usage: python3 verify_evidence.py <evidence-pack.json>")

    try:
        with open(argv[1], "rb") as handle:
            pack = json.loads(handle.read().decode("utf-8"))
    except Exception as exc:
        sys.exit("Could not read the pack as JSON: %s" % exc)

    if pack.get("format") != "nativegate-evidence/v1":
        sys.exit("Not a nativegate-evidence/v1 pack (format=%r)." % pack.get("format"))

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pack["public_key"], validate=True))
        signature = base64.b64decode(pack["signature"], validate=True)
    except Exception as exc:
        sys.exit("Pack is missing or has a malformed key/signature: %s" % exc)

    project = pack.get("project") or {}
    rng = pack.get("range") or {}
    print("Evidence pack")
    print("  project      : %s (%s)" % (project.get("name"), project.get("slug")))
    print("  range        : %s .. %s" % (rng.get("since") or "(open)",
                                         rng.get("until") or "(open)"))
    print("  generated at : %s" % pack.get("generated_at"))
    print("  calls        : %s" % pack.get("call_count"))
    print("  signing key  : %s" % pack.get("public_key"))
    print("")
    print("Builds that served these calls:")
    for entry in pack.get("builds") or []:
        print("  build %-6s %-75s %s calls" % (
            entry.get("build_id"), entry.get("image_digest") or "(not recorded)",
            entry.get("call_count")))
    if not pack.get("builds"):
        print("  (none - no calls in range)")
    print("")

    try:
        public_key.verify(signature, canonical_bytes(pack))
    except InvalidSignature:
        print("RESULT: INVALID.")
        print("The signature does not match this document. It was either")
        print("modified after signing, or signed by a different key than the")
        print("one it carries. Do not rely on its contents.")
        return 1

    print("RESULT: VALID.")
    print("This document is unchanged since it was signed by the key above.")
    print("")
    print("What that establishes:")
    for line in (pack.get("attestation") or {}).get("what_this_proves") or []:
        print("  + %s" % line)
    print("")
    print("What it does NOT establish:")
    for line in (pack.get("attestation") or {}).get("what_this_does_not_prove") or []:
        print("  - %s" % line)
    print("")
    print("A valid signature says nothing about who owns the signing key.")
    print("Confirm the key above against a copy you got through a channel you")
    print("already trust before treating this as evidence from that party.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''
