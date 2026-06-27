"""MA-9 — Meta Product Sets (auto-curated catalog collections).

A Product Set is a server-side FILTER over the catalog that Meta surfaces as a
shoppable collection / dynamic-ad audience. Each Meta Product Set config row maps a
dimension (item_group -> product_type, brand, availability, or a manual raw filter)
to a Meta filter; sync_product_sets() creates the set on the catalog (or updates it in
place via its id) and stores the returned id for idempotency. Reuses the catalog token.

product_type is set on each item by sync._build_payloads (= the item_group), so the
item_group dimension actually has something to match.
"""
from __future__ import annotations

import json

import frappe
import requests
from frappe.utils import now_datetime

from doco_meta_catalog import sync

_DOCTYPE = "Meta Product Set"


def build_filter(dimension: str, value: str) -> dict:
    """Meta product_set filter JSON for a dimension + value."""
    v = (value or "").strip()
    if dimension == "item_group":
        return {"product_type": {"i_contains": v}}
    if dimension == "brand":
        return {"brand": {"i_contains": v}}
    if dimension == "availability":
        return {"availability": {"eq": v or "in stock"}}
    if dimension == "manual":
        return json.loads(v) if v else {}
    frappe.throw(frappe._("Unknown product set dimension: {0}").format(dimension))


def _sync_one(row, settings) -> str | None:
    """Create or update one Meta Product Set. POST to the catalog creates; POST to the
    set id updates in place. Returns the set id."""
    filt = build_filter(row.dimension, row.value)
    root = settings.get_graph_root()
    url = f"{root}/{row.meta_set_id}" if row.get("meta_set_id") else f"{root}/{settings.catalog_id}/product_sets"
    r = requests.post(
        url,
        params={"access_token": settings.get_token()},
        data={"name": row.set_name, "filter": json.dumps(filt)},
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error", {})
            meta_err = f"{err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except Exception:
            meta_err = f"HTTP {r.status_code}"
        frappe.log_error(title=f"Product set sync {r.status_code}", message=f"{row.set_name}: {meta_err}")
        r.raise_for_status()
    set_id = (r.json() or {}).get("id") or row.get("meta_set_id")
    frappe.db.set_value(_DOCTYPE, row.name,
                        {"meta_set_id": set_id, "last_synced": now_datetime(), "last_error": ""},
                        update_modified=False)
    return set_id


@frappe.whitelist()
def sync_product_sets():
    """Create/update every enabled Meta Product Set on the catalog. Gated by catalog enabled."""
    frappe.only_for("System Manager")
    s = sync._get_settings()
    if not s:
        frappe.throw(frappe._("Meta catalog is not enabled / configured."))
    rows = frappe.get_all(_DOCTYPE, filters={"enabled": 1},
                          fields=["name", "set_name", "dimension", "value", "meta_set_id"])
    out = []
    for r in rows:
        row = frappe._dict(r)
        try:
            out.append({"set": row.set_name, "id": _sync_one(row, s)})
        except Exception as e:
            frappe.db.set_value(_DOCTYPE, row.name, "last_error", str(e)[:300], update_modified=False)
            out.append({"set": row.set_name, "error": str(e)[:140]})
    return out


@frappe.whitelist()
def autoseed(by_brand: int = 1, by_group: int = 0, limit: int = 25):
    """Convenience: create config rows (no Meta push) from the catalog's distinct brands
    and/or item_groups, plus a standard 'Disponibles' availability set. Run
    sync_product_sets() afterward. Idempotent on set_name."""
    frappe.only_for("System Manager")
    s = sync._get_settings()
    if not s:
        frappe.throw(frappe._("Meta catalog is not enabled / configured."))
    created = []
    _ensure_row("Disponibles", "availability", "in stock", created)
    leaves = sync._eligible_leaves(None)
    if int(by_brand):
        brands = sorted({(it.get("brand") or "").strip() for it in leaves if it.get("brand")})[: int(limit)]
        for b in brands:
            _ensure_row(b, "brand", b, created)
    if int(by_group):
        groups = sorted({(it.get("item_group") or "").strip() for it in leaves if it.get("item_group")})[: int(limit)]
        for g in groups:
            _ensure_row(g, "item_group", g, created)
    return {"created": created}


def _ensure_row(name: str, dimension: str, value: str, created: list) -> None:
    if frappe.db.exists(_DOCTYPE, name):
        return
    frappe.get_doc({"doctype": _DOCTYPE, "set_name": name, "dimension": dimension,
                    "value": value, "enabled": 1}).insert(ignore_permissions=True)
    created.append(name)
