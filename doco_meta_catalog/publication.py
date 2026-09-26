"""Durable catalog batches. Only this worker crosses the provider write boundary."""

import json
import re
from datetime import timedelta
from uuid import uuid4

import frappe
import requests
from frappe.utils import cint, get_datetime, now_datetime

from doco_meta_catalog import publication_contract as contract
from doco_meta_catalog.utils import assert_outbound_allowed

DOCTYPE = "Meta Catalog Publication"
RUN = "Meta Catalog Diagnostic Run"
RESOLUTION = "Meta Catalog Publication Resolution"
_TOKEN = object()


def mark(doc):
    doc.flags.catalog_publication_service = _TOKEN
    return doc


def validate_evidence(doc):
    if doc.flags.get("catalog_publication_service") is not _TOKEN:
        frappe.throw("Use the catalog publication service.", frappe.PermissionError)
    previous = doc.get_doc_before_save()
    immutable = ("catalog_id", "account_name", "scope_revision", "scope_json")
    if doc.doctype == DOCTYPE:
        immutable += ("request_id", "request_hash", "request_body", "reconciles")
        if contract.digest(json.loads(doc.request_body)) != doc.request_hash:
            frappe.throw("Publication request was modified.")
    if previous and any(previous.get(key) != doc.get(key) for key in immutable):
        frappe.throw("Publication evidence identity is immutable.")


def scope(settings, *, lock=False):
    catalog = str(settings.catalog_id or "")
    if not re.fullmatch(r"[0-9]{1,40}", catalog):
        raise ValueError("catalog_id_invalid")
    version = settings.graph_api_version or "v21.0"
    contract.graph_root(version)
    account_name = settings.whatsapp_account or ""
    account = None
    if account_name:
        account = frappe.db.get_value(
            "WhatsApp Account",
            account_name,
            ["name", "phone_id", "app_id", "business_id"],
            as_dict=True,
            for_update=lock,
        )
        if not account:
            raise ValueError("catalog_account_unavailable")
    values = {
        "catalog_id": catalog,
        "account_name": account_name,
        "account": dict(account)
        if account
        else {"source": "Meta Catalog Settings.access_token"},
        "graph_version": version,
    }
    return {
        "catalog_id": catalog,
        "account_name": account_name,
        "scope_revision": contract.digest(values),
        "scope_json": contract.canonical(values),
    }


def current_settings(doc):
    # Build the document from locking VALUES, not a later ordinary RR read.
    from doco_meta_catalog.commerce import _settings

    values = _settings()
    settings = frappe.get_doc(
        {"doctype": "Meta Catalog Settings", "name": "Meta Catalog Settings", **values}
    )
    if not cint(settings.enabled):
        raise ValueError("catalog_disabled")
    if scope(settings, lock=True)["scope_revision"] != doc.scope_revision:
        raise ValueError("catalog_binding_changed")
    return settings


def graph_request(method, settings, edge, *, params=None, body=None):
    """Fixed Graph origin, no redirects, token outside URLs, bounded response."""
    if edge not in {"items_batch", "check_batch_request_status", "products"}:
        raise ValueError("graph_edge_invalid")
    identity = scope(settings)
    version = json.loads(identity["scope_json"])["graph_version"]
    url = f"{contract.graph_root(version)}/{identity['catalog_id']}/{edge}"
    token = settings.get_token()
    if not token:
        raise ValueError("catalog_token_unavailable")
    response = requests.request(
        method,
        url,
        params=params,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        timeout=(10, 45),
        allow_redirects=False,
        stream=True,
    )
    try:
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > contract.MAX_RESPONSE_BYTES:
                raise ValueError("graph_response_too_large")
            chunks.append(chunk)
        content = b"".join(chunks).decode("utf-8")
        # Provider messages can echo caller credentials. Retain no token in evidence.
        content = content.replace(token, "[redacted]")
        return response.status_code, json.loads(content)
    finally:
        response.close()


