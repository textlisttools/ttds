"""Web dashboard and OAuth callbacks.  Run with:  uvicorn app.main:app"""
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .clients.mock import MockTikTokClient
from . import settings_store
from .config import get_settings
from .settings_store import current_settings
from .db import Event, Order, Product, Variant, get_token, init_db, session
from .services import connections, orders, products, tiktok_files
from .services.sheets import SheetError

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def _plaintext(html: str) -> str:
    import html as html_lib
    import re
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


templates.env.filters["plaintext"] = _plaintext


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.mock_mode and (s.dashboard_password in ("change-me", "") or
                            s.session_secret.startswith("change-this") or len(s.session_secret) < 16):
        raise RuntimeError("Set DASHBOARD_PASSWORD and SESSION_SECRET in .env before using real API keys.")
    init_db()
    sched = None if os.environ.get("TTDS_NO_SCHEDULER") else scheduler.start()
    yield
    if sched:
        sched.shutdown(wait=False)


settings = get_settings()
app = FastAPI(title="TTDS", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

PUBLIC = ("/login", "/static", "/healthz")


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not request.url.path.startswith(PUBLIC) and not request.session.get("user"):
        return RedirectResponse("/login", 303)
    return await call_next(request)


# Added after require_login so it wraps it (the session must exist before the login check).
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, 
                   https_only=settings.base_url.startswith("https"))


def render(request: Request, name: str, **ctx):
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"s": current_settings(), "flash": flash, **ctx})


def back(request: Request, url: str, message: str = "", level: str = "ok"):
    if message:
        request.session["flash"] = {"msg": message, "level": level}
    return RedirectResponse(url, 303)


# ------------------------------------------------------------------ Login --
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    s = current_settings()
    ok = hmac.compare_digest(username, s.dashboard_username) and \
        hmac.compare_digest(password, s.dashboard_password)
    if not ok:
        return back(request, "/login", "Wrong username or password.", "error")
    request.session["user"] = username
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# -------------------------------------------------------------- Dashboard --
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    s = current_settings()
    with session() as db:
        all_orders = list(db.exec(select(Order).order_by(Order.created_at.desc())))
        counts = {}
        for o in all_orders:
            counts[o.status] = counts.get(o.status, 0) + 1
        shipped = [o for o in all_orders if o.status in ("placed", "tracking_ready", "shipped")]
        stats = {
            "orders": len(all_orders),
            "revenue": sum(o.sale_total for o in shipped),
            "profit": sum(o.est_profit for o in shipped),
            "products": db.exec(select(func.count()).select_from(Product)).one(),
            "listed": db.exec(select(func.count()).select_from(Product)
                              .where(Product.status == "listed")).one(),
        }
        action = [o for o in all_orders if o.status in ("needs_approval", "held", "error")]
        risky = [o for o in all_orders if orders.at_risk(o, s)]
        events = list(db.exec(select(Event).order_by(Event.at.desc()).limit(25)))
        conn = {p: get_token(db, p) for p in ("tiktok", "aliexpress")}
        tracking = tiktok_files.tracking_orders(db) if s.tiktok_mode != "api" else []
        updates = tiktok_files.pending_updates(db) if s.tiktok_mode != "api" else []
    return render(request, "dashboard.html", tracking=tracking, updates=updates, stats=stats, counts=counts, action=action,
                  risky=risky, events=events, conn=conn, at_risk=lambda o: orders.at_risk(o, s))


@app.post("/jobs/{name}")
def run_job(request: Request, name: str):
    if name not in scheduler.JOBS:
        return back(request, "/", "Unknown job.", "error")
    msg = scheduler.run_job(name)
    return back(request, request.headers.get("referer", "/"), msg,
                "error" if "failed" in msg else "ok")


