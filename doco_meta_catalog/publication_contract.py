"""Bounded, pure publication evidence rules; acceptance is never approval."""

import hashlib
import json
import re

MAX_REQUESTS = 1000
MAX_HANDLES = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def graph_root(version):
    if not isinstance(version, str) or not re.fullmatch(
        r"v[0-9]{1,3}\.[0-9]{1,2}", version
    ):
        raise ValueError("graph_version_invalid")
    return "https://graph.facebook.com/" + version


def request_body(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_REQUESTS:
        raise ValueError("publication_batch_invalid")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"method", "data"}:
            raise ValueError("publication_batch_invalid")
        data = row["data"]
        code = data.get("id") if isinstance(data, dict) else None
        if (
            row["method"] not in {"UPDATE", "DELETE"}
            or not isinstance(code, str)
            or not 0 < len(code) <= 140
            or code != code.strip()
            or code in seen
            or any(ord(c) < 32 or ord(c) == 127 for c in code)
        ):
            raise ValueError("publication_batch_invalid")
        if row["method"] == "DELETE" and set(data) != {"id"}:
            raise ValueError("publication_batch_invalid")
        seen.add(code)
    body = canonical({"item_type": "PRODUCT_ITEM", "requests": rows})
    if len(body.encode()) > 8 * 1024 * 1024:
        raise ValueError("publication_batch_too_large")
    return body


def accepted(body):
    """Only usable handles make a successful HTTP response trackable."""
    if not isinstance(body, dict):
        return "Unknown", [], [], "response_invalid"
    handles = body.get("handles")
    errors = [
        row
        for row in body.get("validation_status", [])
        if isinstance(row, dict) and row.get("errors")
    ]
    if (
        not isinstance(handles, list)
        or not 1 <= len(handles) <= MAX_HANDLES
        or not all(
            isinstance(h, str) and 0 < len(h) <= 500 and not any(ord(c) < 32 for c in h)
            for h in handles
        )
        or len(set(handles)) != len(handles)
    ):
        return (
            "Rejected" if errors else "Unknown",
            [],
            errors,
            "batch_handles_unavailable",
        )
    return (
        "Rejected" if errors else "Pending",
        handles,
        errors,
        "item_rejected" if errors else "awaiting_batch_result",
    )


def batch_result(body, handle):
    """Preserve provider status; an unknown response cannot mean processed.

    The SDK exposes status as a string, not an enum. Only the known `finished`
    spelling completes polling; all other nonempty values stay pending.
    Processed means batch work ended, NOT review approval or read-back parity.
    """
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("batch_status_invalid")
    row = rows[0]
    if (
        row.get("handle") != handle
        or not isinstance(row.get("status"), str)
        or not row["status"]
    ):
        raise ValueError("batch_status_invalid")
    errors = row.get("errors") or []
    invalid = row.get("ids_of_invalid_requests") or []
    count = row.get("errors_total_count")
    if (
        not isinstance(errors, list)
        or not isinstance(invalid, list)
        or type(count) is not int
        or count < 0
    ):
        raise ValueError("batch_status_invalid")
    return {
        "handle": handle,
        "provider_status": row["status"][:100],
        "finished": row["status"].lower() == "finished",
        "rejected": bool(errors or invalid or count),
        "errors_total_count": count,
        "errors": errors,
        "ids_of_invalid_requests": invalid,
        "warnings": row.get("warnings") or [],
    }


def next_cursor(body, seen):
    """Use only an opaque cursor on our fixed endpoint, never a supplied URL."""
    paging = body.get("paging") or {}
    if not isinstance(paging, dict):
        raise ValueError("paging_invalid")
    if not paging.get("next"):
        return None
    cursors = paging.get("cursors") or {}
    cursor = cursors.get("after") if isinstance(cursors, dict) else None
    if (
        not isinstance(cursor, str)
        or not 0 < len(cursor) <= 2048
        or cursor in seen
        or any(ord(c) < 32 for c in cursor)
    ):
        raise ValueError("paging_cursor_invalid")
    return cursor


def drift(expected, observed, *, complete):
    """Compare identity and explicit prices/stock; a count match proves nothing."""
    wanted = {
        row["data"]["id"]: row["data"] for row in expected if row["method"] == "UPDATE"
    }
    actual = {row["retailer_id"]: row for row in observed}
    result = {
        "complete": bool(complete),
        "missing": [],
        "unexpected": [],
        "changed": [],
        "unknown": [],
    }
    for code, data in wanted.items():
        row = actual.get(code)
        if row is None:
            result["missing" if complete else "unknown"].append(code)
            continue
        changed, unknown = [], []
        for key in ("price", "availability", "visibility"):
            if key not in row or row[key] is None:
                unknown.append(key)
            elif str(row[key]) != str(data.get(key)):
                # Graph may return amount + separate currency, or an ISO-bearing price.
                price = str(row[key]) + " " + str(row.get("currency", ""))
                if key != "price" or price.strip() != str(data.get(key)):
                    changed.append(key)
        if changed:
            result["changed"].append({"retailer_id": code, "fields": changed})
        if unknown:
            result["unknown"].append(code)
    if complete:
        result["unexpected"] = sorted(set(actual) - set(wanted))
    result["state"] = (
        "Unknown"
        if not complete or result["unknown"]
        else (
            "Drift"
            if result["missing"] or result["unexpected"] or result["changed"]
            else "Matched"
        )
    )
    return result
