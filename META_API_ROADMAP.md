# Meta API Roadmap — Marco's shops

What's worth building from the Meta Graph API for **own-shops-first** (docomexico repair+accessories,
mumulenceria lingerie), MX, WhatsApp-first. One Meta App `erpnext connector` (2082376078930469);
one broad System User token already holds every scope below unless noted.

## Strategic reality (read first)

- **Almost everything ships NOW** against Marco's own assets on scopes already held.
- **Business Verification (pending, gov papers) gates ONLY**: catalog >1000 items, WhatsApp/IG commerce
  item-review (what makes catalog products *sendable*/shoppable), FB/IG Shop + product-tagging. It does
  **NOT** block any Phase 1/2 task.
- **App Review (Advanced Access)** needed to act on **non-tester** customers for `pages_messaging`,
  IG messaging/comments, and Lead Ads at scale → submit in parallel (Phase 0).
- **MX-ineligible / dead-on-arrival — do NOT build**: WhatsApp Payments, Commerce Order Management,
  `commerce_account_read_reports`, native FB/IG/WhatsApp checkout. Keep the **off-Meta Mercado Pago link**
  via a CTA-URL button.
- New code lands in `doco_marketing` (campaigns/inbox/dispatch) + `doco_meta_catalog` (Meta token plumbing,
  sync extensions, CAPI emitter, webhook capture). Meta-HTTP duplicated per the no-cross-vertical-import
  rule; vertical-neutral so mumulenceria inherits it.

## Themes
1. **WhatsApp transactional + interactive ops** — utility templates on ERPNext events + interactive menus (daily driver).
2. **Catalog-driven ads + server-side attribution** — CAPI + CTWA `ctwa_clid` loop (the reason the stack exists).
3. **Lead capture into CRM** — Lead Ads, comment→DM, ref-tracked deep links → fcrm.
4. **Unified inbox UX** — Messenger/IG profile menus, quick replies, handover.
5. **Catalog extensions + promos** — product sets, sale_price, diagnostics.

## Task queue