@app.post("/mock/order")
def mock_order(request: Request):
    if not current_settings().mock_mode:
        return back(request, "/", "Only available in MOCK_MODE.", "error")
    if not MockTikTokClient().simulate_order():
        return back(request, "/", "Publish a product to (mock) TikTok first.", "error")
    msg = scheduler.run_job("orders")
    return back(request, "/orders", "Simulated a TikTok sale. " + msg)


# ----------------------------------------------------------------- Orders --
@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, status: str = ""):
    s = current_settings()
    with session() as db:
        q = select(Order).order_by(Order.created_at.desc())
        if status:
            q = q.where(Order.status == status)
        rows = list(db.exec(q.limit(500)))
    return render(request, "orders.html", rows=rows, status=status,
                  at_risk=lambda o: orders.at_risk(o, s))


@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_page(request: Request, oid: int):
    with session() as db:
        o = db.get(Order, oid)
        if not o:
            return back(request, "/orders", "Order not found.", "error")
        events = list(db.exec(select(Event).where(Event.order_id == oid).order_by(Event.at.desc())))
        choices = [(v.id, f"{p.title[:50]} — {v.name} ({v.seller_sku or 'no SKU yet'})")
                   for p in db.exec(select(Product)) for v in products.variants_of(db, p.id)]
    return render(request, "order.html", o=o, events=events, choices=choices,
                  at_risk=orders.at_risk(o, current_settings()))


@app.post("/orders/{oid}/approve")
def approve(request: Request, oid: int, force: bool = Form(False)):
    s = current_settings()
    with session() as db:
        o = db.get(Order, oid)
        try:
            o = orders.place_order(db, s, o, force=force)
        except Exception as e:
            return back(request, f"/orders/{oid}", str(e), "error")
        msg = {"placed": "AliExpress order placed.",
               "held": f"Held: {o.hold_reason}. Use 'Approve anyway' to buy it regardless."}
        return back(request, f"/orders/{oid}", msg.get(o.status, o.error or o.status),
                    "ok" if o.status == "placed" else "error")


@app.post("/orders/{oid}/dismiss")
def dismiss(request: Request, oid: int):
    with session() as db:
        o = db.get(Order, oid)
        if o and not o.ae_order_ids:
            o.status = "cancelled"
            db.add(o)
            db.commit()
    return back(request, f"/orders/{oid}",
                "Marked as not fulfilling here. Remember to cancel it in TikTok Seller Center.")


@app.post("/orders/{oid}/tracking")
def manual_tracking(request: Request, oid: int, tracking_number: str = Form(...),
                    carrier: str = Form(""), provider_id: str = Form("")):
    s = current_settings()
    with session() as db:
        o = db.get(Order, oid)
        try:
            ok = orders.upload_tracking(db, s, o, tracking_number.strip(), carrier.strip(),
                                        provider_id.strip() or None)
        except Exception as e:
            return back(request, f"/orders/{oid}", f"TikTok rejected the tracking: {e}", "error")
    return back(request, f"/orders/{oid}", "Tracking uploaded." if ok else o.error,
                "ok" if ok else "error")


@app.get("/orders/{oid}/providers", response_class=HTMLResponse)
def providers(request: Request, oid: int):
    with session() as db:
        o = db.get(Order, oid)
        tt, _ = connections.tiktok(db, current_settings())
        rows = tt.get_shipping_providers(o.address.get("delivery_option_id", ""))
    return render(request, "providers.html", o=o, rows=rows)


# --------------------------------------------------------------- Products --
@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request):
    with session() as db:
        rows = list(db.exec(select(Product).order_by(Product.created_at.desc())))
        vcount = {p.id: products.variants_of(db, p.id) for p in rows}
    return render(request, "products.html", rows=rows, variants=vcount)


@app.post("/products/import")
def import_product(request: Request, url: str = Form(...), multiplier: str = Form("")):
    s = current_settings()
    with session() as db:
        try:
            p = products.import_product(db, s, url, float(multiplier) if multiplier else None)
        except Exception as e:
            return back(request, "/products", f"Import failed: {e}", "error")
        nxt = "publish" if s.tiktok_mode == "api" else "follow 'List it on TikTok' below"
        return back(request, f"/products/{p.id}", f"Imported as a draft. Check prices, then {nxt}.")


