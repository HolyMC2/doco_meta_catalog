"""Private receipt-backed intake. Generic Desk/REST access cannot lend authority."""
import frappe
from frappe.model.document import Document


def get_permission_query_conditions(user=None):
    return "1=0"


def has_permission(doc, ptype=None, user=None, permission_type=None):
    return False


def filter_shared_documents(user, doctype, names):
    return []


class MetaOrderLog(Document):
    def notify_update(self):
        return

    def has_permission(self, permtype="read", *, debug=False, user=None):
        from doco_meta_catalog.orders import _SERVICE_TOKEN
        return permtype in {"create", "write"} and self.flags.get("meta_order_service") is _SERVICE_TOKEN

    def check_permission(self, permtype="read", permlevel=None):
        if not self.has_permission(permtype):
            raise frappe.PermissionError("Use the conversation cart review broker.")

    def validate(self):
        from doco_meta_catalog.orders import IMMUTABLE, _SERVICE_TOKEN
        from doco_meta_catalog.orders_contract import digest
        if self.flags.get("meta_order_service") is not _SERVICE_TOKEN:
            raise frappe.PermissionError("Use the conversation cart review broker.")
        if self.identity_state != "verified" or not self.intake_key or self.intake_key != self.receipt:
            frappe.throw("Verified receipt identity is required.")
        if digest(frappe.parse_json(self.raw_order)) != self.raw_order_hash:
            frappe.throw("Cart evidence is immutable.")
        before = self.get_doc_before_save()
        if before and any(self.get(field) != before.get(field) for field in IMMUTABLE):
            frappe.throw("Cart receipt evidence is immutable.")

    def on_trash(self):
        raise frappe.PermissionError("Retain cart and order evidence.")

    def before_rename(self, old, new, merge=False):
        raise frappe.PermissionError("Cart identities cannot be renamed.")
