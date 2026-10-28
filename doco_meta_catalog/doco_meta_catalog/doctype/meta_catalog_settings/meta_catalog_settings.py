import frappe
from frappe import _
from frappe.model.document import Document


class MetaCatalogSettings(Document):
    def validate(self):
        """Reject a binding the publication worker could not scope, at save time."""
        import re

        from doco_meta_catalog.publication_contract import graph_root

        if self.catalog_id and not re.fullmatch(r"[0-9]{1,40}", str(self.catalog_id)):
            frappe.throw(_("Catalog ID must be the numeric Meta catalog identifier."))
        try:
            graph_root(self.graph_api_version or "v21.0")
        except ValueError:
            frappe.throw(_("Graph API version must look like v21.0."))
        if self.enabled and not self.default_currency:
            frappe.throw(_("Set the catalog currency before enabling the catalog."))
        if self.storefront and not frappe.db.get_value("Storefront Profile", self.storefront, "enabled"):
            frappe.throw(_("The web shop {0} is turned off. Choose an enabled shop or leave it empty.").format(
                self.storefront))
        if self.product_link_base:
            from urllib.parse import urlsplit

            self.product_link_base = self.product_link_base.strip().rstrip("/")
            parsed = urlsplit(self.product_link_base)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query:
                frappe.throw(_("Product Page Address must look like https://mitienda.mx, without a path."))

    def get_token(self):
        if self.whatsapp_account:
            return frappe.get_doc("WhatsApp Account", self.whatsapp_account).get_password(
                "token", raise_exception=False
            )
        return self.get_password("access_token", raise_exception=False)

    def get_graph_root(self):
        return f"https://graph.facebook.com/{self.graph_api_version or 'v21.0'}"

    def get_capi_token(self):
        """Conversions API dataset token — SEPARATE from the catalog/WhatsApp token."""
        return self.get_password("capi_access_token", raise_exception=False)
