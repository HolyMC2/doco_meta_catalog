"""Retain legacy links while replacing unsafe global message-id uniqueness."""
import frappe


def execute():
    # Drop only the obsolete global-ID index before widening its column to Text.
    indexes = {}
    for row in frappe.db.sql("SHOW INDEX FROM `tabMeta Order Log`", as_dict=True):
        if not row.Non_unique and row.Key_name != "PRIMARY":
            indexes.setdefault(row.Key_name, []).append((row.Seq_in_index, row.Column_name))
    for name, columns in indexes.items():
        if [column for _, column in sorted(columns)] == ["wa_msg_id"]:
            frappe.db.sql("ALTER TABLE `tabMeta Order Log` DROP INDEX `" + name.replace("`", "``") + "`")
    frappe.reload_doc("doco_meta_catalog", "doctype", "meta_order_log")
    # New fields are additive; NULL identities remain distinct under the unique index.
    frappe.db.sql("""UPDATE `tabMeta Order Log`
        SET identity_state = 'account_unknown', state = 'Legacy'
        WHERE intake_key IS NULL OR intake_key = ''""")
