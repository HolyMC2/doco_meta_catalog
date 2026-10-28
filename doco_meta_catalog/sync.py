"""Bridge ERPNext Item -> Meta Commerce Catalog via the Graph API items_batch endpoint.

Source of truth = the storefront's PUBLISHED + SELLABLE view (``doco.docoutils.storefront``),
so the Facebook / Instagram / WhatsApp catalog matches the live web shop EXACTLY:

  - gate:         ``Item.publish_on_web == 1`` AND not disabled   (NOT ``show_in_website``)
                  AND in the mirrored shop's scope (catalog scope, hidden items, groups)
  - price:        ``Item Price.price_list_rate`` in the shop's price list (NOT ``standard_rate``)
  - availability: live Bin ``actual_qty - reserved_qty`` in the shop's warehouses;
                  non-stock items (services) always available
  - image:        public ``/files`` or ``https`` only — never ``/private`` or a signed B2/S3 URL
                  (Meta scrapes anonymously; a 401 image silently drops the item)
  - link:         the shop's product page, ``<shop domain>/producto/<code>``; a variant links
                  to its template's page (the storefront has no page per variant)

Variants are pushed as individual ``retailer_id``s grouped under their template via
``item_group_id`` (Meta's native variant grouping). Templates (``has_variants=1``) are
never pushed — their priced variants are.

An item whose price is ambiguous (two current prices, or its own pricing rule) is HELD:
it is neither updated nor removed on Meta, and the reason is shown to the shop. Only a
rule that applies to every item stops the whole catalog.

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

import re
from urllib.parse import quote as urlquote, urlsplit

import frappe
from frappe.utils import flt, get_url, strip_html_tags, today

from doco_meta_catalog.utils import assert_outbound_allowed

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
    s = frappe.get_doc(SETTINGS_DOCTYPE)
    if not s.enabled or not s.catalog_id:
        return None
    return s


def _shop(settings) -> tuple[dict | None, str]:
    """The web shop this catalog mirrors: (storefront profile or None, public origin).

    The catalog uses the shop's catalog scope, hidden items, price list and warehouses, and
    links to the shop's own domain. An empty setting picks the only enabled shop; with none
    or several, the cell defaults apply and links use this site's address."""
    name = (settings.get("storefront") or "").strip()
    if not name and frappe.db.exists("DocType", "Storefront Profile"):
        enabled = frappe.get_all("Storefront Profile", filters={"enabled": 1}, pluck="name", limit=2)
        name = enabled[0] if len(enabled) == 1 else ""
    profile = sf._profile(name) if name else None
    origin = (settings.get("product_link_base") or "").strip().rstrip("/")
    if not origin and name:
        domains = frappe.db.get_value("Storefront Profile", name, "domains") or ""
        host = next((line.strip() for line in domains.splitlines() if line.strip()), "")
        if host:
            origin = "https://" + host.split("://", 1)[-1].strip("/").lower()
    return profile, origin or get_url().rstrip("/")


def _product_link(origin: str, item: dict) -> str:
    page = item.get("variant_of") or item["name"]
    return f"{origin}/producto/{urlquote(sf._slugify(page), safe='')}"


# Kinds of skipped item. A held item keeps whatever Meta already shows.
HELD_KINDS = {"duplicate_price", "price_rule"}


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

_LEAF_FIELDS = ["name", "item_name", "description", "item_group", "image", "brand", "variant_of", "stock_uom",
                "default_manufacturer_part_no"]


