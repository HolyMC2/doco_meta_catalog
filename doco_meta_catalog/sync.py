"""Bridge ERPNext Item -> Meta Commerce Catalog via Graph API items_batch endpoint.

Public hooks (wired from hooks.py):
    queue_item_sync(doc, method)     called from Item.on_update
    queue_item_delete(doc, method)   called from Item.on_trash
    full_reconcile()                 scheduled daily

Internal:
    _build_item_payload(item, settings) -> dict matching Meta items_batch schema
    _post_items_batch(settings, requests_payload) -> dict (Meta response)
"""

from __future__ import annotations

import json

import frappe
import requests
from frappe.utils import flt, get_url


SETTINGS_DOCTYPE = "Meta Catalog Settings"


def _get_settings():
    s = frappe.get_cached_doc(SETTINGS_DOCTYPE)
    if not s.enabled or not s.catalog_id:
        return None
    return s


def _should_sync(item) -> bool:
    if item.get("disabled"):
        return False
    s = _get_settings()
    if not s:
        return False
    if s.sync_only_website_items:
        # ERPNext: Website Item is a child doctype keyed by item_code OR Item.show_in_website is the legacy flag
        if not (item.get("show_in_website") or frappe.db.exists("Website Item", {"item_code": item.name})):
            return False
    return True


def _public_image_url(item, settings) -> str | None:
    img = item.get("image") or frappe.db.get_value("Website Item", {"item_code": item.name}, "website_image")
    if not img:
        return settings.fallback_image_url or None
    if img.startswith("http"):
        return img
    base = (settings.image_url_base or get_url()).rstrip("/")
    return base + (img if img.startswith("/") else "/" + img)


def _price_minor_units(amount, currency) -> int:
    # Meta items_batch expects minor units (e.g. 12999 = 129.99 MXN).
    # MXN is 2-decimal; treat all here as 2 decimals — safe for MXN/USD/EUR.
    return int(round(flt(amount) * 100))


def _build_item_payload(item, settings) -> dict:
    """Return one item dict for the items_batch `requests` array."""
    price_amt = flt(item.get("standard_rate")) * (1 + flt(settings.price_markup_percent) / 100.0)
    return {
        "method": "UPDATE",
        "data": {
            "id": item.name,  # retailer_id = ERPNext item_code
            "title": item.get("item_name") or item.name,
            "description": (item.get("description") or item.get("item_name") or item.name)[:9999],
            "availability": settings.default_availability or "in stock",
            "condition": settings.default_condition or "new",
            "price": _price_minor_units(price_amt, settings.default_currency),
            "currency": settings.default_currency or "MXN",
            "image_link": _public_image_url(item, settings) or "",
            "brand": item.get("brand") or settings.default_brand or "",
            "url": f"{(settings.image_url_base or get_url()).rstrip('/')}/shop/{item.name}",
        },
    }


def _post_items_batch(settings, requests_payload):
    url = f"{settings.get_graph_root()}/{settings.catalog_id}/items_batch"
    token = settings.get_token()
    if not token:
        frappe.throw("Meta Catalog: no access token configured")
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"requests": requests_payload},
        timeout=30,
    )
    if r.status_code >= 400:
        frappe.log_error(
            title=f"Meta Catalog push HTTP {r.status_code}",
            message=f"URL: {url}\nPayload: {json.dumps(requests_payload)[:2000]}\nResponse: {r.text[:2000]}",
        )
        r.raise_for_status()
    return r.json()


# ---------------- hooks ----------------


def queue_item_sync(doc, method=None):
    """Item.on_update — enqueue a background push so save is not blocked by Meta latency."""
    if not _should_sync(doc):
        return
    frappe.enqueue(
        "doco_meta_catalog.sync.push_one",
        item_name=doc.name,
        queue="short",
        job_id=f"meta_catalog_push::{doc.name}",
        deduplicate=True,
    )


def queue_item_delete(doc, method=None):
    """Item.on_trash — enqueue a DELETE for this retailer_id."""
    s = _get_settings()
    if not s:
        return
    frappe.enqueue(
        "doco_meta_catalog.sync.delete_one",
        item_name=doc.name,
        queue="short",
        job_id=f"meta_catalog_delete::{doc.name}",
        deduplicate=True,
    )


# ---------------- workers ----------------


def push_one(item_name: str):
    s = _get_settings()
    if not s:
        return
    item = frappe.get_doc("Item", item_name).as_dict()
    if not _should_sync(item):
        return
    payload = [_build_item_payload(item, s)]
    _post_items_batch(s, payload)


def delete_one(item_name: str):
    s = _get_settings()
    if not s:
        return
    _post_items_batch(s, [{"method": "DELETE", "data": {"id": item_name}}])


def full_reconcile():
    """Nightly: re-push every eligible Item in chunks of 1000 (Meta batch limit is ~5000;
    chunking smaller keeps payload size sane)."""
    s = _get_settings()
    if not s:
        return
    filters = {"disabled": 0}
    fields = [
        "name", "item_name", "description", "standard_rate", "image", "brand", "show_in_website", "disabled",
    ]
    items = frappe.get_all("Item", filters=filters, fields=fields, limit_page_length=0)
    sent = 0
    for chunk_start in range(0, len(items), 1000):
        chunk = items[chunk_start : chunk_start + 1000]
        batch = []
        for it in chunk:
            if not _should_sync(it):
                continue
            batch.append(_build_item_payload(it, s))
        if not batch:
            continue
        try:
            _post_items_batch(s, batch)
            sent += len(batch)
        except Exception as e:
            frappe.db.set_value(
                SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "last_error", str(e)[:500], update_modified=False
            )
            raise
    frappe.db.set_value(
        SETTINGS_DOCTYPE,
        SETTINGS_DOCTYPE,
        {
            "last_full_reconcile": frappe.utils.now(),
            "last_full_reconcile_status": f"OK ({sent} items)",
            "last_error": "",
        },
        update_modified=False,
    )


# ---------------- whitelisted admin actions ----------------


@frappe.whitelist()
def sync_all_now():
    """Manual trigger from Desk: enqueue a full reconcile."""
    frappe.only_for("System Manager")
    frappe.enqueue("doco_meta_catalog.sync.full_reconcile", queue="long", job_id="meta_catalog_full_reconcile")
    return {"queued": True}


@frappe.whitelist()
def sync_item_now(item_code: str):
    frappe.only_for("System Manager")
    push_one(item_code)
    return {"ok": True}