@app.get("/products/{pid}", response_class=HTMLResponse)
def product_page(request: Request, pid: int):
    s = current_settings()
    with session() as db:
        p = db.get(Product, pid)
        if not p:
            return back(request, "/products", "Product not found.", "error")
        vs = products.ensure_seller_skus(db, p) if s.tiktok_mode != "api" else products.variants_of(db, pid)
        events = list(db.exec(select(Event).where(Event.product_id == pid)
                              .order_by(Event.at.desc()).limit(30)))
    from .services.pricing import profit
    return render(request, "product.html", p=p, variants=vs, events=events,
                  profit=lambda v: profit(v.price, v.cost + v.shipping_cost, s))


@app.post("/products/{pid}/edit")
async def edit_product(request: Request, pid: int):
    form = await request.form()
    with session() as db:
        p = db.get(Product, pid)
        if p.tiktok_product_id or p.status in ("listed", "paused"):
            return back(request, f"/products/{pid}",
                        "Already on TikTok. Change the markup on the Settings page, or edit it on TikTok.",
                        "error")
        p.title = str(form.get("title", p.title))[:255]
        p.description = str(form.get("description", p.description))
        for v in products.variants_of(db, pid):
            if f"price_{v.id}" in form:
                try:
                    v.price = round(float(form[f"price_{v.id}"]), 2)
                except ValueError:
                    pass
            db.add(v)
        db.add(p)
        db.commit()
    return back(request, f"/products/{pid}", "Saved.")


@app.post("/products/{pid}/publish")
def publish(request: Request, pid: int):
    s = current_settings()
    if s.tiktok_mode != "api":
        return back(request, f"/products/{pid}", "In files mode, list it in Seller Center using the "
                    "steps below, then click 'I've listed it'.", "error")
    with session() as db:
        p = db.get(Product, pid)
        try:
            products.publish_product(db, s, p)
        except Exception as e:
            return back(request, f"/products/{pid}", f"TikTok rejected the listing: {e}", "error")
    return back(request, f"/products/{pid}", "Sent to TikTok. It goes live after TikTok's review.")


@app.post("/products/{pid}/sync")
def sync(request: Request, pid: int):
    s = current_settings()
    with session() as db:
        try:
            n = products.sync_product(db, s, db.get(Product, pid))
        except Exception as e:
            return back(request, f"/products/{pid}", f"Sync failed: {e}", "error")
    return back(request, f"/products/{pid}", f"Checked AliExpress: {n} change(s) pushed.")


@app.post("/products/{pid}/delete")
def delete(request: Request, pid: int):
    with session() as db:
        p = db.get(Product, pid)
        if p.tiktok_product_id or p.status in ("listed", "paused"):
            return back(request, f"/products/{pid}",
                        "It's live on TikTok — remove it in Seller Center, then 'Move back to draft' first.",
                        "error")
        for v in products.variants_of(db, pid):
            db.delete(v)
        db.delete(p)
        db.commit()
    return back(request, "/products", "Draft deleted.")


# -------------------------------------------------------- Settings / OAuth --
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = current_settings()
    with session() as db:
        conn = {p: get_token(db, p) for p in ("tiktok", "aliexpress")}
    env = get_settings()
    groups: dict[str, list] = {}
    for rule in settings_store.RULES:
        groups.setdefault(rule.group, []).append(
            (rule, settings_store.display_value(s, rule),
             getattr(s, rule.key) != getattr(env, rule.key)))
    return render(request, "settings.html", conn=conn, groups=groups,
                  errors=request.session.pop("settings_errors", None))