def _eligible_leaves(item_codes: list[str] | None = None, *, lock=False, profile=None) -> list[dict]:
    """Published, sellable LEAF items (``has_variants=0``) — the same universe the storefront
    sells. Variant leaves ARE included (each a real sellable Item with its own Bin + Item
    Price); they group on Meta via ``item_group_id``. Templates are excluded. With a shop
    ``profile``, only the items that shop's product pages serve."""
    filters: dict = {"publish_on_web": 1, "disabled": 0, "has_variants": 0}
    if item_codes is not None:
        if not item_codes:
            return []
        filters["name"] = ["in", item_codes]
    leaves = frappe.db.get_values("Item", filters=filters, fieldname=_LEAF_FIELDS, limit=None, for_update=lock, as_dict=True)
    # A variant inherits a COPY of publish_on_web from its template; if the template is later
    # unpublished/disabled that copy can go stale. Mirror the storefront: a variant is sellable
    # only while its TEMPLATE is published — keeps catalog == web shop and blocks orphaned variants.
    templates = {l.get("variant_of") for l in leaves if l.get("variant_of")}
    if templates:
        live = set(
            frappe.db.get_values(
                "Item",
                filters={"name": ["in", list(templates)], "publish_on_web": 1, "disabled": 0},
                fieldname="name", pluck=True,
                for_update=lock,
            )
        )
        leaves = [l for l in leaves if not l.get("variant_of") or l.get("variant_of") in live]
    if profile and leaves:
        # The product page's own gate: catalog scope + hidden items + included/excluded
        # groups; a variant follows its template. An item the shop hides stays off Meta.
        shown = sf._scoped_products([l["name"] for l in leaves], profile)
        leaves = [l for l in leaves if l["name"] in shown]
    return leaves


# ---------------- variant grouping ----------------


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_")[:100]


def _variant_meta(leaves: list[dict], settings, *, lock=False) -> dict:
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
        for r in frappe.db.get_values("Item", filters={"name": ["in", list(templates)]}, fieldname=["name", "item_name"], for_update=lock, as_dict=True)
    }
    group_attr = (getattr(settings, "variant_group_attribute", None) or "").strip()
    color_attr = (getattr(settings, "variant_color_attribute", None) or "").strip()
    wanted = [a for a in (group_attr, color_attr) if a]
    attr_map: dict = {}
    if wanted:
        for r in frappe.db.get_values(
            "Item Variant Attribute",
            filters={"parent": ["in", names], "attribute": ["in", wanted]},
            fieldname=["parent", "attribute", "attribute_value"],
            limit=None,
            for_update=lock,
            as_dict=True,
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
    return f"{flt(amount):.2f} {currency}"


def _public_image(item: dict, settings, *, images=None, lock=False) -> str | None:
    """Apply the same privacy guard to item and fallback, then require public HTTPS."""
    for raw in (item.get("image"), settings.fallback_image_url):
        if not raw:
            continue
        safe = images.get(raw) if images is not None else sf._image_url(raw, lock=lock)
        if not safe:
            continue
        if safe.startswith("/"):
            base = (settings.image_url_base or get_url()).rstrip("/")
            safe = base + safe
        parsed = urlsplit(safe)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return safe
    return None


def _build_payloads(item_codes: list[str] | None, settings, *, lock=False) -> tuple[list[dict], list[dict]]:
    """Build the items_batch ``requests`` array for the eligible items.

    Returns ``(requests, skipped)`` where each ``skipped`` is ``{code, kind, reason}``. An item
    is skipped (never sent) when it has no selling price or no public image — exactly the cases
    the storefront would also refuse to sell, so the two surfaces stay identical. Kinds in
    ``HELD_KINDS`` are price problems on one item: callers keep that item as Meta has it."""
    profile, origin = _shop(settings)
    leaves = _eligible_leaves(item_codes, lock=lock, profile=profile)
    if not leaves:
        return [], []

    names = [it["name"] for it in leaves]
    # §19.3 marketing-channel identifiers — reuse the storefront's EXACT GTIN resolution (native
    # Item Barcode rows) so FB/IG/Google carry the same g:gtin the RSS feed emits (no divergence).
    gtins = sf._feed_gtins(names, lock=lock)
    from doco_meta_catalog.catalog_pricing import quote

    pricing = quote(names, settings, lock=lock, hold=True,
                    price_list=profile["price_list"] if profile else None)
    prices = pricing["base_rates"]
    held = pricing.get("held") or {}
    # 'out' | 'low' | 'in'; services always 'in'
    if profile:
        levels = sf._stock_levels(names, profile["warehouses"], profile["low_threshold"], lock=lock)
    else:
        levels = sf._stock_levels(names, lock=lock)
    # Generic catalog prices cannot safely quote customer/quantity Pricing Rules.
    # quote() visibly blocks that configuration; never call _sale_prices here:
    # it elevates, inserts a throwaway Sales Order and rolls back the caller.
    overrides = _group_overrides(settings)
    # Default 0 → Meta price == the exact shop price (parity). A shop may opt into a markup.
    markup = 1 + flt(settings.price_markup_percent) / 100.0
    from doco_meta_catalog.catalog_pricing import catalog_currency

    currency = catalog_currency(settings)
    # staging = synced to the catalog but NOT shown on public FB/IG (review-first); published = live.
    default_visibility = settings.default_visibility or "staging"
    # No shop name is baked into settings; an unset brand is the tenant's own company.
    brand = settings.default_brand or frappe.defaults.get_global_default("company") or ""
    vmeta = _variant_meta(leaves, settings, lock=lock)  # clean per-template (+ optional per-model) variant groups

    from doco.docoutils.storefront._common import _image_urls

    images = _image_urls([item.get("image") for item in leaves] + [settings.fallback_image_url], lock=lock)
    reqs: list[dict] = []
    skipped: list[dict] = []
    for it in leaves:
        code = it["name"]
        ov = overrides.get(it.get("item_group") or "", {})
        if ov.get("exclude"):
            skipped.append({"code": code, "kind": "excluded", "reason": "item group excluded from Meta catalog"})
            continue
        if code in held:
            skipped.append({"code": code, **held[code]})
            continue
        rate = prices.get(code)
        if not rate:
            skipped.append({"code": code, "kind": "unpriced", "reason": "no Item Price in selling price list"})
            continue
        img = _public_image(it, settings, images=images, lock=lock)
        if not img:
            skipped.append({"code": code, "kind": "no_image",
                            "reason": "no public image (private/signed/missing, no fallback)"})
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
            "link": _product_link(origin, it),
            "image_link": img,
            "brand": it.get("brand") or brand,
            "visibility": ov.get("visibility") or default_visibility,
            # MA-9: item_group as product_type so Meta Product Sets can auto-curate by group.
            "product_type": it.get("item_group") or "",
        }
        # gtin/mpn: same native sources + edge regex the storefront feed uses, so the two channels
        # carry identical identifiers. Meta 'gtin'/'mpn' are optional; omit rather than send empties.
        g = gtins.get(code, "")
        if g and re.fullmatch(r"\d{8}|\d{12,14}", g):
            data["gtin"] = g
        mpn = str(it.get("default_manufacturer_part_no") or "").strip()[:70]
        if mpn:
            data["mpn"] = mpn
        if group_id:
            data["item_group_id"] = group_id  # group a template's variants (per model when configured)
        if vm.get("color"):
            data["color"] = vm["color"]  # Meta variant option
        if ov.get("google_product_category"):
            data["google_product_category"] = ov["google_product_category"]
        # MA-5: discounted price (struck-through original kept as `price`). markup applied
        # to both so sale_price < price holds. effective_date only when bounded.
        reqs.append({"method": "UPDATE", "data": data})
    return reqs, skipped


