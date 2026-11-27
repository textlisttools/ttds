# Getting your API keys and going live

Allow about an hour of your time tonight. Platform approvals can then take anywhere from a few hours to several business days.
Do the steps in this order, because steps 2 and 3 need the web address you get in step 1.

> **Keep your keys secret.** The App Secret values are like passwords. Put them only in your `.env` file or in your hosting provider's "environment variables" page. Never paste them into chat, email or GitHub.

---

## Step 1: Give the app a public https address

TikTok and AliExpress send you back to the app after you click **Connect**, so it needs a real `https://` address that stays online 24/7, since it checks for orders every 10 minutes.

**Easiest: Railway** (about $5/month)

1. Sign up at https://railway.app using your GitHub account.
2. Click **New Project → Deploy from GitHub repo** and choose `textlisttools/ttds`. Railway finds the `Dockerfile` automatically.
3. In the service, open **Settings → Networking → Generate Domain**. You'll get something like `https://ttds-production.up.railway.app`. **This is your BASE_URL.**
4. Open **Settings → Volumes → Add Volume** with mount path `/data`. This keeps your database when the app restarts.
5. Under **Variables**, add the values from `.env.example`. For now set:
   - `MOCK_MODE` = `true`
   - `BASE_URL` = your Railway address (no slash at the end)
   - `DASHBOARD_PASSWORD` = a strong password
   - `SESSION_SECRET` = a long random string (a password generator works; use 40+ characters)
6. Open your address. You should see the login page.

*(Render.com works the same way: create a Web Service from the repo with Docker and add a Disk mounted at `/data`.)*

---

## Step 2: TikTok Shop API keys

You need an active **TikTok Shop seller account** first (https://seller-us.tiktok.com for the US).

1. Go to the **TikTok Shop Partner Center**: https://partner.us.tiktokshop.com (US) or https://partner.tiktokshop.com (other regions), and sign up or log in.
2. When asked for a partner type, choose **App developer** (or "Developer").
3. Go to **App & Service → Create app**:
   - Choose **Custom app**. It's for your own shop only and gets approved faster than a public app.
   - Enter any app name, for example "TTDS".
   - **Redirect URL:** `https://YOUR-BASE-URL/oauth/tiktok/callback`
4. Under **API scopes / permissions**, request:
   - **Shop authorized information** (Authorization)
   - **Product**: read and manage products, plus image upload
   - **Order**: read orders
   - **Fulfillment**: mark packages shipped and upload tracking
   - **Logistics**: warehouses and shipping providers
   - **Customer / recipient address data (PII)**. *This one matters most:* without it TikTok hides the buyer's address and you can't ship. TikTok may ask you to fill in a data-security questionnaire to get it. Say the data is used only to fulfil the buyer's order and is stored securely.
5. Submit the app and wait for approval.
6. When it's approved, copy these from the app page:
   - **App Key** → `TIKTOK_APP_KEY`
   - **App Secret** → `TIKTOK_APP_SECRET`
   - **Service ID** → `TIKTOK_SERVICE_ID`. It may be shown in the authorization link, which looks like `.../open/authorize?service_id=1234567890`.

---

## Step 3: AliExpress API keys

You need a normal **AliExpress buyer account** with a **saved card or PayPal**, since that's how dropship orders are paid. Joining the AliExpress Dropshipping Center (https://ds.aliexpress.com) with the same account is also recommended.

1. Go to the **AliExpress Open Platform**: https://openservice.aliexpress.com, sign in with your AliExpress account, and register as a developer. Choose **Individual** if you don't have a company.
2. Go to **App Management → Create App** and pick the **Drop Shipping** app category. This unlocks the `aliexpress.ds.*` APIs the tool uses.
3. Set the **Callback URL** to `https://YOUR-BASE-URL/oauth/aliexpress/callback`.
4. Submit it. Approval usually takes 1–5 business days.
5. When it's approved, copy:
   - **App Key** → `ALIEXPRESS_APP_KEY`
   - **App Secret** → `ALIEXPRESS_APP_SECRET`
6. If the app page has an **API permissions** list, make sure these are enabled: product get (`aliexpress.ds.product.get`), freight query (`aliexpress.ds.freight.query`), order create (`aliexpress.ds.order.create`), order get (`aliexpress.trade.ds.order.get`) and order tracking (`aliexpress.ds.order.tracking.get`).

---

## Step 4: Switch to live mode

1. In your host's Variables page (or `.env` if running locally), set:

   ```
   MOCK_MODE=false
   TIKTOK_APP_KEY=...
   TIKTOK_APP_SECRET=...
   TIKTOK_SERVICE_ID=...
   ALIEXPRESS_APP_KEY=...
   ALIEXPRESS_APP_SECRET=...
   ```

   You'll set your markup, minimum profit and TikTok fee rate on the app's **Settings** page in step 4, so you don't need to add them here.
2. Restart or redeploy. The app refuses to start in live mode if the password or session secret is still the default.
3. Log in, go to **Settings**, and click **Connect TikTok Shop**. Approve on TikTok's page, and you'll come back showing "✓ Connected — *your shop name*" with a warehouse id.
4. Click **Connect AliExpress**, approve, and you'll come back connected.
5. Further down the same page, under **Your rules**, set your markup, minimum profit and TikTok fee rate (check your Seller Center fee page), then click **Save settings**.

Tokens refresh automatically. If one ever expires (for example after a long time offline), just click **Reconnect**.

---

## Step 5: First real test (do this before going hands-off)

1. **Products:** import one cheap item that ships from your country. Check the prices and profit column, then **Publish**. TikTok reviews new listings, which can take from minutes to a day.
2. When it's live, watch for the first real order. It appears on the Dashboard as **needs approval**.
3. Check the profit, then click **Approve**. Then:
   - open the AliExpress order link on the order page and confirm the address and that it's **paid**. If it says *Awaiting payment*, pay it there.
4. When the supplier ships, tracking uploads to TikTok automatically (checked hourly). You can also enter it by hand on the order page.

If anything fails, the exact message from TikTok or AliExpress appears in red on the page and in the Activity log. Copy that message to a developer (or to me) to adjust.

Once a handful of orders have gone through cleanly, you can tick **Automatic ordering** on the Settings page. Orders that fail a rule (low profit, unknown item, low stock) will still wait for you.

---

## Troubleshooting

| Message | Fix |
|---|---|
| "redirect_uri mismatch" / "invalid redirect" | The callback URL registered with TikTok/AliExpress must exactly match `BASE_URL` + `/oauth/.../callback`. |
| "Security check failed (state mismatch)" | Start the Connect click from the Settings page in the same browser, and don't refresh during authorization. |
| TikTok "invalid signature" | Check that `TIKTOK_APP_SECRET` has no spaces. |
| AliExpress "IncompleteSignature" / "InvalidApiPath" | Check the secret, and that your app is the **Drop Shipping** type with the API permission enabled. |
| Buyer name/address is blank or `***` | Your TikTok app doesn't have the recipient address (PII) permission yet (Step 2.4). |
| Publish fails with "attribute" or "brand" errors | That category needs extra details. Finish the listing in TikTok Seller Center, or send me the message. |
| "No variant passed your rules" | The item doesn't ship from your country fast enough. Pick a supplier with local stock, or loosen `MAX_DELIVERY_DAYS` / `REQUIRE_LOCAL_WAREHOUSE`. |
