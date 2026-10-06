import frappe


def assert_outbound_allowed():
    """Refuse ANY outbound Meta write when the site opts in via site_config
    ``meta_catalog_block_outbound``.

    Why: a mirror/lab site restored from prod carries the LIVE catalog_id +
    working token, and ``queue_item_sync`` fires on every Item save — queue
    workers don't honor a paused scheduler, so one test edit on a mirror
    mutates the real Meta catalog. Opt-in flag set on mirror sites only;
    inert everywhere else. Same pattern as the taller ``in_test`` /
    saldo mock-gateway guards.

    Guards the four outbound choke points: items_batch (sync), product sets,
    CAPI events, and WA product messages.
    """
    if frappe.conf.get("meta_catalog_block_outbound"):
        # Mirror and test sites only: tells support why nothing reached Meta.
        frappe.throw(  # jargon-ok
            "Meta Catalog: outbound Meta writes are blocked on this site "
            "(site_config meta_catalog_block_outbound). This is a mirror/lab "
            "guard — the settings here point at the LIVE catalog."
        )