| ID | Task | Val | Eff | Blocked |
|----|------|-----|-----|---------|
| MA-1 ✅ | WhatsApp **utility templates wired to ERPNext events** — SHIPPED: fcrm review queue (supervised/auto) + per-shop status→template config map (taller `57de18a1`, doco_marketing `860dc79`) | high | M | done |
| MA-2 ✅ | **Interactive reply-buttons + list menu + CTA-URL** (Mercado Pago link) on inbound — SHIPPED (`6eb2096`): send_reply_buttons/send_list_menu/send_menu + namespaced inbound menu routing (pay→cta_url MP link), gated `inbound_menu_enabled` | high | S | done |
| MA-3 ✅ | **CAPI Purchase/Lead emitter** from ERPNext Sales Invoice / fcrm lead (separate dataset token) — SHIPPED (`e96399d`): capi.py, SHA256-hashed user_data, dedup by doc name, async+guarded, gated capi_enabled | high | M | done |
| MA-4 ⚠️ | Capture **`ctwa_clid`** on inbound WA webhook → fire messaging-channel conversion (CTWA→sale loop) — LOOP SHIPPED (`1336788`): Meta CTWA Click store + Purchase→business_messaging attribution, all tested. CAPTURE DORMANT: frappe_whatsapp (baked-only) drops the inbound referral → ctwa_clid custom field unpopulated; needs a frappe_whatsapp webhook change/PR to fill it | high | M | frappe_whatsapp referral |
| MA-5 ✅ | **sale_price + effective_date** on catalog sync (self-expiring promos) — SHIPPED: mirrors `sf._sale_prices` (exact Pricing-Rule discount) + ISO8601 effective-date window from active rule bounds (omitted when any active rule is open-ended; daily reconcile is the backstop) | high | S | done |
| MA-6 ✅ | **ref-tracked wa.me / m.me deep links** with attribution — SHIPPED (`6eb2096`): build_wa_link/build_mme_link ([ref:CODE] in wa.me prefill / m.me ?ref=) + inbound text-ref → CRM Touchpoint(deeplink_click), gated `deeplink_capture_enabled`. Messenger m.me ref→touchpoint capture = small follow-up (ref already lands in the messenger audit row) | high | S | mostly done |
| MA-7 ✅ | **Messenger Profile** (ice breakers + persistent menu + get started) — SHIPPED (doco_marketing `e078e75`): set/get_messenger_profile + tap-router (Get Started/menu→reply), gated menu_enabled | high | S | done |
| MA-8 ✅ | Quick replies + sender actions on Messenger — SHIPPED (`e078e75`): send_quick_replies/send_sender_action/send_text on the page-token path | med | S | done |
| MA-9 ✅ | **Product Sets** (auto-curated collections by item_group/brand/availability) — SHIPPED (`32d338e`): Meta Product Set doctype + sync_product_sets (create/update idempotent) + autoseed; product_type=item_group on payload | high | M | done |
| MA-10 ✅ | **Catalog Diagnostics** read (per-item review_status + per-channel capability) — SHIPPED (`7f10ada`): run_diagnostics (paged) + summary + actionable snapshot in Meta Catalog Diagnostic + item_diagnostic | high | M | done |
| MA-11 ✅ | **Endpoint-free WhatsApp Flow** for repair intake → fcrm — SHIPPED (`0713b31`): flow completion (content_type=flow) → CRM Lead, lenient field map, gated intake_enabled/intake_flow_id | high | M | done |
| MA-12 ✅ | Inbound WA **media capture** onto Repair Order (cracked-screen photo) — SHIPPED (`0713b31`): inbound image/video re-attached to sender's open RO + note, by doctype name, gated media_capture_enabled | med | S | done |
| MA-13 | **Advantage+ catalog retargeting** / dynamic product ads | high | L | none |
| MA-14 | **CTWA campaign** create/manage wrapper | high | L | none |
| MA-15 | Ads Insights mini-dashboard (true cost-per-sale) | med | S | none |
| MA-16 | FB/IG **Lead Ads** real-time retrieval → fcrm Lead | high | M | app_review |
| MA-17 | **Comment → private DM** auto-funnel (FB + IG) | high | M | app_review |
| MA-18 | **InstagramProvider**: IG DM inbound + 24h reply into inbox | high | M | app_review |
| MA-19 | Messenger/IG **handover protocol** (bot ↔ human) | high | M | app_review |
| MA-20 | WA **marketing templates**: product-carousel / LTO / coupon | high | M | opt-in step |
| MA-21 | **Custom Audience + Lookalike** from ERPNext customers | med | M | ToS/consent |
| MA-22 | WA template/messaging **analytics + quality-rating alert** | med | M | none |
| MA-23 ◑ | FB Page + IG **content publishing** Social Post module — **Phase A (FB)+C (AI/recurring/insights) SHIPPED** lab-verified+audited (doco_marketing `2532bff`…`ecf07ef`, crm `c9da5006`…`888ebd87`), NOT on prod; **Phase B (IG) blocked on app_review**; **Phase D multistore + big features specced** → `doco_marketing/docs/SOCIAL_HANDOFF.md` | high | L | IG=app_review |
| MA-24 | **Social multistore** (Phase D): `Social Shop` doctype (branch) + per-shop accounts/voice/locale/warehouse + User-Permission scoping (employees→shop, mgr→all) + cross-branch dashboard. Brand=site, branch=Social Shop. SaaS-grade 50+ | high | L | none |
| MA-25 | **Cross-promotion + reporting** (Phase E/F): Social Campaign (fan-out to branches) + branch shoutout + Social Template library; per-shop leaderboard + roll-up + export + scheduled email digests | med | M | MA-24 |
| MA-26 | **Local SEO / Google Business Profile** — SEPARATE app `gbp_connector` (Google API, not Meta): per-branch GBP posts + reviews/Q&A inbox + hours/NAP; local landing pages follow-on | high | L | google_api + verification |

## Phased plan

- **Phase 0 — gates + plumbing (parallel, do first):** submit App Review (pages_messaging + Human Agent + IG/leadgen);
  chase Business Verification; **rotate the exposed catalog token**; mint a separate dataset-scoped CAPI token;
  Graph-Explorer pass to ground product_set filters + capability enums + current API version.
- **Phase 1 — WhatsApp daily-ops (ungated, highest ROI):** MA-1, MA-2, MA-5, MA-6, MA-12.
- **Phase 2 — attribution + ads spine:** MA-3, MA-4, MA-9, MA-10, MA-15.
- **Phase 3 — inbox UX + intake:** MA-7, MA-8, MA-11, MA-18, MA-19.
- **Phase 4 — acquisition + lead capture (post App Review):** MA-13, MA-14, MA-16, MA-17, MA-21.
- **Phase 5 — promo engine + governance + content:** MA-20, MA-22, MA-23.

**Quick wins (buildable today):** MA-1, MA-2, MA-5, MA-6, MA-7, MA-8, MA-9, MA-10, MA-11, MA-12, MA-15.

---

## MA-23 — Social Publishing module (epic detail)

**Goal:** create/schedule/publish + track FB Page & Instagram organic content (feed/Reels/Stories) from
the ERP, on a content calendar, **drafted from live inventory**, with deep-link attribution back to CRM.
Closes the only Meta surface we don't touch (organic publishing); the messaging/commerce/attribution
surfaces are MA-1…12. Compares to Buffer/Hootsuite/Meta Business Suite — but ERP-native.

