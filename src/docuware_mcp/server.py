"""docuware-mcp — MCP server exposing a DocuWare DMS to LLM-based agents."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import docuware
from fastmcp import FastMCP

from docuware_mcp.filters import (
    FilterValidationError,
    build_conditions,
    parse_combinator,
)
from docuware_mcp.schema import ArchiveSchema, describe_dialog

log = logging.getLogger("docuware_mcp")


_SCHEMA_TTL_SECONDS = 300.0

_client: Optional[docuware.Client] = None
_schema_cache: Dict[str, Tuple[float, ArchiveSchema]] = {}


def _verify_cert_from_env() -> bool:
    raw = os.environ.get("DW_VERIFY_CERT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _get_client() -> docuware.Client:
    global _client
    if _client is None:
        verify = _verify_cert_from_env()
        if not verify:
            log.warning("DW_VERIFY_CERT disabled — TLS certificate not verified")
        creds_file = os.environ.get("DW_CREDENTIALS_FILE")
        if creds_file:
            log.info("Connecting to DocuWare with credentials from %s", creds_file)
            _client = docuware.connect(
                credentials_file=creds_file, verify_certificate=verify
            )
        else:
            log.info("Connecting to DocuWare via docuware.connect()")
            _client = docuware.connect(verify_certificate=verify)
    return _client


def _resolve_archive(client: docuware.Client, name_or_id: str) -> docuware.FileCabinet:
    """Resolve an archive across all organizations by ID or display name.

    Baskets are excluded — this server exposes archives only.
    """
    candidates: List[docuware.FileCabinet] = []
    cf = name_or_id.casefold()
    for org in client.organizations:
        for fc in org.file_cabinets:
            if not isinstance(fc, docuware.FileCabinet) or fc.is_basket:
                continue
            if fc.id == name_or_id:
                return fc
            if fc.name.casefold() == cf:
                candidates.append(fc)
    if not candidates:
        raise ValueError(f"Archive not found: {name_or_id!r}")
    if len(candidates) > 1:
        names = [
            f"{c.name} [id={c.id}, org={c.organization.name}]" for c in candidates
        ]
        raise ValueError(
            f"Archive name {name_or_id!r} is ambiguous across organizations. "
            f"Use the internal ID instead. Candidates: {names}"
        )
    return candidates[0]


def _get_search_dialog(fc: docuware.FileCabinet) -> docuware.SearchDialog:
    dlg = fc.search_dialog(required=True)
    if not isinstance(dlg, docuware.SearchDialog):
        raise RuntimeError(
            f"Archive {fc.name!r} returned unexpected dialog type {type(dlg).__name__}"
        )
    return dlg


def _get_schema(client: docuware.Client, archive: str) -> ArchiveSchema:
    now = time.monotonic()
    cached = _schema_cache.get(archive)
    if cached and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
        return cached[1]
    fc = _resolve_archive(client, archive)
    dlg = _get_search_dialog(fc)
    schema = describe_dialog(dlg, fc.name, fc.id)
    _schema_cache[archive] = (now, schema)
    _schema_cache[fc.id] = (now, schema)
    log.info("Loaded schema for archive %r [id=%s]: %d fields",
             fc.name, fc.id, len(schema.fields))
    return schema


def _fields_to_dict(
    field_values: Any, allowed_ids: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Render a list of FieldValue objects to a ``{name: value}`` dict.

    If ``allowed_ids`` is given, only fields whose internal ID is in the set
    are emitted. This is used to suppress DocuWare system metadata that isn't
    part of the archive's search-dialog schema (and thus not described to
    callers via :func:`describe_archive`).
    """
    out: Dict[str, Any] = {}
    for fv in field_values or []:
        fid = getattr(fv, "id", None)
        if allowed_ids is not None and fid not in allowed_ids:
            continue
        name = getattr(fv, "name", None) or fid
        if not name:
            continue
        value = getattr(fv, "value", None)
        if value is not None and hasattr(value, "isoformat"):
            value = value.isoformat()
        out[name] = value
    return out


def _extract_doc_id(field_values: Iterable[Any]) -> Optional[str]:
    """Pull the DWDOCID value out of a FieldValue list as a string."""
    for fv in field_values or []:
        if getattr(fv, "id", None) == "DWDOCID":
            value = getattr(fv, "value", None)
            return str(value) if value is not None else None
    return None


# --- MCP server ---

mcp = FastMCP("docuware-mcp")


@mcp.tool()
def list_archives() -> List[Dict[str, str]]:
    """List archives accessible to the configured DocuWare service account.

    Analogous to ``SHOW DATABASES``. Returns each archive's display name,
    internal ID, and the organization it belongs to. Baskets are excluded.
    """
    client = _get_client()
    out: List[Dict[str, str]] = []
    for org in client.organizations:
        for fc in org.file_cabinets:
            if fc.is_basket:
                continue
            out.append({
                "name": fc.name,
                "id": fc.id,
                "organization": org.name,
            })
    log.info("list_archives → %d archives", len(out))
    return out