def _sale_window() -> str | None:
    """ISO8601 interval for Meta ``sale_price_effective_date``, derived from the
    active price-discount selling Pricing Rules' date bounds (MX/CST, -06:00).

    Returns None when ANY active sale rule is open-ended (no valid_upto) — then we
    omit the field and let the sale stand until the next sync clears it. The window
    is shop-level (min start .. max end); for the common single-promo case it is
    exact, and the daily reconcile corrects any multi-rule drift."""
    td = today()
    starts: list[str] = []
    ends: list[str] = []
    for r in frappe.get_all(
        "Pricing Rule",
        filters={"selling": 1, "disable": 0, "coupon_code_based": 0, "price_or_product_discount": "Price"},
        fields=["valid_from", "valid_upto"], limit=50,
    ):
        vf, vu = r.get("valid_from"), r.get("valid_upto")
        if vf and str(vf) > td:
            continue  # not started
        if vu and str(vu) < td:
            continue  # already ended
        starts.append(str(vf) if vf else td)
        if not vu:
            return None  # an open-ended active rule → don't constrain the window
        ends.append(str(vu))
    if not ends:
        return None
    return f"{min(starts)}T00:00:00-06:00/{max(ends)}T23:59:59-06:00"


# ---------------- durable publication ----------------


def _post_items_batch(settings, requests_payload, request_id=None):
    """Compatibility producer: persist exact intent; only the worker sends it."""
    assert_outbound_allowed()
    from doco_meta_catalog.publication import queue

    return queue(settings, requests_payload, request_id=request_id)


