import frappe
from frappe.model.document import Document


class MetaCatalogSettings(Document):
    def get_token(self):
        if self.whatsapp_account:
            return frappe.get_doc("WhatsApp Account", self.whatsapp_account).get_password(
                "token", raise_exception=False
            )
        return self.get_password("access_token", raise_exception=False)

    def get_graph_root(self):
        return f"https://graph.facebook.com/{self.graph_api_version or 'v21.0'}"

    def get_app_secret(self):
        """Meta App Secret used to verify inbound webhook HMAC signatures."""
        return self.get_password("app_secret", raise_exception=False)
