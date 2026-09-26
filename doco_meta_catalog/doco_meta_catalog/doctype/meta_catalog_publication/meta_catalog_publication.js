frappe.ui.form.on('Meta Catalog Publication', {
  refresh(frm) {
    if (frm.doc.state !== 'Unknown' || !frappe.user.has_role('System Manager')) return
    if (frm.doc.handles && frm.doc.handles !== '[]') {
      frm.add_custom_button(__('Check provider batch status'), () => frappe.call({
        method: 'doco_meta_catalog.publication.check_status',
        args: { name: frm.doc.name },
        callback() { frappe.msgprint(__('Status check queued. Refresh this record for the result.')) },
      }))
    }
    frm.add_custom_button(__('Reconcile current item values'), () => {
      const request = crypto.randomUUID()
      const dialog = new frappe.ui.Dialog({
        title: __('Reconcile uncertain catalog publication'),
        fields: [
          { fieldtype: 'HTML', options: '<p>' + __('The prior provider effect remains unconfirmed. This action records your review and queues current item values for the same catalog and account. It does not mark the prior request approved or successful.') + '</p>' },
          { fieldname: 'note', label: __('Review note'), fieldtype: 'Small Text', reqd: 1 },
        ],
        primary_action_label: __('Reconcile current values'),
        async primary_action(values) {
          await frappe.call({
            method: 'doco_meta_catalog.publication.reconcile_unknown',
            args: { name: frm.doc.name, request_id: request, note: values.note },
            freeze: true,
            callback(response) {
              dialog.hide()
              frappe.set_route('Form', 'Meta Catalog Publication', response.message.publication)
            },
          })
        },
      })
      dialog.show()
    })
  },
})
