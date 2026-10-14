# Bridge plans: AI included (Bridge Cloud)

People can use Bridge without their own OpenAI key. They start a free trial in
**Settings › Plan & AI**, then subscribe to **Bridge Plus** or buy credit. The pieces:

* **Bridge Cloud** (`cloud/`) is a small server on Fly.io. It holds your OpenAI key, each
  account's credit, and forwards the app's AI requests to OpenAI, charging what OpenAI says
  each one cost. It never stores or logs what people ask or what the AI answers.
* **Lemon Squeezy** takes the payments (cards, PayPal, sales tax and VAT, invoices, refunds).
  Bridge Cloud only listens to its webhooks.
* **The app** sends AI requests to Bridge Cloud instead of OpenAI when someone is on a plan.
  Voice conversations still go straight to OpenAI; Bridge Cloud only opens them and charges
  by the minute.

## Prices (change them in `cloud/bridge_cloud/config.py`)

| What | Price | AI credit | Notes |
|---|---|---|---|
| Free trial | free | $0.50 | Once per new Mac (at most 3 a day from one address) |
| Bridge Plus | $12 / month | $6 a month | Unused credit carries over |
| Top-up | $5 | $3.50 | One-off |
| Top-up | $20 | $15 | One-off |

AI credit is measured in OpenAI's own prices: a $6 credit pays for $6 of OpenAI usage. The
difference pays Lemon Squeezy's fee (about 5% + 50¢), the server and you. Voice costs
$0.10 a minute (gpt-realtime-mini) or $0.30 (gpt-realtime-2.1), set in `voice_minute`.

## Once: Lemon Squeezy (about an hour, plus their review)

1. Sign up at <https://lemonsqueezy.com> and create a store. To take real payments they
   verify your identity and look at your product; you need a privacy policy and terms.
2. Make three products (Store › Products):
   * **Bridge Plus**: subscription, $12 every month. Turn on **Generate license keys**
     (that's how people use their plan on another Mac).
   * **$5 of AI credit**: single payment, $5.
   * **$20 of AI credit**: single payment, $20.

   Note each product's **variant id**: open the product, then the variant's ⋯ menu ›
   Copy ID.
3. Make an API key: Settings › API › + (name it "Bridge Cloud").
4. Note your **store id**: Settings › Stores (the number next to your store).
5. Add a webhook: Settings › Webhooks › +
   * URL: `https://bridge-cloud.fly.dev/v1/webhooks/lemonsqueezy` (your app name)
   * Signing secret: a long random string (keep it for step 4 below), for example from
     `openssl rand -hex 32`
   * Events: `order_created`, `order_refunded`, `subscription_created`,
     `subscription_updated`, `subscription_cancelled`, `subscription_resumed`,
     `subscription_expired`, `subscription_paused`, `subscription_unpaused`,
     `subscription_payment_success`, `subscription_payment_refunded`, `license_key_created`.

## Once: Fly.io (about 20 minutes)

From the repository root:

1. Sign in: `fly auth login`
2. Create the app (pick another name if it's taken, then change `app` and `PUBLIC_URL` in
   `cloud/fly.toml`, and `cloud_url` in `app/config/settings.py`):

   ```bash
   fly apps create bridge-cloud
   ```

3. A disk for the accounts and credit (1 GB is plenty; Fly snapshots it daily):

   ```bash
   fly volumes create bridge_data --size 1 --region iad --config cloud/fly.toml
   ```

4. The secrets. Type them in yourself; they live only on Fly:

   ```bash
   fly secrets set --config cloud/fly.toml \
     OPENAI_API_KEY=sk-... \
     LEMONSQUEEZY_API_KEY=... LEMONSQUEEZY_STORE_ID=... LEMONSQUEEZY_WEBHOOK_SECRET=... \
     PLUS_VARIANT_ID=... TOPUP_SMALL_VARIANT_ID=... TOPUP_LARGE_VARIANT_ID=... \
     ACCEPT_TEST_PAYMENTS=true
   ```

   Use a **separate OpenAI project key** for Bridge Cloud, with a monthly spending limit set
   in OpenAI's dashboard, so a bug can never run up a large bill.
5. Deploy:

   ```bash
   fly deploy --config cloud/fly.toml --dockerfile cloud/Dockerfile
   ```

   Check it: `curl https://bridge-cloud.fly.dev/health` answers `{"ok":true}`.

Bridge Cloud runs on **one** machine on purpose (the ledger is SQLite on the volume). Don't
scale it to several machines; a bigger single machine is the way to grow.

## Try it end to end with test payments

1. In Lemon Squeezy, switch on **Test mode** (bottom left).
2. In Bridge: Settings › Plan & AI › **Start free trial**. Ask something; the credit goes down.
3. **Upgrade to Plus**: pay with the test card `4242 4242 4242 4242`, any future date, any
   CVC. Within a few seconds the plan says Bridge Plus and the credit goes up by $6.
4. Try **Add $5**, **Manage subscription**, and the license key from the test receipt on a
   second Mac (or after **Sign this Mac out**).
5. When everything works: `fly secrets set ACCEPT_TEST_PAYMENTS=false --config cloud/fly.toml`
   and switch Lemon Squeezy to live mode.

## Before you charge real people

* **Privacy policy and terms.** People's questions (and anything Bridge reads for them) pass
  through your server to OpenAI. Say so, and say that nothing is stored.
* **OpenAI's usage policies** apply to what your customers do with your key; you're
  responsible for it.
* **Signed, notarized app** (docs/releasing.md): paying customers expect it to open
  without warnings.
* **Watch the first weeks**: compare Lemon Squeezy's income with OpenAI's bill, and adjust
  the credit per plan if needed.

## How it's kept safe

* The OpenAI key never leaves the server; Macs only get a random account token (stored in the
  Mac's Keychain, and only as a hash on the server).
* Webhooks are checked with Lemon Squeezy's signature, and each payment is credited once even
  if the webhook arrives twice. Test-mode payments are ignored unless you allow them.
* Every request is checked against the account's credit first, only priced models are allowed,
  and each account gets at most 90 requests a minute.
* Request and answer contents are never written to disk or logs.
