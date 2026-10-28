// Item form: one line on whether this product reaches Facebook, Instagram and WhatsApp,
// and if not, why. Only for products the web shop publishes; quiet when the catalog is off.
frappe.ui.form.on('Item', {
  refresh(frm) {
    if (frm.is_new() || !frm.doc.publish_on_web || frm.doc.disabled) return
    frappe.call({
      method: 'doco_meta_catalog.management.item_status',
      args: { item_code: frm.doc.name },
      callback(r) { showMetaStatus(frm, r.message) },
    })
  },
})

function showMetaStatus(frm, s) {
  if (!s || !s.enabled || s.state === 'not_published') return
  const esc = (value) => frappe.utils.escape_html(value == null ? '' : String(value))
  let color = 'blue'
  let text
  if (s.state === 'sent') {
    const visible = s.payload.visibility === 'published'
    const review = s.meta && s.meta.review_status ? ' · ' + __('Meta review: {0}', [s.meta.review_status]) : ''
    color = visible && (!s.meta || (s.meta.review_status || '').toLowerCase() === 'approved') ? 'green' : 'blue'
    text = __('Meta catalog: {0}, {1}, {2}', [s.payload.price, s.payload.availability, visible ? __('visible to shoppers') : __('hidden from shoppers')]) + review
  } else if (s.state === 'held') {
    color = 'orange'
    text = __('Meta catalog: kept at its last price. {0}', [s.reason])
  } else if (s.state === 'template') {
    text = __('Meta catalog: {0}', [s.reason])
  } else {
    text = __('Meta catalog: not sent. {0}', [s.reason || ''])
  }
  frm.dashboard.add_comment(esc(text), color, true)
  if (s.state === 'sent' && frappe.user.has_role('System Manager')) {
    frm.add_custom_button(__('Send to Meta now'), () => frappe.call({
      method: 'doco_meta_catalog.sync.sync_item_now',
      args: { item_code: frm.doc.name },
      freeze: true,
      callback(r) {
        const result = r.message || {}
        frappe.show_alert({
          message: result.state === 'Blocked' ? esc(result.detail || result.reason_code) : __('Queued for Meta.'),
          indicator: result.state === 'Blocked' ? 'orange' : 'green',
        })
      },
    }), __('Meta'))
  }
}
