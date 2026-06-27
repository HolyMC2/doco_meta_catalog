"""MA-4 — Click-to-WhatsApp (CTWA) attribution loop.

When a customer clicks a CTWA ad, the FIRST inbound WhatsApp message carries a
`referral` object with `ctwa_clid`. We store (phone -> ctwa_clid) so that when the
customer later buys, the Purchase CAPI event (capi.py) is emitted with
action_source=business_messaging + the ctwa_clid — attributing the sale back to the ad.

CAPTURE STATUS: frappe_whatsapp (baked-only here) DROPS the inbound `referral`, so the
`ctwa_clid` custom field on WhatsApp Message is not yet populated in production. The loop
is built + tested; capture activates the moment that field is filled (a small
frappe_whatsapp webhook change / upstream PR — see on_inbound_message).
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, now_datetime, today

_SETTINGS = "Meta Catalog Settings"
_DOCTYPE = "Meta CTWA Click"
_ATTRIBUTION_DAYS = 7


def capture(phone: str | None, ctwa_clid: str | None, *,
            source_id=None, source_type=None, headline=None) -> str | None:
    """Upsert a CTWA click (dedup on ctwa_clid). Returns the doc name, or None when
    phone/ctwa_clid is missing."""
    if not (phone and ctwa_clid):
        return None
    name = frappe.db.get_value(_DOCTYPE, {"ctwa_clid": ctwa_clid}, "name")
    if name:
        frappe.db.set_value(_DOCTYPE, name, {"phone": phone, "captured_at": now_datetime()}, update_modified=False)
        return name
    doc = frappe.get_doc({
        "doctype": _DOCTYPE, "phone": phone, "ctwa_clid": ctwa_clid,
        "source_id": source_id, "source_type": source_type, "headline": headline,
        "captured_at": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def recent_click(phone: str | None, days: int = _ATTRIBUTION_DAYS) -> dict | None:
    """Most recent CTWA click for a phone within the attribution window, or None.
    Matches on the last 10 digits so +52 / 52 / 521 variants all resolve."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]
    if len(digits) < 10:
        return None
    rows = frappe.get_all(
        _DOCTYPE,
        filters={"phone": ["like", f"%{digits}"], "captured_at": [">=", add_days(today(), -days)]},
        fields=["ctwa_clid", "source_id", "captured_at", "phone"],
        order_by="captured_at desc",
        limit=20,
    )
    if not rows:
        return None
    # Bail if the last-10 match spans >1 distinct full number (e.g. +1 vs +52 sharing
    # 10 digits) — don't attribute a sale to the wrong buyer's click.
    fulls = {"".join(ch for ch in (r.get("phone") or "") if ch.isdigit()) for r in rows}
    if len(fulls) > 1:
        return None
    return rows[0]


def on_inbound_message(wa_message: str) -> None:
    """Worker (MA-4 capture): stamp a CTWA click from an inbound WhatsApp Message that
    carries a ctwa_clid (custom field). Dormant until frappe_whatsapp populates it."""
    if not frappe.db.get_single_value(_SETTINGS, "deeplink_capture_enabled"):
        return  # CTWA capture rides the same attribution gate as deep-link capture
    row = frappe.db.get_value(
        "WhatsApp Message", wa_message,
        ["from", "ctwa_clid", "ctwa_source_id", "type"], as_dict=True)
    if not row or (row.get("type") or "").lower() == "outgoing" or not row.get("ctwa_clid"):
        return
    frappe.set_user("Administrator")
    capture(row.get("from"), row.get("ctwa_clid"), source_id=row.get("ctwa_source_id"))