**Differentiator (why us, not Buffer):** post tied to live stock → auto-draft from in-stock items/promos +
inject a `[ref:POST-x]` deep link (reuse MA-6 `deeplinks.py`) → resulting WhatsApp/Messenger leads
attributed via CRM Touchpoint. Standalone schedulers can't see inventory or close the lead loop.

**Home:** `doco_marketing` (marketing backend, surfaced in fcrm). Reuse `Messenger Settings.page_id/
page_access_token` (FB) + `Meta Catalog Settings` token/version. Provider-registry pattern like
`doco_marketing/services/dispatch/messenger.py`. Vertical-neutral (mumulenceria inherits).

### Doctypes
| Doctype | Key fields |
|---|---|
| **Social Post** | channel, status (Draft/Scheduled/Publishing/Published/Failed/Cancelado), caption, link, scheduled_time, published_time, meta_post_id, permalink, ref_code, source (manual/auto-item), error, attempts, approver |
| **Social Post Media** (child) | file/url, type (image/video), seq, ig_container_id |
| **Social Post Item** (child) | item (Link Item) — inventory-driven drafting |
| **Social Post Insight** (child) | metric, value, pulled_at (reach/impr/likes/comments/shares/saves/video_views) |
| **Social Settings** (Single) | fb_page_id, fb_page_token (or reuse Messenger), ig_business_id, default_channel, auto_draft rules, insights_enabled |

### Graph API + gotchas
- **FB Page:** `POST /{page-id}/feed` (message+link), `/photos`, Reels via `/video_reels`. **Native scheduling**
  = `scheduled_publish_time` (10min–6mo) + `published=false`. Works on the **current Page token, no app review**.
- **IG:** `POST /{ig-user-id}/media` (image_url/video_url/caption, media_type=REELS/STORIES, carousel=children)
  → `POST /{ig-user-id}/media_publish`. **No native scheduling → our cron publishes at due time.** Video
  container is async (poll `status_code`).
- **🔴 Constraint:** IG requires a **public HTTPS media URL** (Meta fetches it). Signed/B2-private URLs fail
  (bit us before — `signed_file_url`). Media must serve from a public file URL.
- **Insights:** `GET /{post-id}/insights`, `GET /{ig-media-id}/insights`.
- **Limits:** IG 50 posts/24h; long-lived Page token refresh.

### Scheduler (reuse existing cron infra)
- `social_publish.run_due` (`*/5`): IG due-time publish + FB native-schedule handoff + retry/backoff
  (mirror `whatsapp_send_review` reliability sweeps).
- `social_insights.pull_daily` (daily): refresh insights for posts published in last N days.

### UI (fcrm SPA, doco-dev)
- **Content Calendar** page (month/week grid, status colors, drag-to-reschedule).
- **Composer**: channel pick + caption + media upload + per-channel preview + schedule + "draft from items".
- Post detail w/ permalink + insights mini-card. Nav item **"Social"**.

### Stories
| # | Story | App-review? | Eff |
|---|------|-----|-----|
| S1 | Social Settings + provider registry + FB publisher scaffold (token reuse) | no | S |
| S2 | Social Post + media/item children + Draft/Schedule lifecycle | no | M |
| S3 | **FB Page publish** (text/link/photo + native scheduled_publish_time) + status sync | no | M |
| S4 | Scheduler `run_due` + retry/backoff (FB handoff + IG due-time) | no | S |
| S5 | IG single-image publish (container→publish, public-URL media, status poll) | **yes** (`instagram_content_publish`) | M |
| S6 | IG carousel + Reels + Stories | yes | M |
| S7 | Insights pull (FB+IG) + Social Post Insight + mini-dashboard | partial | M |
| S8 | fcrm Content Calendar + composer + preview | no | L |
| S9 | **Inventory-driven auto-draft** (in-stock items/promos) + `[ref:]` deep-link → attribution loop | no | M |
| S10 | Optional supervised approval gate (reuse review-queue pattern) | no | S |

### Phasing
- **A — FB MVP (ungated, ships fast):** S1+S2+S3+S4 + minimal calendar → schedule/publish FB Page posts today.
- **B — Instagram:** S5+S6 + public-media plumbing (after `instagram_content_publish` review + IG Business acct on Page).
- **C — leverage:** S7 insights + S8 full calendar + **S9 (differentiator)** + S10 approval.

### Risks / deps
- Public media URL for IG (biggest; needs public file serving, not B2-signed).
- App review for IG + `pages_manage_posts` scope (FB Page posting likely ok on current token).
- IG no native scheduling (our cron owns timing) + 50/day cap; Page token longevity/refresh.

**Size:** ~comparable to the MA-1…12 saga in aggregate. **Phase A alone is small (~1–2 stories) and unblocked** —
the fast win; IG (Phase B) is the app-review-gated part.