@mcp.tool()
def describe_archive(archive: str) -> Dict[str, Any]:
    """Describe an archive's schema: fields, types, and allowed operators.

    Analogous to ``DESCRIBE TABLE``. The ``operators`` list per field tells you
    which operators are valid in :func:`search`'s ``filters`` for that field.
    Keyword fields with a defined value list also expose ``select_list``.

    Args:
        archive: Display name or internal ID of the archive.
    """
    client = _get_client()
    schema = _get_schema(client, archive)
    return schema.to_dict()


@mcp.tool()
def search(
    archive: str,
    filters: Optional[Dict[str, Any]] = None,
    combinator: str = "AND",
    limit: int = 25,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search documents in an archive using the structured filter DSL.

    Args:
        archive: Display name or internal ID of the archive.
        filters: Dict mapping field names to either a bare value (= ``eq``) or
            a single-operator dict like ``{"gte": 100}``. Supported operators:
            ``eq``, ``like``, ``gte``, ``lte``, ``between``, ``empty``. Use
            :func:`describe_archive` to see which operators each field accepts.
        combinator: How multiple conditions are combined: ``"AND"`` (default)
            or ``"OR"``. DocuWare does not support mixed AND/OR in one query.
        limit: Maximum results to return (1–200, default 25).
        offset: Number of results to skip (client-side slicing).

    Returns:
        A dict with ``items`` (list of result dicts containing ``id``,
        ``title``, ``content_type``, ``fields``), ``count`` (server-reported
        total when known), ``limit``, and ``offset``.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    client = _get_client()
    schema = _get_schema(client, archive)
    fc = _resolve_archive(client, archive)

    if not filters:
        raise ValueError(
            "search currently requires at least one filter condition. "
            "Match-everything is not yet implemented in v1."
        )

    try:
        conditions = build_conditions(filters, schema)
    except FilterValidationError as exc:
        raise ValueError(str(exc)) from None

    op = parse_combinator(combinator)
    dlg = _get_search_dialog(fc)

    log.info(
        "search archive=%r [id=%s] filters=%s combinator=%s limit=%d offset=%d",
        fc.name, fc.id, list(filters.keys()), combinator, limit, offset,
    )

    result_iter = dlg.search(conditions, operation=op, quote=docuware.QuoteMode.NONE)
    allowed_ids = {f.internal_id for f in schema.fields}

    items: List[Dict[str, Any]] = []
    skipped = 0
    for item in result_iter:
        if skipped < offset:
            skipped += 1
            continue
        if len(items) >= limit:
            break
        items.append({
            "id": _extract_doc_id(item.fields),
            "title": item.title,
            "content_type": item.content_type,
            "fields": _fields_to_dict(item.fields, allowed_ids=allowed_ids),
        })

    return {
        "items": items,
        "count": getattr(result_iter, "count", None),
        "limit": limit,
        "offset": offset,
    }


@mcp.tool()
def get_document(archive: str, document_id: str) -> Dict[str, Any]:
    """Fetch a single document's metadata by primary-key ID.

    Returns index field values, title, and content type. Does not return file
    content — binary download will be a separate tool.

    Args:
        archive: Display name or internal ID of the archive.
        document_id: DocuWare document ID (DWDOCID).
    """
    client = _get_client()
    schema = _get_schema(client, archive)
    fc = _resolve_archive(client, archive)
    doc = fc.get_document(document_id)
    allowed_ids = {f.internal_id for f in schema.fields}
    return {
        "id": str(getattr(doc, "id", document_id)),
        "title": getattr(doc, "title", None),
        "content_type": getattr(doc, "content_type", None),
        "fields": _fields_to_dict(getattr(doc, "fields", None), allowed_ids=allowed_ids),
    }


@mcp.tool()
def status() -> Dict[str, Any]:
    """Connection health: organizations and visible archive count.

    Useful as a first call to verify credentials and surface what the
    configured service account can actually see.
    """
    try:
        client = _get_client()
        orgs_info = []
        archive_count = 0
        for org in client.organizations:
            archives = [fc for fc in org.file_cabinets if not fc.is_basket]
            archive_count += len(archives)
            orgs_info.append({
                "name": org.name,
                "id": org.id,
                "archive_count": len(archives),
            })
        return {
            "connected": True,
            "organizations": orgs_info,
            "archive_count": archive_count,
        }
    except Exception as exc:
        log.exception("status check failed")
        return {"connected": False, "error": str(exc)}


def main() -> None:
    """Entry point — run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
