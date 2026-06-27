"""MA-3 — Meta Conversions API (CAPI) emitter.

Server-side conversion events to a Meta DATASET (Pixel): Purchase from a submitted
Sales Invoice, Lead from a new CRM Lead. PII is SHA256-hashed per Meta's normalization
rules; a SEPARATE dataset-scoped token (not the catalog/WhatsApp token) is used. Events
are deduped by event_id = the source doc name, so a server event can safely co-exist
with a browser Pixel firing the same conversion.

Emission is async (enqueue_after_commit, off the submit path) and guarded — a CAPI
outage must never affect invoicing. Gated by Meta Catalog Settings.capi_enabled.

MA-4 upgrades the Purchase action_source to business_messaging when the buyer has a
captured CTWA click (see ctwa.py).
"""
from __future__ import annotations

import hashlib
import json
import re

import frappe
import requests
from frappe.utils import flt

_SETTINGS = "Meta Catalog Settings"


def _enabled() -> bool:
    return bool(frappe.db.get_single_value(_SETTINGS, "capi_enabled"))


# --- user_data normalization + hashing (Meta spec) -------------------------

def _hash(v) -> str | None:
    """SHA256 of a lowercased, trimmed value (email / name)."""
    v = (str(v) if v is not None else "").strip().lower()
    return hashlib.sha256(v.encode()).hexdigest() if v else None


def _hash_phone(v) -> str | None:
    """SHA256 of digits-only phone (country code included, no +/spaces)."""
    d = re.sub(r"\D", "", str(v or ""))
    return hashlib.sha256(d.encode()).hexdigest() if d else None


def _user_data(*, email=None, phone=None, first=None, last=None, ctwa_clid=None) -> dict:
    ud: dict = {}
    for key, val in (("em", _hash(email)), ("ph", _hash_phone(phone)),
                     ("fn", _hash(first)), ("ln", _hash(last))):
        if val:
            ud[key] = [val]
    if ctwa_clid:
        ud["ctwa_clid"] = ctwa_clid  # NOT hashed — Meta matches the raw click id
    return ud


# --- emit + transport ------------------------------------------------------

def emit(event_name: str, event_id: str, *, user_data: dict, custom_data: dict | None = None,
         action_source: str = "website", event_time=None, messaging_channel: str | None = None) -> None:
    """Enqueue ONE CAPI event (after_commit, guarded). No-op unless capi_enabled and
    the event has at least one user_data match key."""
    if not _enabled() or not user_data:
        return
    event = {
        "event_name": event_name,
        "event_time": int(event_time or frappe.utils.now_datetime().timestamp()),
        "event_id": event_id,
        "action_source": action_source,
        "user_data": user_data,
    }
    if custom_data:
        event["custom_data"] = custom_data
    if messaging_channel:
        event["messaging_channel"] = messaging_channel
    try:
        frappe.enqueue(
            "doco_meta_catalog.capi._post",
            queue="short",
            job_id=f"capi::{event_name}::{event_id}",
            deduplicate=True,
            enqueue_after_commit=True,
            event=event,
        )
    except Exception:
        frappe.log_error(title="CAPI enqueue failed", message=frappe.get_traceback())


def _post(event: dict):
    """Worker: POST one event to the dataset /events endpoint."""
    s = frappe.get_cached_doc(_SETTINGS)
    dataset = (s.get("capi_dataset_id") or "").strip()
    token = s.get_capi_token()
    if not (dataset and token):
        frappe.log_error(title="CAPI not configured", message="missing capi_dataset_id or token")
        return
    body = {"data": [event], "access_token": token}
    tec = (s.get("capi_test_event_code") or "").strip()
    if tec:
        body["test_event_code"] = tec
    r = requests.post(f"{s.get_graph_root()}/{dataset}/events", json=body, timeout=30)
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error", {})
            meta_err = f"{err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except Exception:
            meta_err = f"HTTP {r.status_code}"
        safe = {k: v for k, v in event.items() if k != "user_data"}  # drop hashed PII from logs
        frappe.log_error(title=f"CAPI POST {r.status_code}", message=f"{meta_err}\nevent={json.dumps(safe)[:800]}")
        r.raise_for_status()
    return r.json()


# --- contact resolution ----------------------------------------------------

def _customer_contact(customer: str | None) -> dict:
    """Best-effort email/phone for a Customer via its primary contact."""
    if not customer:
        return {}
    contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
    if not contact:
        return {}
    row = frappe.db.get_value("Contact", contact, ["email_id", "mobile_no", "first_name", "last_name"], as_dict=True)
    return row or {}


# --- triggers (wired in hooks.py) ------------------------------------------

def on_sales_invoice_submit(doc, method=None):
    """Sales Invoice on_submit -> CAPI Purchase. action_source from settings."""
    if not _enabled():
        return
    c = _customer_contact(doc.get("customer"))
    email = doc.get("contact_email") or c.get("email_id")
    phone = doc.get("contact_mobile") or c.get("mobile_no")
    # MA-4: attribute the Purchase to the click-to-WhatsApp ad when a CTWA click is on
    # file for this buyer (within the attribution window) — action_source flips to
    # business_messaging and the raw ctwa_clid rides in user_data.
    click = None
    try:
        from doco_meta_catalog import ctwa
        click = ctwa.recent_click(phone)
    except Exception:
        click = None
    user_data = _user_data(email=email, phone=phone, ctwa_clid=(click or {}).get("ctwa_clid"))
    custom = {
        "currency": doc.get("currency") or "MXN",
        "value": flt(doc.get("grand_total")),
        "content_type": "product",
        "content_ids": list({i.item_code for i in (doc.get("items") or []) if i.item_code}),
        "order_id": doc.name,
    }
    if click:
        action_source, messaging_channel = "business_messaging", "whatsapp"
    else:
        action_source = (frappe.db.get_single_value(_SETTINGS, "capi_purchase_source") or "website")
        messaging_channel = None
    emit("Purchase", doc.name, user_data=user_data, custom_data=custom,
         action_source=action_source, messaging_channel=messaging_channel)


def on_crm_lead_insert(doc, method=None):
    """CRM Lead after_insert -> CAPI Lead."""
    if not _enabled():
        return
    user_data = _user_data(email=doc.get("email"), phone=doc.get("mobile_no"),
                           first=doc.get("first_name"), last=doc.get("last_name"))
    emit("Lead", doc.name, user_data=user_data, action_source="website")


@frappe.whitelist()
def send_test_event():
    """Fire a Test event (set capi_test_event_code first, watch Events Manager > Test Events)."""
    frappe.only_for("System Manager")
    emit("Lead", f"test-{frappe.generate_hash(length=8)}",
         user_data=_user_data(email="test@example.com", phone="5215555550000"),
         action_source="website")
    return {"ok": True}
