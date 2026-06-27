"""MA-6 — ref-tracked wa.me / m.me deep links + inbound attribution.

GENERATE: build_wa_link / build_mme_link produce share links carrying a campaign
``ref``. On WhatsApp the ref rides in the PREFILLED TEXT (so it arrives as a normal
inbound message we can parse — frappe_whatsapp drops the Cloud API ``referral``
object, and we deliberately don't front its webhook), on Messenger in the ``ref``
query param (surfaced to our Messenger webhook).

CAPTURE: parse_ref() pulls the token off the first inbound message; record_attribution()
logs a CRM Touchpoint so a conversation is attributable to the campaign that drove it.
Wired from inbound.py (WhatsApp text). Gated by Meta Catalog Settings.deeplink_capture_enabled.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import frappe
from frappe import _

_SETTINGS = "Meta Catalog Settings"
# Marker appended to the wa.me prefill — tight charset so parsing is unambiguous and
# a customer can't accidentally inject one.
_REF_RE = re.compile(r"\[ref:([A-Za-z0-9_.\-]{1,40})\]")
_REF_OK = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


def _clean_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not _REF_OK.match(ref):
        frappe.throw(_("ref inválido: 1–40 caracteres [A-Za-z0-9_.-]"))
    return ref


@frappe.whitelist()
def build_wa_link(ref: str, text: str | None = None, number: str | None = None) -> str:
    """wa.me share link whose prefilled text carries ``[ref:CODE]`` for attribution.
    ``number`` (business WhatsApp, E.164 digits) falls back to the settings field."""
    frappe.only_for(["System Manager", "Sales User"])
    ref = _clean_ref(ref)
    number = re.sub(r"\D", "", number or frappe.db.get_single_value(_SETTINGS, "business_whatsapp_number") or "")
    if not number:
        frappe.throw(_("Falta el número de WhatsApp (parámetro `number` o Meta Catalog Settings.business_whatsapp_number)."))
    prefill = f"{text or 'Hola, me interesa 👋'} [ref:{ref}]"
    return f"https://wa.me/{number}?text={quote(prefill)}"


@frappe.whitelist()
def build_mme_link(ref: str, page: str | None = None) -> str:
    """m.me share link with a tracked ``ref`` param (surfaced to the Messenger webhook).
    ``page`` falls back to Messenger Settings.page_id."""
    frappe.only_for(["System Manager", "Sales User"])
    ref = _clean_ref(ref)
    page = page or frappe.db.get_value("Messenger Settings", "Messenger Settings", "page_id")
    if not page:
        frappe.throw(_("Falta el page id de Messenger (parámetro `page` o Messenger Settings.page_id)."))
    return f"https://m.me/{page}?ref={ref}"


def parse_ref(text: str | None) -> str | None:
    """Extract a ``[ref:CODE]`` token from an inbound message body, or None."""
    if not text:
        return None
    m = _REF_RE.search(text)
    return m.group(1) if m else None


def record_attribution(*, ref: str, channel: str, phone: str | None = None, message_id: str | None = None) -> None:
    """Log a CRM Touchpoint for a deep-link click→conversation. Best-effort: an
    attribution failure must never disrupt message handling. utm_campaign = the ref."""
    try:
        from doco_marketing.services import touchpoint
        touchpoint.record_touchpoint(
            contact=_contact_for_phone(phone),
            channel=channel,
            event="deeplink_click",
            utm={"utm_campaign": ref, "utm_source": channel, "utm_medium": "deeplink"},
            props={"ref": ref, "phone": phone, "message_id": message_id},
        )
    except Exception:
        frappe.log_error(title="MA-6 deeplink attribution failed", message=frappe.get_traceback())


def _contact_for_phone(phone: str | None) -> str | None:
    """Best-effort Contact match by the last 10 digits, or None when absent OR ambiguous
    (>1 contact shares the last 10 — don't attribute to the wrong party)."""
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) < 10:
        return None
    contacts = frappe.get_all("Contact", filters={"mobile_no": ["like", f"%{digits}"]},
                              fields=["name"], limit=2)
    return contacts[0].name if len(contacts) == 1 else None