# ---------------- doc-event hooks ----------------


def _queue_refresh(item_name, doc, method):
    """Enqueue only. Binding validation and scope belong to the worker, never to the
    stock/price/order transaction that fired the event."""
    from doco_meta_catalog.publication_contract import digest

    settings = _get_settings()
    if not settings:
        return
    request = digest([doc.doctype, doc.name, str(doc.modified), method, item_name])
    # One pending refresh per item and binding; the worker re-reads current state.
    key = digest([str(settings.catalog_id), str(settings.whatsapp_account or ""), item_name])
    # A change committed while that job already runs is deduplicated away; the
    # dirty mark makes the running job refresh once more when it finishes.
    frappe.db.after_commit.add(lambda: frappe.cache.hset(_DIRTY, key, 1))
    _enqueue_refresh(item_name, key, request_id=request)


_DIRTY = "meta_catalog_refresh_dirty"


def _enqueue_refresh(item_name, key, *, request_id=None, rerun=False):
    # A follow-up uses the other job id: the running job still holds its own.
    frappe.enqueue("doco_meta_catalog.sync.push_one", item_name=item_name,
                   request_id=request_id, refresh_key=key, rerun=rerun, queue="short",
                   job_id="meta-catalog-refresh-" + key + ("-rerun" if rerun else ""),
                   deduplicate=True, enqueue_after_commit=True)


def _never_block(handler):
    """Catalog refresh must not fail the business document that triggered it.

    Database errors still propagate: the transaction is already unusable then.
    """
    from functools import wraps

    @wraps(handler)
    def run(doc, method=None):
        nonfatal_errors = (frappe.ValidationError, frappe.PermissionError,
                           ValueError, KeyError, TypeError, AttributeError)
        try:
            return handler(doc, method)
        except nonfatal_errors:
            try:
                frappe.log_error(title="Meta catalog refresh not queued",
                                 reference_doctype=doc.get("doctype"), reference_name=doc.get("name"))
            except nonfatal_errors:
                # Queue-pressure diagnostics can hit the same permission/validation
                # guard. Keep the after-commit dirty mark; database errors still raise.
                pass

    return run


@_never_block
def queue_item_sync(doc, method=None):
    """Every change matters, including unpublish/disable/template changes."""
    if not _get_settings():
        return
    _queue_refresh(doc.name, doc, method)
    for name in frappe.get_all("Item", filters={"variant_of": doc.name}, pluck="name", limit_page_length=0):
        _queue_refresh(name, doc, method)


def queue_item_delete(doc, method=None):
    # Resolve after commit: an on_trash rollback must never remove a remote item.
    queue_item_sync(doc, method)


@_never_block
def queue_price_or_stock_sync(doc, method=None):
    # Hot stock path: no bundle lookups on tenants without an enabled catalog binding.
    if not _get_settings():
        return
    if doc.get("item_code"):
        _queue_refresh(doc.item_code, doc, method)
        # A component's Bin also changes the published bundle's availability.
        bundles = frappe.get_all("Product Bundle Item", filters={"item_code": doc.item_code},
                                  pluck="parent", limit_page_length=0)
        if bundles:
            for code in frappe.get_all("Product Bundle", filters={"name": ["in", bundles]},
                                       pluck="new_item_code", limit_page_length=0):
                _queue_refresh(code, doc, method)


@_never_block
def queue_document_stock_sync(doc, method=None):
    if not _get_settings():
        return
    for code in {row.item_code for row in doc.get("items") or [] if row.get("item_code")}:
        _queue_refresh(code, doc, method)


# ---------------- workers ----------------


def push_one(item_name: str, request_id=None, expected_scope=None, refresh_key=None, rerun=False):
    if refresh_key:
        frappe.cache.hdel(_DIRTY, refresh_key)
    try:
        return _push_one(item_name, request_id, expected_scope)
    finally:
        if refresh_key and frappe.cache.hget(_DIRTY, refresh_key):
            _enqueue_refresh(item_name, refresh_key, rerun=not rerun)


