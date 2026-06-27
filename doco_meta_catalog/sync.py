"""Bridge ERPNext Item -> Meta Commerce Catalog via the Graph API items_batch endpoint.

Source of truth = the storefront's PUBLISHED + SELLABLE view (``doco.docoutils.storefront``),
so the Facebook / Instagram / WhatsApp catalog matches the live web shop EXACTLY:

  - gate:         ``Item.publish_on_web == 1`` AND not disabled   (NOT ``show_in_website``)
  - price:        ``Item Price.price_list_rate`` in the selling price list (NOT ``standard_rate``)
  - availability: live Bin ``actual_qty - reserved_qty``; non-stock items (services) always available
  - image:        public ``/files`` or ``https`` only — never ``/private`` or a signed B2/S3 URL
                  (Meta scrapes anonymously; a 401 image silently drops the item)

Variants are pushed as individual ``retailer_id``s grouped under their template via
``item_group_id`` (Meta's native variant grouping). Templates (``has_variants=1``) are
never pushed — their priced variants are.

Public hooks (wired from hooks.py):
    queue_item_sync(doc, method)     Item.on_update
    queue_item_delete(doc, method)   Item.on_trash
    full_reconcile()                 scheduled daily (safety net for missed events)

Whitelisted (Desk):
    sync_all_now()        enqueue a full reconcile
    sync_item_now(code)   push one item now
    dry_run()             build the whole payload WITHOUT posting — parity inspection
"""

from __future__ import annotations

import json
import re

import frappe
import requests
from frappe.utils import flt, get_url, strip_html_tags

# The storefront module is the SINGLE SOURCE OF TRUTH for what is published, its real
# selling price, live stock, and which images are safe to expose to an anonymous caller.
# Reusing it (instead of re-deriving) guarantees the Meta catalog == the live shop.
# ``doco`` is always installed alongside this app (see required_apps in hooks.py).
from doco.docoutils import storefront as sf

SETTINGS_DOCTYPE = "Meta Catalog Settings"

_TITLE_MAX = 200  # Meta product title limit
_DESC_MAX = 9999
_BATCH_CHUNK = 1000  # Meta items_batch hard limit ~5000; smaller keeps payload size sane


# ---------------- settings ----------------


def _get_settings():
    """Active settings, or None when the master gate is off / no catalog wired.
    Hooks + workers short-circuit on None → zero side effects when not configured."""
    s = frappe.get_cached_doc(SETTINGS_DOCTYPE)
    if not s.enabled or not s.catalog_id:
        return None
    return s


def _group_overrides(settings) -> dict:
    """``{item_group: {"condition": .., "google_product_category": ..}}`` from the optional
    Meta Catalog Category Map child table. Lets a shop map e.g. ``Seminuevos -> refurbished``
    by DATA, not hardcoded to any vertical. Absent table / pre-migrate -> ``{}`` (defaults)."""
    out: dict[str, dict] = {}
    for row in settings.get("category_map") or []:
        g = (row.get("item_group") or "").strip()
        if not g:
            continue
        out[g] = {
            "condition": (row.get("condition") or "").strip() or None,
            "google_product_category": (row.get("google_product_category") or "").strip() or None,
            "exclude": bool(row.get("exclude")),
            "visibility": (row.get("visibility") or "").strip() or None,
        }
    return out


# ---------------- eligibility (mirror the storefront gate) ----------------

_LEAF_FIELDS = ["name", "item_name", "description", "item_group", "image", "brand", "variant_of", "stock_uom"]


def _eligible_leaves(item_codes: list[str] | None = None) -> list[dict]:
    """Published, sellable LEAF items (``has_variants=0``) — the same universe the storefront
    sells. Variant leaves ARE included (each a real sellable Item with its own Bin + Item
    Price); they group on Meta via ``item_group_id``. Templates are excluded."""
    filters: dict = {"publish_on_web": 1, "disabled": 0, "has_variants": 0}
    if item_codes is not None:
        if not item_codes:
            return []
        filters["name"] = ["in", item_codes]
    leaves = frappe.get_all("Item", filters=filters, fields=_LEAF_FIELDS, limit_page_length=0)
    # A variant inherits a COPY of publish_on_web from its template; if the template is later
    # unpublished/disabled that copy can go stale. Mirror the storefront: a variant is sellable
    # only while its TEMPLATE is published — keeps catalog == web shop and blocks orphaned variants.
    templates = {l.get("variant_of") for l in leaves if l.get("variant_of")}
    if templates:
        live = set(
            frappe.get_all(
                "Item",
                filters={"name": ["in", list(templates)], "publish_on_web": 1, "disabled": 0},
                pluck="name",
            )
        )
        leaves = [l for l in leaves if not l.get("variant_of") or l.get("variant_of") in live]
    return leaves


# ---------------- variant grouping ----------------


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_")[:100]


