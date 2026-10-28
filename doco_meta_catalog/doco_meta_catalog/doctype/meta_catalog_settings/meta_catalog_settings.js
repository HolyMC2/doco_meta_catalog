// Meta catalog management: what reaches Facebook, Instagram and WhatsApp, what is held
// back and why, and the action that fixes each thing. Data: management.overview.

const esc = (value) => frappe.utils.escape_html(value == null ? '' : String(value))
const LEVEL_COLOR = { red: 'red', orange: 'orange', blue: 'blue', green: 'green' }
const KIND_LABEL = {
  no_image: () => __('No photo'),
  unpriced: () => __('No price'),
  duplicate_price: () => __('Two prices'),
  price_rule: () => __('Pricing rule'),
  excluded: () => __('Category left out'),
}

frappe.ui.form.on('Meta Catalog Settings', {
  refresh(frm) {
    render(frm, 0)
    if (!frappe.user.has_role('System Manager')) return
    const group = __('Catalog')
    frm.add_custom_button(__('Send all now'), () => act(frm, {
      method: 'doco_meta_catalog.sync.sync_all_now',
      confirm: __('Send every product to Meta now? It takes a few minutes.'),
      done: __('Sending every product. Results show here in a few minutes.'),
    }), group)
    frm.add_custom_button(__('Check Meta now'), () => act(frm, {
      method: 'doco_meta_catalog.diagnostics.queue_check',
      done: __('Checking what Meta shows. Refresh this page in a few minutes.'),
    }), group)
    frm.add_custom_button(__('Look up a product'), () => lookup(frm), group)
    frm.add_custom_button(__('Preview the catalog'), () => preview(frm), group)
  },
})

async function render(frm, refresh) {
  const $wrap = frm.get_field('overview_html').$wrapper
  if (frm.is_dirty()) return
  $wrap.html(`<div class="text-muted small" style="padding: var(--padding-md) 0">${__('Reading the catalog…')}</div>`)
  let data
  try {
    data = (await frappe.call({ method: 'doco_meta_catalog.management.overview', args: { refresh } })).message
  } catch (e) {
    $wrap.html(`<div class="text-muted small">${__('The overview could not be loaded.')}</div>`)
    return
  }
  frm._meta_overview = data
  $wrap.html(overviewHtml(data))
  $wrap.find('[data-todo]').on('click', (event) => {
    const [i, j] = event.currentTarget.dataset.todo.split(':').map(Number)
    act(frm, data.todo[i].actions[j])
  })
  $wrap.find('[data-refresh]').on('click', () => render(frm, 1))
}

function stat(label, value, note) {
  return `<div class="col-sm-3" style="margin-bottom: var(--margin-sm)">
    <div class="text-muted small">${esc(label)}</div>
    <div style="font-size: var(--text-2xl); font-weight: 600">${esc(value)}</div>
    <div class="text-muted small">${note || ''}</div></div>`
}