def _push_one(item_name, request_id=None, expected_scope=None):
    from doco_meta_catalog import publication

    s = _get_settings()
    if not s:
        return {"state": "Blocked", "reason_code": "catalog_disabled"}
    try:
        if expected_scope and publication.scope(s)["scope_revision"] != expected_scope:
            return {"state": "Blocked", "reason_code": "catalog_binding_changed"}
        reqs, skipped = _build_payloads([item_name], s)
        held = [row for row in skipped if row["kind"] in HELD_KINDS]
        if held:
            # Meta keeps its last good price until the shop fixes this one.
            return {"state": "Blocked", "reason_code": "catalog_price_review", "detail": held[0]["reason"][:200]}
        if not reqs:
            # Lost eligibility means removal, but only of IDs this binding published.
            if item_name not in publication.known_items(s):
                return {"state": "Skipped", "reason_code": "never_published"}
            reqs = [{"method": "DELETE", "data": {"id": item_name}}]
    except (frappe.ValidationError, ValueError) as error:
        # A price/configuration blocker is visible state, not an RQ failure.
        return {"state": "Blocked", "reason_code": "catalog_review_required", "detail": str(error)[:200]}
    return _post_items_batch(s, reqs, request_id=request_id)


def delete_one(item_name: str):
    # Re-read current eligibility so a delayed deletion cannot remove a republished SKU.
    return push_one(item_name)


def full_reconcile():
    """Queue canonical upserts and removals for IDs owned by this binding."""
    from doco_meta_catalog import publication

    s = _get_settings()
    if not s:
        return {"state": "Blocked", "reason_code": "catalog_disabled"}
    try:
        reqs, skipped = _build_payloads(None, s)
    except (frappe.ValidationError, ValueError) as error:
        frappe.db.set_single_value(SETTINGS_DOCTYPE, {
            "last_full_reconcile": frappe.utils.now(),
            "last_full_reconcile_status": "BLOCKED: canonical catalog price/source requires review",
            "last_error": str(error)[:500],
        })
        return {"state": "Blocked", "reason_code": "canonical_price_or_source_unavailable"}
    desired = {row["data"]["id"] for row in reqs}
    held = {row["code"] for row in skipped if row["kind"] in HELD_KINDS}
    removed = sorted(set(publication.known_items(s)) - desired - held)
    operations = reqs + [{"method": "DELETE", "data": {"id": code}} for code in removed]
    queued = [_post_items_batch(s, operations[i:i + _BATCH_CHUNK])
              for i in range(0, len(operations), _BATCH_CHUNK)]
    status = f"QUEUED: {len(reqs)} updates, {len(removed)} removals, {len(held)} held for price review"
    frappe.db.set_single_value(SETTINGS_DOCTYPE, {
        "last_full_reconcile": frappe.utils.now(), "last_full_reconcile_status": status[:140], "last_error": "",
    })
    return {"state": "Queued", "publications": queued, "updates": len(reqs),
            "removals": len(removed), "held": len(held), "skipped": len(skipped)}


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
    return push_one(item_code)


@frappe.whitelist()
def dry_run(limit: int = 20):
    """Build the WHOLE catalog payload WITHOUT posting to Meta. Returns the eligible/skipped
    counts, a sample of the real payloads, and every skipped item with its reason — so the
    Meta feed can be eyeballed for parity against the live storefront BEFORE a real
    catalog_id/token is wired. Works even when the master gate is off."""
    frappe.only_for("System Manager")
    s = frappe.get_doc(SETTINGS_DOCTYPE)  # bypass _get_settings: dry_run needs no catalog/token
    return preview(s, int(limit or 20))


def preview(settings, limit=20) -> dict:
    """What a full sync would send, and every item it would leave out, grouped by why."""
    profile, origin = _shop(settings)
    reqs, skipped = _build_payloads(None, settings)
    by_kind: dict[str, list] = {}
    for row in skipped:
        by_kind.setdefault(row["kind"], []).append(row)
    return {
        "eligible": len(reqs),
        "skipped": len(skipped),
        "skipped_detail": skipped[:limit],
        "by_kind": {kind: len(rows) for kind, rows in by_kind.items()},
        "examples": {kind: rows[:limit] for kind, rows in by_kind.items()},
        "sample": [r["data"] for r in reqs[:limit]],
        "shop": profile["name"] if profile else None,
        "link_origin": origin,
        "price_list": profile["price_list"] if profile else sf._selling_price_list(),
        "settings_enabled": bool(settings.enabled),
        "catalog_id_set": bool(settings.catalog_id),
    }