@app.post("/settings")
async def save_settings(request: Request):
    before = current_settings()
    form = await request.form()
    errors = settings_store.save(dict(form))
    if errors:
        request.session["settings_errors"] = errors
        return back(request, "/settings#rules", "Nothing was saved — please fix the highlighted values.",
                    "error")
    after = current_settings()
    scheduler.reschedule()
    msg = "Settings saved and applied."
    if (before.markup_multiplier, before.markup_fixed, before.max_listed_stock) != \
            (after.markup_multiplier, after.markup_fixed, after.max_listed_stock):
        msg += (" Your TikTok prices/stock update at the next price check — or click "
                "'Update all listings now' to do it immediately.")
    if after.auto_order and not before.auto_order:
        msg += " Automatic ordering is ON."
    with session() as db:
        from .db import log
        log(db, f"Settings changed (automatic ordering {'on' if after.auto_order else 'off'}, "
                f"markup ×{after.markup_multiplier} + ${after.markup_fixed}, "
                f"min profit ${after.min_profit})")
        db.commit()
    return back(request, "/settings#rules", msg)


@app.post("/settings/reset")
def reset_settings(request: Request):
    settings_store.reset()
    scheduler.reschedule()
    return back(request, "/settings#rules", "Settings reset to the defaults from .env.")


@app.get("/connect/{platform}")
def connect(request: Request, platform: str):
    if platform not in ("tiktok", "aliexpress"):
        return back(request, "/settings", "Unknown platform.", "error")
    s = current_settings()
    key = s.tiktok_app_key if platform == "tiktok" else s.aliexpress_app_key
    if not s.mock_mode and not key:
        return back(request, "/settings", f"Add your {platform} app key to .env first.", "error")
    state = secrets.token_urlsafe(16)
    request.session[f"oauth_{platform}"] = state
    return RedirectResponse(connections.oauth_client(platform, s).authorize_url(state), 303)


@app.get("/oauth/{platform}/callback")
def oauth_callback(request: Request, platform: str, code: str = "", state: str = "",
                   error: str = ""):
    s = current_settings()
    if platform not in ("tiktok", "aliexpress"):
        return back(request, "/settings", "Unknown platform.", "error")
    expected = request.session.pop(f"oauth_{platform}", None)
    if error or not code:
        return back(request, "/settings", f"Authorization cancelled: {error or 'no code'}", "error")
    # Some platforms don't echo `state` back; a *different* state is always rejected.
    if not expected or (state and not hmac.compare_digest(expected, state)):
        return back(request, "/settings", "Security check failed (state mismatch). Try again.", "error")
    with session() as db:
        try:
            fields = connections.oauth_client(platform, s).exchange_code(code)
            if platform == "tiktok":
                connections.finish_tiktok_connect(db, s, fields)
            else:
                connections.save_token(db, "aliexpress", fields)
        except Exception as e:
            return back(request, "/settings", f"Connecting {platform} failed: {e}", "error")
    return back(request, "/settings", f"{platform.title()} connected.")