function overviewHtml(d) {
  const p = d.preview || {}
  const m = d.meta || {}
  const kinds = Object.entries(p.by_kind || {})
    .map(([kind, n]) => `${esc((KIND_LABEL[kind] || (() => kind))())}: ${n}`).join(' · ')
  const vis = m.by_visibility || {}
  const metaNote = m.checked
    ? `${__('visible')}: ${vis.published || 0} · ${__('hidden')}: ${vis.staging || 0}`
    : __('not checked yet')
  const sent = d.reconcile.at ? frappe.datetime.comment_when(d.reconcile.at) : __('never')
  const stats = `<div class="row">
    ${stat(__('Ready to send'), p.error ? '—' : (p.eligible ?? '—'), p.shop ? esc(__('from shop {0}', [p.shop])) : '')}
    ${stat(__('On Meta'), m.checked ? m.total : '—', esc(metaNote))}
    ${stat(__('Not sent'), p.error ? '—' : (p.skipped ?? '—'), kinds)}
    ${stat(__('Last full send'), '', sent)}
  </div>`

  const todo = d.todo.length
    ? d.todo.map((t, i) => `<div style="padding: var(--padding-sm) 0; border-top: 1px solid var(--border-color)">
        <span class="indicator-pill ${LEVEL_COLOR[t.level] || 'gray'}">${esc(t.title)}</span>
        ${t.detail ? `<div class="text-muted small" style="white-space: pre-line; margin-top: var(--margin-xs)">${esc(t.detail)}</div>` : ''}
        ${(t.actions || []).map((a, j) => `<button class="btn btn-xs btn-default" style="margin-top: var(--margin-xs); margin-right: var(--margin-xs)" data-todo="${i}:${j}">${esc(a.label)}</button>`).join('')}
      </div>`).join('')
    : `<div style="padding: var(--padding-sm) 0"><span class="indicator-pill green">${__('Everything is in order.')}</span></div>`

  const channels = Object.entries(m.by_channel || {}).map(([channel, counts]) =>
    `<tr><td>${esc(channelName(channel))}</td><td>${esc(Object.entries(counts).map(([s, n]) => `${s}: ${n}`).join(' · '))}</td></tr>`).join('')
  const sends = Object.entries(d.publications.counts || {}).map(([state, n]) =>
    `<a href="/app/meta-catalog-publication?state=${encodeURIComponent(state)}">${esc(state)}: ${n}</a>`).join(' · ')
  const conn = (m.connection || {})
  const details = `<table class="table table-sm small" style="margin-top: var(--margin-md)"><tbody>
    <tr><td style="width: 30%">${__('Web shop')}</td><td>${esc(d.shop.used || __('none chosen'))} · ${esc(d.shop.link_origin || '')} · ${esc(d.shop.price_list || '')}</td></tr>
    <tr><td>${__('WhatsApp catalog')}</td><td>${esc(conn.state ? `${conn.state}${conn.cart_enabled ? ' · ' + __('cart on') : ''}${conn.reason ? ' · ' + conn.reason : ''}` : '—')}</td></tr>
    ${channels}
    <tr><td>${__('Sends')}</td><td>${sends || '—'}</td></tr>
  </tbody></table>`

  const footer = `<div class="text-muted small">
    ${p.computed_at ? esc(__('Products read {0}', [frappe.datetime.comment_when(p.computed_at)])) : ''}
    ${m.checked ? ' · ' + esc(__('Meta checked {0}', [frappe.datetime.comment_when(m.checked_at)])) : ''}
    · <a data-refresh>${__('Refresh')}</a></div>`

  return `<div style="margin-bottom: var(--margin-lg)">${stats}
    <div style="font-weight: 600; margin-top: var(--margin-sm)">${__('What needs you')}</div>${todo}${details}${footer}</div>`
}

function channelName(channel) {
  return {
    FB_SHOPS: __('Facebook shop'),
    INSTAGRAM_SHOPPING: __('Instagram shopping'),
    WHATSAPP: 'WhatsApp',
    WHATSAPP_SHOPPING: 'WhatsApp',
    MARKETPLACE: 'Marketplace',
    DYNAMIC_ADS: __('Ads'),
  }[channel] || channel
}

async function act(frm, action) {
  if (action.route) {
    frappe.route_options = action.route[2] || null
    frappe.set_route(action.route[0], action.route[1])
    return
  }
  if (action.confirm && !(await new Promise((ok) => frappe.confirm(action.confirm, () => ok(true), () => ok(false))))) return
  await frappe.call({ method: action.method, args: action.args || {}, freeze: true })
  frappe.show_alert({ message: action.done || __('Done. The overview refreshes now.'), indicator: 'green' })
  if (action.method.endsWith('set_visibility')) frm.reload_doc()
  else render(frm, 1)
}

function lookup(frm) {
  const dialog = new frappe.ui.Dialog({
    title: __('Look up a product'),
    fields: [
      { fieldname: 'item_code', fieldtype: 'Link', options: 'Item', label: __('Product'), reqd: 1,
        onchange: () => show(dialog) },
      { fieldname: 'result', fieldtype: 'HTML' },
    ],
    primary_action_label: __('Send this product now'),
    async primary_action(values) {
      const r = (await frappe.call({ method: 'doco_meta_catalog.sync.sync_item_now', args: { item_code: values.item_code }, freeze: true })).message || {}
      frappe.show_alert({ message: sendResult(r), indicator: r.state === 'Blocked' ? 'orange' : 'green' })
      show(dialog)
    },
  })
  dialog.show()
}

