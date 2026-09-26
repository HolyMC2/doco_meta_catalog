from frappe.model.document import Document


class MetaCatalogPublication(Document):
    def validate(self):
        from doco_meta_catalog.publication import validate_evidence
        validate_evidence(self)

    def on_trash(self):
        import frappe
        frappe.throw("Publication evidence is retained.", frappe.PermissionError)
