"""Bounded account-scoped observations. Missing rows never imply approval."""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from doco_meta_catalog import publication, publication_contract as contract, sync

_DOCTYPE = "Meta Catalog Diagnostic"
_FIELDS = "retailer_id,review_status,capability_to_review_status,errors,price,currency,availability,visibility"


def _fetch(settings, max_pages=60):
    if type(max_pages) is not int or not 1 <= max_pages <= 60:
        raise ValueError("diagnostic_page_limit_invalid")
    out, seen_ids, cursors, cursor, size = [], set(), set(), None, 0
    pages = 0
    try:
        for _ in range(max_pages):
            params = {"fields": _FIELDS, "limit": 200}
            if cursor:
                params["after"] = cursor
            status, body = publication.graph_request(
                "GET", settings, "products", params=params
            )
            if (
                status != 200
                or not isinstance(body, dict)
                or not isinstance(body.get("data"), list)
            ):
                raise ValueError("diagnostic_response_invalid")
            rows = body["data"]
            if len(rows) > 200:
                raise ValueError("diagnostic_response_limit")
            size += len(contract.canonical(rows).encode())
            if size > 16 * 1024 * 1024:
                raise ValueError("diagnostic_response_limit")
            for row in rows:
                code = row.get("retailer_id") if isinstance(row, dict) else None
                if (
                    not isinstance(code, str)
                    or not 0 < len(code) <= 140
                    or code in seen_ids
                ):
                    raise ValueError("diagnostic_identity_invalid")
                seen_ids.add(code)
                out.append(row)
            pages += 1
            cursor = contract.next_cursor(body, cursors)
            if cursor is None:
                return {
                    "items": out,
                    "complete": True,
                    "pages": pages,
                    "reason_code": "",
                }
            cursors.add(cursor)
        reason = "diagnostic_page_limit"
    except (ValueError, TypeError, UnicodeError, publication.requests.RequestException):
        reason = "diagnostic_scan_incomplete"
    return {"items": out, "complete": False, "pages": pages, "reason_code": reason}


def _caps(item):
    return {
        row["capability"]: row.get("review_status")
        for row in item.get("capability_to_review_status") or []
        if isinstance(row, dict) and row.get("capability")
    }


def _wa(caps):
    # Marketing-message review and shop connection are distinct capabilities.
    return caps.get("WHATSAPP") or caps.get("WHATSAPP_SHOPPING")


@frappe.whitelist()
def run_diagnostics(store=1):
    frappe.only_for("System Manager")
    settings = sync._get_settings()
    if not settings:
        frappe.throw("Meta catalog is not enabled / configured.")
    identity = publication.scope(settings)
    # The source snapshot is taken once. It is an observed comparison, not a claim
    # that catalog/source stayed unchanged during this non-atomic provider scan.
    source_reason = ""
    try:
        expected, _ = sync._build_payloads(None, settings)
    except (frappe.ValidationError, ValueError):
        expected = None
        source_reason = "canonical_price_or_source_unavailable"
    fetched = _fetch(settings)
    by_review, by_channel, actionable = {}, {}, []
    for item in fetched["items"]:
        review = item.get("review_status") or "unknown"
        by_review[review] = by_review.get(review, 0) + 1
        caps = _caps(item)
        for capability, status in caps.items():
            by_channel.setdefault(capability, {})
            counts = by_channel[capability]
            counts[status or "unknown"] = counts.get(status or "unknown", 0) + 1
        if review.lower() != "approved" or item.get("errors") or not _wa(caps):
            actionable.append(item["retailer_id"])
    state = (
        "Complete"
        if fetched["complete"]
        else "Partial"
        if fetched["items"]
        else "Unknown"
    )
    summary = {
        "state": state,
        "complete": fetched["complete"],
        "reason_code": fetched["reason_code"],
        "total": len(fetched["items"]),
        "by_review": by_review,
        "by_channel": by_channel,
        "actionable": len(actionable),
        "sample": actionable[:10],
        "drift": contract.drift(
            expected, fetched["items"], complete=fetched["complete"]
        )
        if expected is not None
        else {"state": "Unknown", "reason_code": source_reason},
        "catalog_id": identity["catalog_id"],
        "account_name": identity["account_name"],
        "scope_revision": identity["scope_revision"],
        "checked_at": str(now_datetime()),
    }
    if int(store):
        run = publication.mark(
            frappe.get_doc(
                {
                    "doctype": publication.RUN,
                    **identity,
                    "state": state,
                    "reason_code": fetched["reason_code"],
                    "checked_at": summary["checked_at"],
                    "pages": fetched["pages"],
                    "observed_count": summary["total"],
                    "summary": contract.canonical(summary),
                }
            )
        ).insert(ignore_permissions=True)
        _store(run, fetched["items"])
        summary["diagnostic_run"] = run.name
    return summary


def _store(run, items):
    """Append every observed item, including approved; retain old/scoped history."""
    for item in items:
        caps = _caps(item)
        publication.mark(
            frappe.get_doc(
                {
                    "doctype": _DOCTYPE,
                    "diagnostic_run": run.name,
                    "catalog_id": run.catalog_id,
                    "account_name": run.account_name,
                    "scope_revision": run.scope_revision,
                    "retailer_id": item["retailer_id"],
                    "review_status": item.get("review_status") or "unknown",
                    "fb_status": caps.get("FB_SHOPS"),
                    "ig_status": caps.get("INSTAGRAM_SHOPPING"),
                    "wa_status": _wa(caps),
                    "errors": contract.canonical(item.get("errors") or [])[:1000],
                    "checked_at": run.checked_at,
                    "observed": contract.canonical(item),
                }
            )
        ).insert(ignore_permissions=True)


@frappe.whitelist()
def item_diagnostic(retailer_id):
    frappe.only_for("System Manager")
    if not isinstance(retailer_id, str) or not 0 < len(retailer_id) <= 140:
        frappe.throw("Invalid retailer ID.")
    settings = frappe.get_doc(sync.SETTINGS_DOCTYPE)
    identity = publication.scope(settings)
    run = frappe.db.get_value(
        publication.RUN,
        {"scope_revision": identity["scope_revision"]},
        ["name", "state", "checked_at", "reason_code"],
        as_dict=True,
        order_by="creation desc, name desc",
    )
    if not run:
        return {
            "retailer_id": retailer_id,
            "state": "Unknown",
            "reason_code": "no_scoped_snapshot",
        }
    row = frappe.db.get_value(
        _DOCTYPE,
        {"diagnostic_run": run.name, "retailer_id": retailer_id},
        ["review_status", "fb_status", "ig_status", "wa_status", "errors", "observed"],
        as_dict=True,
    )
    return {
        "retailer_id": retailer_id,
        "state": "Observed"
        if row
        else "Missing"
        if run.state == "Complete"
        else "Unknown",
        "diagnostic_run": run.name,
        "scan_state": run.state,
        "checked_at": run.checked_at,
        "reason_code": run.reason_code,
        "catalog_id": identity["catalog_id"],
        "account_name": identity["account_name"],
        **(row or {}),
    }
