"""Desk management for the Meta catalog: «is it working, and what needs me?» in one call.

Reads only. Every action the page offers goes through the existing workers
(full reconcile, item refresh, diagnostics job); nothing here talks to Meta.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from doco_meta_catalog import publication, sync

_PREVIEW_KEY = "meta_catalog_preview"
_PREVIEW_TTL = 900  # A full preview reads every published item; keep it 15 minutes.
_STALE_HOURS = 36  # The daily check plus a little slack.
_ATTENTION = ("Unknown", "Blocked", "Rejected")
_RECENT_DAYS = 14


def _reason(code: str | None) -> str:
    """Plain words for the reason codes the workers record."""
    return {
        "catalog_connection_unavailable": _("Meta did not answer the connection check."),
        "catalog_link_not_returned": _("Meta does not list this catalog on the WhatsApp account."),
        "catalog_phone_unconfirmed": _("Meta did not confirm the WhatsApp number."),
        "catalog_phone_disconnected": _("The WhatsApp number is disconnected in Meta."),
        "catalog_commerce_unconfirmed": _("Meta did not return the WhatsApp shop settings."),
        "catalog_visibility_disabled": _("The catalog is hidden in WhatsApp (WhatsApp Manager → Catalog)."),
        "catalog_connection_unchecked": _("Not checked yet."),
        "catalog_connection_stale": _("The last check is more than a day old."),
        "prior_publication_unknown": _("Waits for an earlier send whose result is unknown."),
        "prior_publication_pending": _("Waits for an earlier send to finish."),
        "catalog_configuration_changed": _("Prices, stock or settings changed before sending; a newer send replaces it."),
        "catalog_configuration_unavailable": _("The catalog settings were incomplete when it ran."),
        "provider_outcome_uncertain": _("Meta did not answer clearly; the result is unknown."),
        "submission_interrupted": _("The send was interrupted; the result is unknown."),
        "batch_status_unavailable": _("Meta's batch status could not be read."),
        "batch_poll_limit": _("Meta never reported the batch as finished."),
        "provider_request_rejected": _("Meta refused the request."),
        "item_rejected": _("Meta refused one or more products."),
        "awaiting_diagnostics": _("Sent; Meta's review shows up after the next check."),
        "awaiting_batch_result": _("Sent; waiting for Meta's batch result."),
        "diagnostic_page_limit": _("The catalog is larger than one check reads."),
        "diagnostic_scan_incomplete": _("Meta stopped answering part way through the check."),
    }.get(code or "", code or "")


# ---------------- reads ----------------


def _preview(settings, refresh=False) -> dict:
    """The full-sync preview, cached per settings revision."""
    key = f"{_PREVIEW_KEY}:{settings.modified}"
    if not refresh:
        cached = frappe.cache.get_value(key)
        if cached:
            return cached
    try:
        data = sync.preview(settings, limit=10)
    except (frappe.ValidationError, ValueError) as error:
        data = {"error": str(error)[:500]}
    data["computed_at"] = str(now_datetime())
    frappe.cache.set_value(key, data, expires_in_sec=_PREVIEW_TTL)
    return data


def _meta(settings) -> dict | None:
    """Latest check of what Meta shows, for the current catalog binding."""
    try:
        identity = publication.scope(settings)
    except ValueError:
        return None
    row = frappe.db.get_value(
        publication.RUN,
        {"scope_revision": identity["scope_revision"]},
        ["name", "state", "reason_code", "checked_at", "observed_count", "summary"],
        as_dict=True,
        order_by="creation desc, name desc",
    )
    if not row:
        return {"checked": False}
    summary = frappe.parse_json(row.summary or "{}") or {}
    age_hours = (now_datetime() - get_datetime(row.checked_at)).total_seconds() / 3600
    conn = summary.get("connection") or {}
    return {
        "checked": True,
        "run": row.name,
        # Per-item rows live on the weekly/manual runs only.
        "items_run": frappe.db.get_value(
            publication.RUN, {"scope_revision": identity["scope_revision"], "items_stored": 1},
            "name", order_by="creation desc, name desc"),
        "state": row.state,
        "reason": _reason(row.reason_code),
        "checked_at": str(row.checked_at),
        "stale": age_hours > _STALE_HOURS,
        "total": row.observed_count or 0,
        "by_review": summary.get("by_review") or {},
        "by_visibility": summary.get("by_visibility") or {},
        "by_channel": summary.get("by_channel") or {},
        "actionable": summary.get("actionable") or 0,
        "sample": summary.get("sample") or [],
        "drift": summary.get("drift") or {},
        "connection": {**conn, "reason": _reason(conn.get("reason_code"))},
    }


def _publications() -> dict:
    counts = dict(frappe.db.sql("SELECT state, COUNT(*) FROM `tabMeta Catalog Publication` GROUP BY state"))
    since = frappe.utils.add_days(now_datetime(), -_RECENT_DAYS)
    rows = frappe.get_all(
        publication.DOCTYPE,
        filters={"state": ["in", _ATTENTION]},
        fields=["name", "state", "reason_code", "creation", "http_status"],
        order_by="creation desc",
        limit=50,
    )
    # An Unknown send fences later sends of the same products until reviewed, so it
    # stays listed however old; refused or blocked sends only while recent.
    attention = [
        {**row, "reason": _reason(row.reason_code), "creation": str(row.creation)}
        for row in rows
        if row.state == "Unknown" or get_datetime(row.creation) >= since
    ][:10]
    unknown = counts.get("Unknown", 0)
    return {"counts": counts, "attention": attention, "unknown": unknown}


def _shop_info(settings, preview: dict) -> dict:
    enabled = frappe.get_all("Storefront Profile", filters={"enabled": 1}, pluck="name") if frappe.db.exists(
        "DocType", "Storefront Profile") else []
    return {
        "chosen": settings.storefront or None,
        "used": preview.get("shop"),
        "enabled_shops": enabled,
        "link_origin": preview.get("link_origin"),
        "price_list": preview.get("price_list"),
    }


@frappe.whitelist()
def overview(refresh=0):
    """Everything the settings page shows: shop, products, Meta, sends, and what to do next."""
    frappe.only_for("System Manager")
    settings = frappe.get_doc(sync.SETTINGS_DOCTYPE)
    preview = _preview(settings, refresh=cint(refresh))
    data = {
        "settings": {
            "enabled": cint(settings.enabled),
            "catalog_id": settings.catalog_id or "",
            "whatsapp_account": settings.whatsapp_account or "",
            "token": bool(settings.catalog_id and settings.get_token()),
            "default_visibility": settings.default_visibility or "staging",
            "blocked_here": bool(frappe.conf.get("meta_catalog_block_outbound")),
        },
        "shop": _shop_info(settings, preview),
        "preview": preview,
        "meta": _meta(settings),
        "publications": _publications(),
        "reconcile": {
            "at": str(settings.last_full_reconcile or ""),
            "status": settings.last_full_reconcile_status or "",
            "error": settings.last_error or "",
        },
    }
    data["todo"] = _todo(settings, data)
    return data


def _todo(settings, data) -> list[dict]:
    """Ordered list of what needs the shop's attention, each with the action that fixes it.

    level: red = nothing reaches shoppers until fixed; orange = some products are affected;
    blue = worth knowing. An empty list means the catalog is in order."""
    out = []
    s, preview, meta, pubs = data["settings"], data["preview"], data["meta"], data["publications"]

    def add(level, title, detail="", actions=None):
        out.append({"level": level, "title": title, "detail": detail, "actions": actions or []})

    if s["blocked_here"]:
        add("blue", _("This is a test copy: nothing is sent to Meta from here."),
            _("The site setting meta_catalog_block_outbound stops every send."))
    if not s["enabled"]:
        add("red", _("The catalog is off. Nothing is sent to Meta."),
            _("Fill in the Catalog ID and the WhatsApp account below, then tick Enabled."))
        return out
    if not s["catalog_id"] or not s["token"]:
        add("red", _("The connection to Meta is incomplete."),
            _("Set the Catalog ID and choose the WhatsApp account whose connection the catalog uses."))
    if preview.get("error"):
        add("red", _("The catalog cannot be prepared."), preview["error"],
            [{"label": _("Open Pricing Rules"), "route": ["List", "Pricing Rule", {"disable": 0, "selling": 1}]}]
            if "pricing rule" in preview["error"].lower() else [])
    if (data["reconcile"]["status"] or "").startswith("BLOCKED"):
        add("red", _("The last full send was stopped."), data["reconcile"]["error"],
            [{"label": _("Send all now"), "method": "doco_meta_catalog.sync.sync_all_now"}])

    shop = data["shop"]
    if not shop["used"] and len(shop["enabled_shops"]) > 1:
        add("orange", _("Choose the web shop this catalog copies."),
            _("This site has several shops; until one is chosen, every published product is sent and links use this site's address."))
    elif (shop["used"] and not settings.product_link_base
          and not frappe.db.get_value("Storefront Profile", shop["used"], "domains")):
        add("orange", _("The shop has no domain, so product links open this site."),
            _("Add the shop's address under Dominios, or set Product Page Address below."),
            [{"label": _("Open the shop"), "route": ["Form", "Storefront Profile", shop["used"]]}])

    if meta and meta.get("checked"):
        visible = meta["by_visibility"].get("published", 0)
        if meta["total"] and not visible:
            add("red", _("Shoppers cannot see any product yet."),
                _("All {0} products on Meta are hidden (staging). They show on Facebook, Instagram and WhatsApp once set to visible.").format(meta["total"]),
                [{"label": _("Show products to shoppers"), "method": "doco_meta_catalog.management.set_visibility",
                  "args": {"visibility": "published"},
                  "confirm": _("Every product the shop publishes becomes visible on Facebook, Instagram and WhatsApp. Continue?")}])
    elif s["default_visibility"] != "published":
        add("orange", _("New products are sent hidden (staging)."),
            _("Shoppers see them only after you switch them to visible."),
            [{"label": _("Show products to shoppers"), "method": "doco_meta_catalog.management.set_visibility",
              "args": {"visibility": "published"},
              "confirm": _("Every product the shop publishes becomes visible on Facebook, Instagram and WhatsApp. Continue?")}])

    by_kind = preview.get("by_kind") or {}
    examples = preview.get("examples") or {}
    held = (examples.get("duplicate_price") or []) + (examples.get("price_rule") or [])
    held_count = by_kind.get("duplicate_price", 0) + by_kind.get("price_rule", 0)
    if held_count:
        codes = [row["code"] for row in held]
        add("orange", _("{0} products keep their last price on Meta until their price is fixed.").format(held_count),
            "\n".join(f"{row['code']}: {row['reason']}" for row in held[:10]),
            [{"label": _("Open these prices"),
              "route": ["List", "Item Price", {"item_code": ["in", codes], "selling": 1}]}])

    if meta is None or not meta.get("checked"):
        if s["catalog_id"]:
            add("orange", _("Meta has not been checked yet."),
                _("A check reads what Meta shows for every product: review, visibility and channels."),
                [{"label": _("Check Meta now"), "method": "doco_meta_catalog.diagnostics.queue_check"}])
    elif meta["stale"]:
        add("orange", _("Meta was last checked on {0}.").format(frappe.utils.format_datetime(meta["checked_at"])),
            _("The daily check has not run since then."),
            [{"label": _("Check Meta now"), "method": "doco_meta_catalog.diagnostics.queue_check"}])
    if meta and meta.get("checked"):
        conn = meta["connection"]
        if conn.get("state") in ("Unknown", "Unavailable"):
            add("orange", _("WhatsApp does not show the catalog yet."), conn.get("reason") or "")
        refused = sum(n for status, n in meta["by_review"].items() if status.lower() in ("rejected", "disapproved"))
        if refused:
            add("orange", _("Meta refused {0} products.").format(refused),
                _("Open Commerce Manager → Catalog → Issues for Meta's reason, or open the product to see it."),
                [{"label": _("See the refused products"),
                  "route": ["List", "Meta Catalog Diagnostic", {"diagnostic_run": meta["items_run"],
                                                               "review_status": ["in", ["rejected", "REJECTED"]]}]}]
                if meta.get("items_run") else [])

    if pubs["unknown"]:
        add("red", _("{0} sends have an unknown result.").format(pubs["unknown"]),
            _("The products in them are not sent again until someone reviews the send."),
            [{"label": _("Review them"), "route": ["List", "Meta Catalog Publication", {"state": "Unknown"}]}])

    if by_kind.get("no_image"):
        add("blue", _("{0} products have no photo and are not sent.").format(by_kind["no_image"]),
            _("Meta needs a public photo for every product. Add one, or set a Fallback Image URL below."),
            [{"label": _("Open products without photo"),
              "route": ["List", "Item", {"publish_on_web": 1, "disabled": 0, "has_variants": 0, "image": ["is", "not set"]}]}])
    if by_kind.get("unpriced"):
        add("blue", _("{0} products have no price in {1} and are not sent.").format(
            by_kind["unpriced"], preview.get("price_list") or ""),
            ", ".join(row["code"] for row in (examples.get("unpriced") or [])[:10]))
    if by_kind.get("excluded"):
        add("blue", _("{0} products are left out by the category list below.").format(by_kind["excluded"]))
    return out


@frappe.whitelist()
def item_status(item_code):
    """One product: would it be sent, and what does Meta show for it."""
    if not frappe.has_permission("Item", "read", item_code):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    settings = frappe.get_doc(sync.SETTINGS_DOCTYPE)
    out = {"enabled": cint(settings.enabled), "item_code": item_code}
    if not out["enabled"]:
        return out
    item = frappe.db.get_value("Item", item_code, ["publish_on_web", "disabled", "has_variants", "variant_of"],
                               as_dict=True)
    if not item:
        frappe.throw(_("Item {0} not found").format(item_code), frappe.DoesNotExistError)
    if item.has_variants:
        out.update(state="template", reason=_("Its variants are sent one by one, each with its own price and stock."))
    elif item.disabled or not item.publish_on_web:
        out.update(state="not_published", reason=_("Not published in the web shop, so it is not sent."))
    else:
        try:
            reqs, skipped = sync._build_payloads([item_code], settings)
        except (frappe.ValidationError, ValueError) as error:
            reqs, skipped = [], [{"code": item_code, "kind": "error", "reason": str(error)[:300]}]
        if reqs:
            data = reqs[0]["data"]
            out.update(state="sent", payload={key: data.get(key) for key in (
                "title", "price", "availability", "visibility", "link", "image_link")})
        elif skipped:
            row = skipped[0]
            out.update(state="held" if row["kind"] in sync.HELD_KINDS else "skipped", kind=row["kind"],
                       reason=_skip_reason(row))
        else:
            out.update(state="out_of_shop", reason=_("The web shop does not show it (hidden, or outside the shop's catalog)."))
    out["last_send"] = _last_send(item_code)
    out["meta"] = _item_meta(settings, item_code)
    return out


def _skip_reason(row) -> str:
    return {
        "unpriced": _("No price in the shop's price list."),
        "no_image": _("No public photo."),
        "excluded": _("Its category is left out of the catalog."),
    }.get(row["kind"], row["reason"])


def _last_send(item_code):
    row = frappe.db.sql(
        """
        SELECT p.name, p.state, p.reason_code, p.creation, i.operation
        FROM `tabMeta Catalog Publication Item` i
        JOIN `tabMeta Catalog Publication` p ON p.name = i.parent
        WHERE i.retailer_id = %s
        ORDER BY p.creation DESC LIMIT 1
        """,
        item_code,
        as_dict=True,
    )
    if not row:
        return None
    row = row[0]
    return {**row, "creation": str(row.creation), "reason": _reason(row.reason_code)}


def _item_meta(settings, item_code):
    try:
        identity = publication.scope(settings)
    except ValueError:
        return None
    row = frappe.db.sql(
        """
        SELECT d.review_status, d.fb_status, d.ig_status, d.wa_status, d.errors, d.checked_at, d.observed
        FROM `tabMeta Catalog Diagnostic` d
        WHERE d.scope_revision = %s AND d.retailer_id = %s
        ORDER BY d.checked_at DESC LIMIT 1
        """,
        (identity["scope_revision"], item_code),
        as_dict=True,
    )
    if not row:
        return None
    row = row[0]
    observed = frappe.parse_json(row.observed or "{}") or {}
    return {
        "review_status": row.review_status,
        "visibility": observed.get("visibility"),
        "facebook": row.fb_status,
        "instagram": row.ig_status,
        "whatsapp": row.wa_status,
        "errors": frappe.parse_json(row.errors or "[]") or [],
        "checked_at": str(row.checked_at),
    }


# ---------------- actions ----------------


@frappe.whitelist(methods=["POST"])
def set_visibility(visibility):
    """Switch what shoppers see, then resend everything with the new visibility."""
    frappe.only_for("System Manager")
    if visibility not in ("staging", "published"):
        frappe.throw(_("Choose visible or hidden."))
    settings = frappe.get_doc(sync.SETTINGS_DOCTYPE)
    settings.default_visibility = visibility
    settings.save()
    sync.sync_all_now()
    return {"default_visibility": visibility, "queued": True}

