# TTDS: TikTok Shop × AliExpress dropshipping

A self-hosted tool that:

1. **Imports products:** paste an AliExpress link and it pulls the title, photos, variants, cost, shipping and stock, then prices the product with your markup.
2. **Lists them on TikTok Shop** through the official TikTok Shop API.
3. **Watches prices and stock:** every few hours it re-checks AliExpress and updates your TikTok price, or sets stock to 0 when the supplier runs out.
4. **Turns TikTok orders into AliExpress orders:** every 10 minutes it pulls new TikTok orders, works out your profit, and (after you click **Approve**) buys the item on AliExpress, shipped straight to your buyer.
5. **Uploads tracking:** when the AliExpress supplier ships, it sends the tracking number to TikTok and marks the order shipped.
6. **Warns you** about orders close to TikTok's ship-by deadline.

## Try it now (no API keys needed)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # leave MOCK_MODE=true
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with `admin` / `change-me`. Then:

1. **Settings:** click **Connect TikTok Shop** and **Connect AliExpress**. In mock mode these connect instantly.
2. **Products:** paste any AliExpress link (for example `https://www.aliexpress.us/item/1005006123456789.html`), click **Import**, then **Publish to TikTok Shop**.
3. **Dashboard:** click **Simulate a sale**, then **Approve** on the new order.
4. After about a minute, click **Check tracking now** and the order turns *shipped*.

Nothing in mock mode touches TikTok or AliExpress or spends money.

## Going live

Follow **[docs/API_KEYS.md](docs/API_KEYS.md)**. In short:

1. Host the app at a public `https://` address (Railway, Render or a VPS; the guide covers this).
2. Create a TikTok Shop Partner Center app and an AliExpress Open Platform app, and put their keys in `.env`.
3. Set `MOCK_MODE=false`, a real `DASHBOARD_PASSWORD` and `SESSION_SECRET`, and restart.
4. On the **Settings** page, click **Connect** for each platform.
5. Import one cheap product and walk one real order through by hand before you rely on it.

## Your rules (in `.env`)

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
  clients/tiktok.py       TikTok Shop API (signing, OAuth, products, orders, fulfilment)
  clients/aliexpress.py   AliExpress Dropshipping API (signing, OAuth, products, freight, orders, tracking)
  clients/mock.py         Fake versions of both for MOCK_MODE and tests
  services/products.py    import / publish / price & stock sync
  services/orders.py      order intake, profit check, AliExpress ordering, tracking upload
  services/pricing.py     markup, stock buffer, profit maths
  scheduler.py            background jobs
  main.py                 web dashboard + OAuth callbacks
tests/                    pytest suite (runs entirely in mock mode)
```

Run the tests with `pytest`.

## Limitations

- **Not yet tested against the live APIs.** The TikTok and AliExpress code follows their published API documentation but has only run against the mock clients. Expect a few field names or error codes to need adjusting on the first real run. The dashboard shows each platform's exact error message.
- **TikTok listing requirements:** some TikTok categories require extra attributes (brand, materials, certifications) or package dimensions. If **Publish** fails with a message about attributes, finish that listing in Seller Center, or tell a developer the error so it can be added.
- **AliExpress payment:** orders are created with "try to pay" using your saved AliExpress payment method. If AliExpress can't charge it automatically, the order sits as *Awaiting payment* on AliExpress until you pay it there.
- **Policies:** you are responsible for meeting TikTok Shop's seller policies, including ship-by deadlines, valid tracking and where products ship from. Keep `REQUIRE_LOCAL_WAREHOUSE=true` unless you've confirmed TikTok allows otherwise for your account.
