"""MA-10 — Catalog Diagnostics (per-item review status + per-channel capability).

Reads each catalog product's review_status + capability_to_review_status (FB Shops /
Instagram / WhatsApp) + errors from the Graph API, so "why isn't product X sendable on
WhatsApp" is answerable. WHATSAPP capability stuck at NO_REVIEW = in the catalog but not
commerce-approved (the Business-Verification gate). Stores the ACTIONABLE rows
(rejected / pending / with errors) in Meta Catalog Diagnostic for a Desk report, and
returns a summary (counts by review status + per channel).
"""
from __future__ import annotations

import json

import frappe
import requests
from frappe.utils import now_datetime

from doco_meta_catalog import sync

_DOCTYPE = "Meta Catalog Diagnostic"
_FIELDS = "retailer_id,review_status,capability_to_review_status,errors"


def _fetch(settings, max_pages: int = 60) -> list:
    """GET catalog products with diagnostics fields, following Graph paging (next URL
    already carries the cursor + token)."""
    out: list = []
    url = f"{settings.get_graph_root()}/{settings.catalog_id}/products"
    params = {"fields": _FIELDS, "limit": 200, "access_token": settings.get_token()}
    for _ in range(max_pages):
        r = requests.get(url, params=params, timeout=60) if params else requests.get(url, timeout=60)
        if r.status_code >= 400:
            frappe.log_error(title=f"Catalog diagnostics GET {r.status_code}", message=r.text[:500])
            r.raise_for_status()
        body = r.json() or {}
        out.extend(body.get("data", []))
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, None
    return out


def _caps(item: dict) -> dict:
    """capability_to_review_status (list of {capability, review_status}) -> dict."""
    caps = {}
    for c in (item.get("capability_to_review_status") or []):
        if isinstance(c, dict) and c.get("capability"):
            caps[c["capability"]] = c.get("review_status")
    return caps


def _wa(caps: dict):
    return caps.get("WHATSAPP_SHOPPING") or caps.get("MARKETING_MESSAGES") or caps.get("WHATSAPP")


@frappe.whitelist()
def run_diagnostics(store: int = 1) -> dict:
    """Fetch every catalog product's review status + per-channel capability. Returns a
    summary; stores the actionable (not-approved / errored) rows for the Desk report."""
    frappe.only_for("System Manager")
    s = sync._get_settings()
    if not s:
        frappe.throw(frappe._("Meta catalog is not enabled / configured."))
    items = _fetch(s)
    by_review: dict = {}
    by_channel: dict = {}
    actionable = []
    for it in items:
        rs = it.get("review_status") or "unknown"
        by_review[rs] = by_review.get(rs, 0) + 1
        caps = _caps(it)
        for cap, st in caps.items():
            by_channel.setdefault(cap, {})
            by_channel[cap][st or "unknown"] = by_channel[cap].get(st or "unknown", 0) + 1
        if rs.lower() != "approved" or it.get("errors"):
            actionable.append({"retailer_id": it.get("retailer_id"), "review_status": rs,
                               "caps": caps, "errors": it.get("errors")})
    if int(store):
        _store(actionable)
    return {"total": len(items), "by_review": by_review, "by_channel": by_channel,
            "actionable": len(actionable), "sample": actionable[:10]}


def _store(actionable: list) -> None:
    """Snapshot the actionable rows (clear-and-insert) for the Desk report."""
    frappe.db.delete(_DOCTYPE)
    now = now_datetime()
    for a in actionable:
        caps = a["caps"]
        frappe.get_doc({
            "doctype": _DOCTYPE,
            "retailer_id": a["retailer_id"],
            "review_status": a["review_status"],
            "fb_status": caps.get("FB_SHOPS") or caps.get("MARKETPLACE"),
            "ig_status": caps.get("INSTAGRAM_SHOPPING"),
            "wa_status": _wa(caps),
            "errors": (json.dumps(a["errors"])[:1000] if a.get("errors") else ""),
            "checked_at": now,
        }).insert(ignore_permissions=True)


@frappe.whitelist()
def item_diagnostic(retailer_id: str) -> dict:
    """One product's diagnostics by retailer_id (Item Code), from the last snapshot —
    the 'why isn't X sendable' answer. Absent = it was approved / not actionable."""
    frappe.only_for("System Manager")
    row = frappe.db.get_value(
        _DOCTYPE, {"retailer_id": retailer_id},
        ["review_status", "fb_status", "ig_status", "wa_status", "errors", "checked_at"], as_dict=True)
    if not row:
        return {"retailer_id": retailer_id,
                "note": "Not in the last diagnostics snapshot (likely approved). Run run_diagnostics() to refresh."}
    return row
