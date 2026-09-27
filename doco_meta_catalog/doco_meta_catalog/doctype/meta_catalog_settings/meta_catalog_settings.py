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
