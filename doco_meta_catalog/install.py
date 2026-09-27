"""Catalog schema prerequisites, called only by native install/migrate hooks."""


def ensure_catalog_schema():
    """Reuse Doco's unpublished-by-default gate without enabling the storefront.

    The owning historical patch commits. It belongs at this schema lifecycle
    boundary and must never be called from a catalog request or receipt worker.
    """
    import frappe

    field = frappe.get_meta("Item", cached=False).get_field("publish_on_web")
    if field and field.fieldtype != "Check":
        frappe.throw(
            "Item.publish_on_web must be Doco's canonical Check field. Review the schema before migrating."
        )
    if field and frappe.db.has_column("Item", "publish_on_web"):
        return

    frappe.get_attr("doco.patches.v0_0.add_publish_on_web_to_item.execute")()
    frappe.clear_cache(doctype="Item")
    field = frappe.get_meta("Item", cached=False).get_field("publish_on_web")
    if (
        not field
        or field.fieldtype != "Check"
        or not frappe.db.has_column("Item", "publish_on_web")
    ):
        frappe.throw(
            "Doco's Item.publish_on_web schema is incomplete. Synchronize Item through the guarded migration before using Meta Catalog."
        )
