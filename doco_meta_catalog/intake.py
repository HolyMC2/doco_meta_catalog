"""MA-11 — endpoint-free WhatsApp Flow repair-intake -> fcrm CRM Lead.

frappe_whatsapp persists a completed Flow as a WhatsApp Message (content_type='flow')
with the full responses in `flow_response`. We pick it off async and create a CRM Lead
from the responses, so a repair intake collected inside WhatsApp lands in the fcrm inbox
for a human to triage/convert. Endpoint-free = no data-exchange server; the flow JSON is
static and the data arrives via the webhook.

The flow is authored + published via frappe_whatsapp's WhatsApp Flow doctype (Marco). A
sample repair-intake flow collects: nombre, telefono, modelo, falla. Field names are
matched leniently (Spanish/English synonyms); unmapped answers are kept in a timeline
Comment. Gated by Meta Catalog Settings.intake_enabled (+ optional intake_flow_id).
"""
from __future__ import annotations

import json

import frappe

_SETTINGS = "Meta Catalog Settings"
_NAME_KEYS = ("nombre", "name", "first_name", "nombre_completo", "cliente")
_PHONE_KEYS = ("telefono", "teléfono", "phone", "mobile", "celular", "whatsapp")


def _pick(resp: dict, keys) -> str | None:
    for k in keys:
        for rk, rv in resp.items():
            if rk.lower() == k and rv:
                return str(rv)
    return None


def _default_lead_status() -> str | None:
    return frappe.db.get_value("CRM Lead Status", {"position": 1}, "name") \
        or frappe.db.get_value("CRM Lead Status", {}, "name")


def process_flow(wa_message: str):
    """Worker: a WhatsApp Flow completion -> CRM Lead. Idempotent per message_id."""
    row = frappe.db.get_value(
        "WhatsApp Message", wa_message,
        ["from", "flow", "flow_response", "content_type", "type", "message_id", "profile_name"],
        as_dict=True)
    if not row or (row.content_type or "") != "flow" or (row.type or "").lower() == "outgoing":
        return
    if not frappe.db.get_single_value(_SETTINGS, "intake_enabled"):
        return
    if not frappe.db.exists("DocType", "CRM Lead"):
        return
    want_flow = (frappe.db.get_single_value(_SETTINGS, "intake_flow_id") or "").strip()
    if want_flow and (row.get("flow") or "") != want_flow:
        return  # a different flow — not our intake
    marker = f"wa-flow-intake:{row.get('message_id')}"
    if frappe.db.exists("Comment", {"reference_doctype": "CRM Lead", "content": marker}):
        return  # already created
    try:
        resp = json.loads(row.get("flow_response") or "{}")
    except Exception:
        frappe.log_error(title="MA-11 flow_response parse failed", message=f"wa_message={wa_message}")
        raise
    if not isinstance(resp, dict):
        resp = {}

    frappe.set_user("Administrator")
    full_name = _pick(resp, _NAME_KEYS) or row.get("profile_name") or "WhatsApp"
    parts = full_name.split(" ", 1)
    phone = _pick(resp, _PHONE_KEYS) or row.get("from")
    lead = frappe.get_doc({
        "doctype": "CRM Lead",
        "first_name": parts[0][:140],
        "last_name": (parts[1][:140] if len(parts) > 1 else None),
        "mobile_no": phone,
        "status": _default_lead_status(),
    })
    lead.flags.ignore_permissions = True
    lead.insert(ignore_permissions=True)
    # Keep the full intake (device, issue, anything else) on the lead timeline + the dedup marker.
    summary = "\n".join(f"{k}: {v}" for k, v in resp.items() if v) or "Flow completed"
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Comment",
        "reference_doctype": "CRM Lead", "reference_name": lead.name,
        "content": f"Intake WhatsApp Flow:\n{summary}",
    }).insert(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "CRM Lead", "reference_name": lead.name, "content": marker,
    }).insert(ignore_permissions=True)
    return lead.name