def _variant_meta(leaves: list[dict], settings) -> dict:
    """For variant leaves, resolve {code: {template_name, group_val, color}} used to build a clean
    Meta variant group. `group_val` (e.g. the phone model) splits one ERPNext template into
    per-model Meta products so a 1000-variant template is not one giant group; `color` becomes the
    Meta variant option. Both source attributes are CONFIGURABLE (variant_group_attribute /
    variant_color_attribute) so this stays vertical-neutral — blank → group by template only."""
    variants = [l for l in leaves if l.get("variant_of")]
    if not variants:
        return {}
    names = [l["name"] for l in variants]
    templates = {l["variant_of"] for l in variants}
    tmpl_names = {
        r["name"]: r["item_name"]
        for r in frappe.get_all("Item", filters={"name": ["in", list(templates)]}, fields=["name", "item_name"])
    }
    group_attr = (getattr(settings, "variant_group_attribute", None) or "").strip()
    color_attr = (getattr(settings, "variant_color_attribute", None) or "").strip()
    wanted = [a for a in (group_attr, color_attr) if a]
    attr_map: dict = {}
    if wanted:
        for r in frappe.get_all(
            "Item Variant Attribute",
            filters={"parent": ["in", names], "attribute": ["in", wanted]},
            fields=["parent", "attribute", "attribute_value"],
            limit_page_length=0,
        ):
            attr_map.setdefault(r["parent"], {})[r["attribute"]] = (r["attribute_value"] or "").strip()
    out = {}
    for l in variants:
        a = attr_map.get(l["name"], {})
        out[l["name"]] = {
            "template_name": tmpl_names.get(l["variant_of"]),
            "group_val": a.get(group_attr) if group_attr else None,
            "color": a.get(color_attr) if color_attr else None,
        }
    return out


# ---------------- payload mapping ----------------


def _availability(level: str) -> str:
    """Storefront stock level ('out' | 'low' | 'in') -> Meta availability string."""
    return "out of stock" if level == "out" else "in stock"


def _format_price(amount, currency: str) -> str:
    """Meta items_batch wants price as a STRING carrying the ISO currency, e.g.
    '2800.00 MXN'. A bare integer + a separate `currency` field is REJECTED by the API
    (warning 'Unrecognized field: currency') and silently falls back to the catalog's
    DEFAULT currency — our catalog defaults to USD, so an integer 280000 published as
    $2,800.00 USD instead of MX$2,800.00 (a ~17x mispricing). The string form pins the
    currency per item and is independent of the catalog default. Verified 2026-06-26."""
    return f"{flt(amount):.2f} {currency or 'MXN'}"


def _public_image(item: dict, settings) -> str | None:
    """Public HTTPS image Meta can scrape. Reuse the storefront guard (rejects ``/private``
    and signed B2/S3 object URLs that 401 for an anonymous crawler), resolve a relative
    ``/files`` path against ``image_url_base``, then fall back to the configured placeholder."""
    raw = item.get("image")
    safe = sf._image_url(raw) if raw else None
    if safe:
        if safe.startswith("http"):
            return safe
        base = (settings.image_url_base or get_url()).rstrip("/")
        return base + (safe if safe.startswith("/") else "/" + safe)
    return settings.fallback_image_url or None