def queue(settings, rows, request_id=None, *, reconciles=None):
    assert_outbound_allowed()
    identity = scope(settings)
    body = contract.request_body(rows)
    request_id = request_id or uuid4().hex
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 140:
        raise ValueError("publication_request_id_invalid")
    name = contract.digest([identity["scope_revision"], request_id])
    old = frappe.db.get_value(
        DOCTYPE, name, ["name", "request_hash", "state"], as_dict=True
    )
    fingerprint = contract.digest(json.loads(body))
    if old:
        if old.request_hash != fingerprint:
            raise ValueError("publication_request_reused")
        return {"publication": old.name, "state": old.state}
    doc = mark(
        frappe.get_doc(
            {
                "doctype": DOCTYPE,
                "name": name,
                **identity,
                "request_id": request_id,
                "request_hash": fingerprint,
                "request_body": body,
                "state": "Queued",
                "reconciles": reconciles,
                "handles": "[]",
                "evidence": "[]",
                "items": [
                    {"retailer_id": row["data"]["id"], "operation": row["method"]}
                    for row in rows
                ],
            }
        )
    ).insert(ignore_permissions=True, set_name=name)
    frappe.enqueue(
        "doco_meta_catalog.publication.submit",
        name=doc.name,
        queue="short",
        enqueue_after_commit=True,
        job_id="catalog-publication-" + doc.name,
        deduplicate=True,
    )
    return {"publication": doc.name, "state": doc.state}


def _save(doc, state, reason, **values):
    doc.update({"state": state, "reason_code": reason, **values})
    mark(doc).save(ignore_permissions=True)


def _worker_only():
    if getattr(frappe.local, "request", None):
        frappe.throw("Catalog publication requires a worker.", frappe.PermissionError)


def _prior_publication(doc):
    codes = [row.retailer_id for row in doc.items]
    rows = frappe.db.sql(
        """
        SELECT DISTINCT p.name,p.state FROM `tabMeta Catalog Publication` p
        JOIN `tabMeta Catalog Publication Item` i ON i.parent=p.name
        WHERE p.scope_revision=%(scope)s AND p.name!=%(name)s AND i.retailer_id IN %(codes)s
          AND (p.state='Unknown' OR p.state IN ('Submitting','Pending')
               OR (p.state='Rejected' AND p.poll_complete=0)
               OR (p.state='Queued' AND (p.creation<%(created)s OR (p.creation=%(created)s AND p.name<%(name)s))))
        ORDER BY p.state DESC,p.name LIMIT 100 FOR UPDATE
    """,
        {
            "scope": doc.scope_revision,
            "name": doc.name,
            "codes": codes,
            "created": doc.creation,
        },
        as_dict=True,
    )
    if doc.reconciles and frappe.db.exists(
        RESOLUTION,
        {
            "prior_publication": doc.reconciles,
            "next_publication": doc.name,
            "scope_revision": doc.scope_revision,
        },
    ):
        rows = [row for row in rows if row.name != doc.reconciles]
    # An explicit manager resolution authorizes continuation while its new
    # canonical request is queued/pending or has finished processing. The old
    # Unknown evidence is never rewritten. A failed resolution closes no fence.
    resolved = {
        row[0]
        for row in frappe.db.sql(
            """
        SELECT r.prior_publication FROM `tabMeta Catalog Publication Resolution` r
        JOIN `tabMeta Catalog Publication` n ON n.name=r.next_publication
        WHERE r.scope_revision=%s AND n.state IN ('Queued','Submitting','Pending','Processed')
    """,
            doc.scope_revision,
        )
    }
    rows = [row for row in rows if row.state != "Unknown" or row.name not in resolved]
    if any(row.state == "Unknown" for row in rows):
        return "prior_publication_unknown"
    return "prior_publication_pending" if rows else None