# ------------------------------------------------------------ Files mode --
def download(data: bytes, filename: str) -> Response:
    media = ("text/csv" if filename.endswith(".csv") else
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def optional_template(upload: UploadFile | None):
    if upload is None or not upload.filename:
        return None
    data = await upload.read()
    return (data, upload.filename) if data else None


@app.post("/files/orders")
async def upload_orders(request: Request, file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return back(request, "/", "Choose the orders file you exported from Seller Center first.", "error")
    with session() as db:
        try:
            r = tiktok_files.import_orders(db, current_settings(), data, file.filename or "orders.xlsx")
        except SheetError as e:
            return back(request, "/", f"Couldn't read that file: {e}", "error")
    parts = [f"{r['new']} new order(s) to approve"]
    if r["held"]:
        parts.append(f"{r['held']} held for review")
    if r["cancelled"]:
        parts.append(f"{r['cancelled']} cancelled")
    if r["already"]:
        parts.append(f"{r['already']} already imported")
    if r["skipped"]:
        parts.append(f"{r['skipped']} skipped (not awaiting shipment)")
    return back(request, "/", "Orders file imported: " + ", ".join(parts) + ".")


@app.get("/files/sample-orders")
def sample_orders(request: Request):
    if not current_settings().mock_mode:
        return back(request, "/", "Only available in MOCK_MODE.", "error")
    with session() as db:
        data = tiktok_files.sample_export(db)
    if data.count(b"\n") < 2:
        return back(request, "/", "Mark a product as listed first (Products → I've listed it).", "error")
    return download(data, "sample-tiktok-orders.csv")


@app.post("/files/tracking")
async def tracking_download(request: Request, template: UploadFile | None = File(None)):
    tpl = await optional_template(template)
    with session() as db:
        if not tiktok_files.tracking_orders(db):
            return back(request, "/", "No tracking numbers waiting to be uploaded.", "error")
        try:
            data, name = tiktok_files.tracking_file(db, tpl)
        except SheetError as e:
            return back(request, "/", f"Couldn't fill that template: {e}", "error")
    return download(data, name)


@app.post("/files/tracking/done")
async def tracking_done(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("order_id") if str(x).isdigit()]
    with session() as db:
        n = tiktok_files.mark_tracking_uploaded(db, ids)
    return back(request, "/", f"{n} order(s) marked as shipped.")


@app.post("/files/prices")
async def prices_download(request: Request, template: UploadFile | None = File(None)):
    tpl = await optional_template(template)
    with session() as db:
        if not tiktok_files.pending_updates(db):
            return back(request, "/", "No price or stock changes to upload.", "error")
        try:
            data, name = tiktok_files.price_stock_file(db, tpl)
        except SheetError as e:
            return back(request, "/", f"Couldn't fill that template: {e}", "error")
    return download(data, name)


@app.post("/files/prices/done")
def prices_done(request: Request):
    with session() as db:
        n = tiktok_files.mark_updates_done(db)
    return back(request, "/", f"{n} price/stock update(s) marked as done.")


@app.get("/products/{pid}/images.zip")
def product_images(request: Request, pid: int):
    with session() as db:
        p = db.get(Product, pid)
        if not p:
            return back(request, "/products", "Product not found.", "error")
        data = tiktok_files.images_zip(p)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="product-{pid}-photos.zip"'})


@app.post("/products/{pid}/listed")
def product_listed(request: Request, pid: int, tiktok_product_id: str = Form("")):
    with session() as db:
        p = db.get(Product, pid)
        products.mark_listed(db, p, tiktok_product_id)
    return back(request, f"/products/{pid}", "Marked as listed. TTDS will now watch its price and stock.")


@app.post("/products/{pid}/unlist")
def product_unlist(request: Request, pid: int):
    with session() as db:
        p = db.get(Product, pid)
        p.status = "draft"
        if current_settings().tiktok_mode != "api":
            p.tiktok_product_id = None
        db.add(p)
        db.commit()
    return back(request, f"/products/{pid}", "Moved back to draft.")


@app.post("/orders/{oid}/link")
def order_link(request: Request, oid: int, index: int = Form(...), variant_id: int = Form(...)):
    with session() as db:
        try:
            orders.link_item(db, current_settings(), db.get(Order, oid), index, variant_id)
        except ValueError as e:
            return back(request, f"/orders/{oid}", str(e), "error")
    return back(request, f"/orders/{oid}", "Item linked.")


@app.post("/orders/{oid}/address")
async def order_address(request: Request, oid: int):
    form = await request.form()
    fields = {k: str(form.get(k, "")) for k in
              ("full_name", "phone", "address", "address2", "city", "province", "zip", "country")}
    with session() as db:
        try:
            orders.update_address(db, current_settings(), db.get(Order, oid), fields)
        except ValueError as e:
            return back(request, f"/orders/{oid}", str(e), "error")
    return back(request, f"/orders/{oid}", "Address saved.")
