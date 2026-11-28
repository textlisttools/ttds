# TTDS: TikTok Shop × AliExpress dropshipping

A self-hosted tool that imports AliExpress products, prices them, turns TikTok Shop orders into AliExpress orders shipped straight to your buyer, gets tracking back to TikTok, and keeps an eye on supplier price and stock.

It has two ways of talking to TikTok, which you choose on the **Settings** page:

| | **Files mode** (default) | **API mode** |
|---|---|---|
| Needs TikTok API keys? | No | Yes (a TikTok Shop Partner Center app) |
| Listing products | You create the listing in Seller Center; TTDS gives you the title, description, photos, prices and **Seller SKUs** to copy | Automatic |
| New orders | You export orders from Seller Center and upload the file | Pulled every 10 minutes |
| Tracking | TTDS makes a file you upload with Seller Center's bulk shipping | Uploaded automatically |
| Price/stock changes | TTDS lists them and makes a file | Updated automatically |

The AliExpress side is automatic in both modes. AliExpress orders are placed when you click **Approve**, or automatically if you turn on **Automatic ordering**.

## Daily routine in files mode (about 5 minutes)

1. **Seller Center → Orders:** filter *To ship / Awaiting shipment*, click **Export**, and upload the file on the TTDS dashboard.
2. **Approve** the new orders. TTDS checks profit, then buys them on AliExpress. If an order says the address is hidden, copy it from the order in Seller Center into **Ship to** on the order page.
3. When suppliers ship (TTDS checks every hour), click **Download tracking file**. In Seller Center, use **Bulk ship / Batch shipping** to upload it, then click **I've uploaded it**.
4. If **Price & stock changes** shows anything, update those products in Seller Center and click **Done**.

**Seller SKUs matter:** when you create a listing, type the Seller SKU TTDS shows for each variant (like `TTDS-3-7`). That's how TTDS knows which AliExpress product an order is for. If you forget, the order is held and you can link it by hand on the order page. TTDS then remembers the link.

**If an upload is rejected:** if Seller Center won't accept TTDS's tracking or price file, download TikTok's own blank template from that same Seller Center screen. Choose it in the "TikTok's template (optional)" box, and TTDS fills it in. If that still fails, send the error message.

## Try it now (no API keys needed)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # leave MOCK_MODE=true
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with `admin` / `change-me`. Then:

1. **Settings:** click **Connect AliExpress**. In mock mode it connects instantly.
2. **Products:** paste any AliExpress link (for example `https://www.aliexpress.us/item/1005006123456789.html`) and click **Import**. Then click **I've listed it**.
3. **Dashboard:** click **Mock mode: download a sample export to try**, then upload that file under **Import new orders**.
4. Click **Approve** on an order.
5. After about a minute, click **Check tracking now**, and step 3 shows a tracking file to download.

Nothing in mock mode touches TikTok or AliExpress or spends money.

## Going live

Follow **[docs/API_KEYS.md](docs/API_KEYS.md)**. In short:

1. Host the app at a public `https://` address (Railway; the guide covers this).
2. Create an AliExpress Open Platform **Dropshipping** app and put its keys in your host's variables. TikTok keys are only needed for API mode.
3. Set `MOCK_MODE=false`, a real `DASHBOARD_PASSWORD` and `SESSION_SECRET`, and restart.
4. On the **Settings** page, click **Connect AliExpress**, then set your rules.
5. List one cheap product and take one real order through by hand before you rely on it.

## Your rules (Settings page)

Change these on the dashboard's **Settings** page. Changes apply immediately, with no restart. The values in `.env` are only the starting defaults; **Reset to defaults** goes back to them. API keys and passwords can only be set in `.env` or your host's environment variables, never on the page.

| Setting | Default | Meaning |
|---|---|---|
| `AUTO_ORDER` | `false` | `false`: you click **Approve** on each order. `true`: profitable orders are bought automatically. Held orders always wait for you. |
| `MARKUP_MULTIPLIER` / `MARKUP_FIXED` | 1.8 / 2.0 | Price = (cost + shipping) × 1.8 + $2, rounded up to .99 |
| `MIN_PROFIT` | 3.0 | Orders below this estimated profit (after `TIKTOK_FEE_RATE`) are **held** |
| `REQUIRE_LOCAL_WAREHOUSE` | `true` | Only import variants shipping from `SHIP_TO_COUNTRY` |
| `MAX_DELIVERY_DAYS` | 8 | Skip variants slower than this |
| `MAX_LISTED_STOCK` | 20 | Never show more than this many units on TikTok, and always keep a 2-unit buffer |

## Safety features

- **No order is ever bought twice:** an order that already has an AliExpress order id can't be placed again.
- **Live price check:** the supplier price is re-checked right before buying. If the profit has dropped below your minimum, the order is held instead of bought.
- **Holds:** orders containing items you didn't import here, or with too little AliExpress stock, are held.
- **Cancellations:** orders cancelled on TikTok before you approve them are marked cancelled automatically.
- **Login required:** the dashboard needs a login, and the app won't start in live mode with the default password.

## Project layout

```
app/
  clients/tiktok.py       TikTok Shop API (API mode)
  clients/aliexpress.py   AliExpress Dropshipping API (signing, OAuth, products, freight, orders, tracking)
  clients/mock.py         Fake versions of both for MOCK_MODE and tests
  services/products.py    import / publish / price & stock sync
  services/orders.py      order intake, profit check, AliExpress ordering, tracking upload
  services/pricing.py     markup, stock buffer, profit maths
  services/tiktok_files.py  files mode: orders export import, tracking & price/stock files
  services/sheets.py      reads/fills Excel and CSV files
  scheduler.py            background jobs
  main.py                 web dashboard + OAuth callbacks
tests/                    pytest suite (runs entirely in mock mode)
```

Run the tests with `pytest`.

## Limitations

- **Not yet tested against the live APIs.** The TikTok and AliExpress code follows their published API documentation but has only run against the mock clients. Expect a few field names or error codes to need adjusting on the first real run. The dashboard shows each platform's exact error message.
- **Files mode column names:** TTDS reads TikTok's files by column name (Order ID, Seller SKU, Quantity, Recipient and so on). Those columns were written from TikTok's published formats and haven't been checked against a real export from your shop yet. If an upload says a column is missing, the message lists the columns it found. Send that list to get the fix.
- **TikTok listing requirements:** some TikTok categories require extra attributes (brand, materials, certifications) or package dimensions. If **Publish** fails with a message about attributes, finish that listing in Seller Center, or tell a developer the error so it can be added.
- **AliExpress payment:** orders are created with "try to pay" using your saved AliExpress payment method. If AliExpress can't charge it automatically, the order sits as *Awaiting payment* on AliExpress until you pay it there.
- **Policies:** you are responsible for meeting TikTok Shop's seller policies, including ship-by deadlines, valid tracking and where products ship from. Keep `REQUIRE_LOCAL_WAREHOUSE=true` unless you've confirmed TikTok allows otherwise for your account.