async function show(dialog) {
  const code = dialog.get_value('item_code')
  if (!code) return
  const s = (await frappe.call({ method: 'doco_meta_catalog.management.item_status', args: { item_code: code } })).message
  dialog.fields_dict.result.$wrapper.html(itemHtml(s))
}

function sendResult(r) {
  if (r.state === 'Blocked' || r.state === 'Skipped') return esc(r.detail || r.reason_code || r.state)
  return __('Queued. Meta shows the change after the next check.')
}

function itemHtml(s) {
  if (!s.enabled) return `<p class="text-muted">${__('The catalog is off.')}</p>`
  const state = {
    sent: __('Sent to Meta'), held: __('Kept as Meta has it'), skipped: __('Not sent'),
    template: __('Product with variants'), not_published: __('Not published'), out_of_shop: __('Not in the web shop'),
  }[s.state] || s.state
  // [label, already-escaped HTML]
  const rows = [[__('Status'), esc(state + (s.reason ? ' — ' + s.reason : ''))]]
  if (s.payload) {
    rows.push(
      [__('Price'), esc(s.payload.price)],
      [__('Stock'), esc(s.payload.availability)],
      [__('Shoppers see it'), s.payload.visibility === 'published' ? __('Yes') : __('No (hidden)')],
      [__('Product page'), `<a href="${esc(s.payload.link)}" target="_blank" rel="noopener">${esc(s.payload.link)}</a>`],
    )
  }
  if (s.last_send) {
    rows.push([__('Last send'), `<a href="/app/meta-catalog-publication/${encodeURIComponent(s.last_send.name)}">${esc(s.last_send.state)}</a>`
      + ` · ${esc(frappe.datetime.comment_when(s.last_send.creation))}${s.last_send.reason ? ' · ' + esc(s.last_send.reason) : ''}`])
  }
  if (s.meta) {
    rows.push(
      [__('Meta review'), esc((s.meta.review_status || '—') + (s.meta.visibility ? ' · ' + s.meta.visibility : ''))],
      [__('Channels'), esc(['facebook', 'instagram', 'whatsapp'].map((k) => `${k}: ${s.meta[k] || '—'}`).join(' · '))],
    )
    if ((s.meta.errors || []).length) {
      rows.push([__('Meta says'), esc(s.meta.errors.map((e) => e.description || e.message || JSON.stringify(e)).join('; '))])
    }
  }
  return `<table class="table table-sm small"><tbody>${rows.map(([label, html]) =>
    `<tr><td style="width: 30%">${esc(label)}</td><td>${html}</td></tr>`).join('')}</tbody></table>`
}

async function preview(frm) {
  const d = (await frappe.call({ method: 'doco_meta_catalog.sync.dry_run', args: { limit: 50 }, freeze: true, freeze_message: __('Reading every product…') })).message
  const sample = d.sample.map((row) => `<tr><td>${esc(row.id)}</td><td>${esc(row.title)}</td><td>${esc(row.price)}</td><td>${esc(row.availability)}</td><td><a href="${esc(row.link)}" target="_blank" rel="noopener">${__('page')}</a></td></tr>`).join('')
  const out = Object.entries(d.examples || {}).map(([kind, rows]) => `<p><b>${esc((KIND_LABEL[kind] || (() => kind))())} (${d.by_kind[kind]})</b><br>${rows.map((r) => esc(r.code)).join(', ')}</p>`).join('')
  new frappe.ui.Dialog({
    title: __('What a full send would do'),
    size: 'extra-large',
    fields: [{ fieldtype: 'HTML', options: `<p>${esc(__('{0} products would be sent; {1} are left out.', [d.eligible, d.skipped]))}</p>${out}
      <table class="table table-sm small"><thead><tr><th>${__('Code')}</th><th>${__('Title')}</th><th>${__('Price')}</th><th>${__('Stock')}</th><th></th></tr></thead><tbody>${sample}</tbody></table>` }],
  }).show()
}
