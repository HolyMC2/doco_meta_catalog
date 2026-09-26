"""Observed catalog/phone readiness, separate from configured local capability."""

import frappe
from frappe.utils import get_datetime, now_datetime

from doco_meta_catalog import publication


def classify(catalog_id, catalogs, phone_id, phone, commerce):
    """Empty provider lists mean unconfirmed; UI confirmation is not invented."""
    if not isinstance(catalogs, dict) or not isinstance(catalogs.get("data"), list):
        return {"state": "Unknown", "reason_code": "catalog_connection_unavailable"}
    linked = any(
        isinstance(row, dict) and str(row.get("id", "")) == catalog_id
        for row in catalogs["data"]
    )
    if not linked:
        return {"state": "Unknown", "reason_code": "catalog_link_not_returned"}
    if not isinstance(phone, dict) or str(phone.get("id", "")) != phone_id:
        return {"state": "Unknown", "reason_code": "catalog_phone_unconfirmed"}
    if phone.get("status") != "CONNECTED":
        return {"state": "Unavailable", "reason_code": "catalog_phone_disconnected"}
    rows = commerce.get("data") if isinstance(commerce, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return {"state": "Unknown", "reason_code": "catalog_commerce_unconfirmed"}
    if rows[0].get("is_catalog_visible") is not True:
        return {"state": "Unavailable", "reason_code": "catalog_visibility_disabled"}
    return {
        "state": "Observed",
        "reason_code": "",
        "cart_enabled": rows[0].get("is_cart_enabled") is True,
    }


def observe(settings):
    """Bounded, fixed-origin GETs only. Never reconnect or mutate provider assets."""
    try:
        account = frappe.get_doc("WhatsApp Account", settings.whatsapp_account)
        result = {}
        for edge, params in (
            ("whatsapp_catalogs", {"fields": "id", "limit": 100}),
            ("whatsapp_phone", {"fields": "id,status"}),
            ("whatsapp_commerce", None),
        ):
            status, body = publication.graph_request(
                "GET", settings, edge, params=params
            )
            if status != 200:
                return {
                    "state": "Unknown",
                    "reason_code": "catalog_connection_unavailable",
                }
            result[edge] = body
        return classify(
            str(settings.catalog_id),
            result["whatsapp_catalogs"],
            str(account.phone_id),
            result["whatsapp_phone"],
            result["whatsapp_commerce"],
        )
    except (
        ValueError,
        TypeError,
        frappe.DoesNotExistError,
        publication.requests.RequestException,
    ):
        return {"state": "Unknown", "reason_code": "catalog_connection_unavailable"}


def latest(settings):
    """A cached observation is explicitly dated; it never certifies delivery."""
    identity = publication.scope(settings)
    row = frappe.db.get_value(
        publication.RUN,
        {"scope_revision": identity["scope_revision"]},
        ["name", "checked_at", "summary"],
        as_dict=True,
        order_by="creation desc,name desc",
    )
    unknown = {"state": "Unknown", "reason_code": "catalog_connection_unchecked"}
    if not row:
        return unknown
    try:
        summary = frappe.parse_json(row.summary)
        data = summary.get("connection") if isinstance(summary, dict) else None
        if not isinstance(data, dict) or data.get("state") not in {
            "Observed",
            "Unknown",
            "Unavailable",
        }:
            return unknown
        age = (now_datetime() - get_datetime(row.checked_at)).total_seconds()
        if age < 0 or age > 86400:
            return {
                "state": "Stale",
                "reason_code": "catalog_connection_stale",
                "checked_at": str(row.checked_at),
            }
        return {
            key: data[key]
            for key in ("state", "reason_code", "cart_enabled")
            if key in data
        } | {"checked_at": str(row.checked_at)}
    except (ValueError, TypeError, AttributeError):
        return unknown