@frappe.whitelist(methods=["POST"])
def check_status(name):
    """An explicit bounded GET follow-up can resolve known batch handles."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("read")
    if not json.loads(doc.handles or "[]"):
        frappe.throw("No provider batch handle is available for a status check.")
    current_settings(doc)
    frappe.enqueue(
        "doco_meta_catalog.publication.poll",
        name=doc.name,
        queue="short",
        enqueue_after_commit=True,
        job_id="catalog-poll-" + doc.name,
        deduplicate=True,
    )
    return {"publication": doc.name, "status_check_queued": True}


@frappe.whitelist(methods=["POST"])
def reconcile_unknown(name, request_id, note):
    """Deliberate manager reconciliation; preserve the old effect as unconfirmed."""
    frappe.only_for("System Manager")
    if (
        not isinstance(request_id, str)
        or not 0 < len(request_id) <= 140
        or not isinstance(note, str)
        or not 1 <= len(note.strip()) <= 500
    ):
        frappe.throw(
            "Provide a request ID and a review note of at most 500 characters."
        )
    previous = frappe.get_doc(DOCTYPE, name, for_update=True)
    previous.check_permission("read")
    resolution_name = contract.digest([name, frappe.session.user, request_id])
    existing = frappe.db.get_value(
        RESOLUTION, resolution_name, ["note", "next_publication"], as_dict=True
    )
    if existing:
        if existing.note != note.strip():
            frappe.throw("Reconciliation request ID was used with a different note.")
        return {
            "publication": existing.next_publication,
            "resolution": resolution_name,
            "replayed": True,
        }
    if previous.state != "Unknown":
        frappe.throw("Only an Unknown publication requires this reconciliation.")
    settings = current_settings(previous)
    codes = [row.retailer_id for row in previous.items]
    for code in codes:
        if frappe.db.exists("Item", code):
            frappe.get_doc("Item", code, for_update=True).check_permission("read")
    from doco_meta_catalog.sync import _build_payloads

    desired, _ = _build_payloads(codes, settings, lock=True)
    present = {row["data"]["id"] for row in desired}
    desired += [
        {"method": "DELETE", "data": {"id": code}}
        for code in codes
        if code not in present
    ]
    result = queue(
        settings, desired, request_id=resolution_name, reconciles=previous.name
    )
    mark(
        frappe.get_doc(
            {
                "doctype": RESOLUTION,
                "name": resolution_name,
                **scope(settings),
                "prior_publication": previous.name,
                "next_publication": result["publication"],
                "actor_user": frappe.session.user,
                "request_id": request_id,
                "note": note.strip(),
            }
        )
    ).insert(ignore_permissions=True, set_name=resolution_name)
    return {**result, "resolution": resolution_name, "prior_effect": "Unconfirmed"}


def submit(name):
    _worker_only()
    doc = frappe.get_doc(DOCTYPE, name, for_update=True)
    if doc.state == "Submitting":
        if doc.lease_until and get_datetime(doc.lease_until) <= now_datetime():
            _save(doc, "Unknown", "submission_interrupted")
            frappe.db.commit()
        return
    if doc.state != "Queued":
        return
    prior = _prior_publication(doc)
    if prior:
        _save(
            doc, "Blocked" if prior == "prior_publication_unknown" else "Queued", prior
        )
        frappe.db.commit()
        return
    try:
        assert_outbound_allowed()
        current_settings(doc)
    except (ValueError, frappe.ValidationError):
        _save(doc, "Blocked", "catalog_configuration_unavailable")
        frappe.db.commit()
        return
    _save(
        doc,
        "Submitting",
        "",
        submitted_at=now_datetime(),
        lease_until=now_datetime() + timedelta(minutes=2),
    )
    frappe.db.commit()  # Durable ambiguity boundary. Never automatically repost this request.
    try:
        settings = current_settings(doc)
        assert_outbound_allowed()
        from doco_meta_catalog.sync import _build_payloads

        frozen = json.loads(doc.request_body)["requests"]
        current, _ = _build_payloads(
            [row["data"]["id"] for row in frozen], settings, lock=True
        )
        desired = {row["data"]["id"]: row for row in current}
        if any(
            (
                row != desired.get(row["data"]["id"])
                if row["method"] == "UPDATE"
                else row["data"]["id"] in desired
            )
            for row in frozen
        ):
            raise ValueError("catalog_source_changed")
    except (ValueError, frappe.ValidationError):
        _save(doc, "Blocked", "catalog_configuration_changed")
        frappe.db.commit()
        return
    try:
        status, body = graph_request(
            "POST", settings, "items_batch", body=doc.request_body.encode()
        )
        if status >= 500 or 300 <= status < 400:
            _save(doc, "Unknown", "provider_outcome_uncertain", http_status=status)
        elif status >= 400:
            _save(
                doc,
                "Rejected",
                "provider_request_rejected",
                http_status=status,
                poll_complete=1,
                evidence=contract.canonical(
                    [{"at": str(now_datetime()), "kind": "rejected", "response": body}]
                ),
            )
        else:
            state, handles, errors, reason = contract.accepted(body)
            evidence = [
                {
                    "at": str(now_datetime()),
                    "kind": "accepted",
                    "validation_errors": errors,
                    "provider_handles": body.get("handles")
                    if isinstance(body, dict)
                    else None,
                }
            ]
            _save(
                doc,
                state,
                reason,
                handles=contract.canonical(handles),
                evidence=contract.canonical(evidence),
                http_status=status,
                poll_complete=int(not handles and state == "Rejected"),
            )
    except (requests.RequestException, ValueError, TypeError, UnicodeError):
        _save(doc, "Unknown", "provider_outcome_uncertain")
    frappe.db.commit()


def poll(name):
    """Read-only provider follow-up; never resends even an Unknown publication."""
    _worker_only()
    doc = frappe.get_doc(DOCTYPE, name, for_update=True)
    handles = json.loads(doc.handles or "[]")
    if not handles or doc.poll_complete:
        return
    try:
        settings = current_settings(doc)
        results = []
        for handle in handles:
            status, body = graph_request(
                "GET",
                settings,
                "check_batch_request_status",
                params={
                    "handle": handle,
                    "load_ids_of_invalid_requests": "true",
                    "fields": "handle,status,errors,errors_total_count,ids_of_invalid_requests,warnings,warnings_total_count",
                },
            )
            if status != 200:
                raise ValueError("batch_status_unavailable")
            results.append(contract.batch_result(body, handle))
        prior = json.loads(doc.evidence or "[]")
        finished = all(row["finished"] for row in results)
        rejected = doc.state == "Rejected" or any(row["rejected"] for row in results)
        state = "Rejected" if rejected else "Processed" if finished else "Pending"
        prior.append({"at": str(now_datetime()), "kind": "poll", "results": results})
        _save(
            doc,
            state,
            "item_rejected"
            if rejected
            else "awaiting_diagnostics"
            if finished
            else "awaiting_batch_result",
            evidence=contract.canonical(prior[:1] + prior[1:][-24:]),
            polled_at=now_datetime(),
            poll_attempts=int(doc.poll_attempts or 0) + 1,
            poll_complete=int(finished),
        )
        if not finished and doc.poll_attempts >= 24:
            _save(doc, "Unknown", "batch_poll_limit")
    except (
        requests.RequestException,
        ValueError,
        TypeError,
        UnicodeError,
        frappe.ValidationError,
    ):
        _save(
            doc,
            "Unknown",
            "batch_status_unavailable",
            polled_at=now_datetime(),
            poll_attempts=int(doc.poll_attempts or 0) + 1,
        )
    frappe.db.commit()


def process_pending():
    """Bounded recovery sweep; Unknown is visible and never an automatic resend."""
    _worker_only()
    for row in frappe.get_all(
        DOCTYPE,
        filters={"state": ["in", ["Queued", "Submitting"]]},
        fields=["name"],
        order_by="creation asc",
        limit=20,
    ):
        submit(row.name)
    for row in frappe.get_all(
        DOCTYPE,
        filters={"state": ["in", ["Pending", "Rejected"]], "poll_complete": 0},
        fields=["name"],
        order_by="polled_at asc, creation asc",
        limit=20,
    ):
        poll(row.name)


def known_items(settings):
    """Only IDs historically queued by this exact binding; never delete strangers."""
    return [
        row[0]
        for row in frappe.db.sql(
            """
        SELECT DISTINCT i.retailer_id FROM `tabMeta Catalog Publication Item` i
        JOIN `tabMeta Catalog Publication` p ON p.name=i.parent
        WHERE p.scope_revision=%s AND i.operation='UPDATE'
    """,
            scope(settings)["scope_revision"],
        )
    ]