def _build_payloads(item_codes: list[str] | None, settings) -> tuple[list[dict], list[dict]]:
    """Build the items_batch ``requests`` array for the eligible items.

    Returns ``(requests, skipped)`` where each ``skipped`` is ``{code, reason}``. An item is
    skipped (never sent) when it has no selling price or no public image — exactly the cases
    the storefront would also refuse to sell, so the two surfaces stay identical."""
    leaves = _eligible_leaves(item_codes)
    if not leaves:
        return [], []

    names = [it["name"] for it in leaves]
    price_list = sf._selling_price_list()
    prices = sf._prices(names, price_list)
    levels = sf._stock_levels(names)  # 'out' | 'low' | 'in'; services always 'in'
    overrides = _group_overrides(settings)
    # Default 0 → Meta price == the exact shop price (parity). A shop may opt into a markup.
    markup = 1 + flt(settings.price_markup_percent) / 100.0
    currency = settings.default_currency or "MXN"
    base_url = (settings.image_url_base or get_url()).rstrip("/")
    # staging = synced to the catalog but NOT shown on public FB/IG (review-first); published = live.
    default_visibility = settings.default_visibility or "staging"
    vmeta = _variant_meta(leaves, settings)  # clean per-template (+ optional per-model) variant groups

    reqs: list[dict] = []
    skipped: list[dict] = []
    for it in leaves:
        code = it["name"]
        ov = overrides.get(it.get("item_group") or "", {})
        if ov.get("exclude"):
            skipped.append({"code": code, "reason": "item group excluded from Meta catalog"})
            continue
        rate = prices.get(code)
        if not rate:
            skipped.append({"code": code, "reason": "no Item Price in selling price list"})
            continue
        img = _public_image(it, settings)
        if not img:
            skipped.append({"code": code, "reason": "no public image (private/signed/missing, no fallback)"})
            continue
        # variant title/group: name the group after the TEMPLATE (+ optional model split) so a
        # template's variants don't surface under one variant's name; color = the variant option.
        vm = vmeta.get(code) or {}
        if it.get("variant_of"):
            base = vm.get("template_name") or it.get("item_name") or code
            gval = vm.get("group_val")
            title = f"{base} {gval}".strip() if gval else base
            group_id = _slug(f"{it['variant_of']}_{gval}") if gval else it["variant_of"]
        else:
            title = it.get("item_name") or code
            group_id = None
        data = {
            "id": code,  # retailer_id == ERPNext item_code
            "title": title[:_TITLE_MAX],
            "description": (strip_html_tags(it.get("description") or "") or it.get("item_name") or code)[:_DESC_MAX],
            "availability": _availability(levels.get(code, "out")),
            "condition": ov.get("condition") or settings.default_condition or "new",
            "price": _format_price(flt(rate) * markup, currency),
            "link": f"{base_url}/shop/{code}",
            "image_link": img,
            "brand": it.get("brand") or settings.default_brand or "",
            "visibility": ov.get("visibility") or default_visibility,
        }
        if group_id:
            data["item_group_id"] = group_id  # group a template's variants (per model when configured)
        if vm.get("color"):
            data["color"] = vm["color"]  # Meta variant option
        if ov.get("google_product_category"):
            data["google_product_category"] = ov["google_product_category"]
        reqs.append({"method": "UPDATE", "data": data})
    return reqs, skipped


# ---------------- Meta transport ----------------


def _post_items_batch(settings, requests_payload):
    url = f"{settings.get_graph_root()}/{settings.catalog_id}/items_batch"
    token = settings.get_token()
    if not token:
        frappe.throw("Meta Catalog: no access token configured")
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"item_type": "PRODUCT_ITEM", "requests": requests_payload},
        timeout=60,
    )
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error", {})
            meta_err = f"{err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except Exception:
            meta_err = r.text[:500]
        frappe.log_error(
            title=f"Meta Catalog push HTTP {r.status_code}",
            message=f"meta_error={meta_err}\nitems={len(requests_payload)} sample={json.dumps(requests_payload[:1])[:800]}",
        )
        r.raise_for_status()
    return r.json()


# ---------------- doc-event hooks ----------------


def queue_item_sync(doc, method=None):
    """Item.on_update — enqueue a background push so the save is not blocked by Meta latency.
    Only published, sellable LEAF items reach Meta (mirror the storefront); templates and
    unpublished/disabled items short-circuit here with zero side effects."""
    if not _get_settings():
        return
    if doc.get("disabled") or not doc.get("publish_on_web") or doc.get("has_variants"):
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
    if not _get_settings():
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
    reqs, _ = _build_payloads([item_name], s)
    if reqs:
        _post_items_batch(s, reqs)


def delete_one(item_name: str):
    s = _get_settings()
    if not s:
        return
    _post_items_batch(s, [{"method": "DELETE", "data": {"id": item_name}}])


def full_reconcile():
    """Nightly: re-push every eligible Item in chunks (safety net for missed webhooks)."""
    s = _get_settings()
    if not s:
        return
    reqs, skipped = _build_payloads(None, s)
    sent = 0
    try:
        for i in range(0, len(reqs), _BATCH_CHUNK):
            chunk = reqs[i : i + _BATCH_CHUNK]
            _post_items_batch(s, chunk)
            sent += len(chunk)
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
            "last_full_reconcile_status": f"OK ({sent} sent, {len(skipped)} skipped)",
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


@frappe.whitelist()
def dry_run(limit: int = 20):
    """Build the WHOLE catalog payload WITHOUT posting to Meta. Returns the eligible/skipped
    counts, a sample of the real payloads, and every skipped item with its reason — so the
    Meta feed can be eyeballed for parity against the live storefront BEFORE a real
    catalog_id/token is wired. Works even when the master gate is off."""
    frappe.only_for("System Manager")
    s = frappe.get_cached_doc(SETTINGS_DOCTYPE)  # bypass _get_settings: dry_run needs no catalog/token
    n = int(limit or 20)
    reqs, skipped = _build_payloads(None, s)
    return {
        "eligible": len(reqs),
        "skipped": len(skipped),
        "skipped_detail": skipped[:n],
        "sample": [r["data"] for r in reqs[:n]],
        "price_list": sf._selling_price_list(),
        "settings_enabled": bool(s.enabled),
        "catalog_id_set": bool(s.catalog_id),
    }
