"""Web dashboard and OAuth callbacks.  Run with:  uvicorn app.main:app"""
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .clients.mock import MockTikTokClient
from .config import get_settings
from .db import Event, Order, Product, Variant, get_token, init_db, session
from .services import connections, orders, products

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


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
    return templates.TemplateResponse(request, name, {"s": get_settings(), "flash": flash, **ctx})


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
    s = get_settings()
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
    s = get_settings()
    with session() as db:
        all_orders = list(db.exec(select(Order).order_by(Order.created_at.desc())))
        counts = {}
        for o in all_orders:
            counts[o.status] = counts.get(o.status, 0) + 1
        shipped = [o for o in all_orders if o.status in ("placed", "shipped")]
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
    return render(request, "dashboard.html", stats=stats, counts=counts, action=action,
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
    if not get_settings().mock_mode:
        return back(request, "/", "Only available in MOCK_MODE.", "error")
    if not MockTikTokClient().simulate_order():
        return back(request, "/", "Publish a product to (mock) TikTok first.", "error")
    msg = scheduler.run_job("orders")
    return back(request, "/orders", "Simulated a TikTok sale. " + msg)


# ----------------------------------------------------------------- Orders --
@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, status: str = ""):
    s = get_settings()
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
    return render(request, "order.html", o=o, events=events,
                  at_risk=orders.at_risk(o, get_settings()))


@app.post("/orders/{oid}/approve")
def approve(request: Request, oid: int, force: bool = Form(False)):
    s = get_settings()
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
    s = get_settings()
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
        tt, _ = connections.tiktok(db, get_settings())
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
    s = get_settings()
    with session() as db:
        try:
            p = products.import_product(db, s, url, float(multiplier) if multiplier else None)
        except Exception as e:
            return back(request, "/products", f"Import failed: {e}", "error")
        return back(request, f"/products/{p.id}", "Imported as a draft. Check prices, then publish.")


@app.get("/products/{pid}", response_class=HTMLResponse)
def product_page(request: Request, pid: int):
    s = get_settings()
    with session() as db:
        p = db.get(Product, pid)
        if not p:
            return back(request, "/products", "Product not found.", "error")
        vs = products.variants_of(db, pid)
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
        if p.tiktok_product_id:
            return back(request, f"/products/{pid}",
                        "Already on TikTok — change markup in .env or edit on TikTok.", "error")
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
    s = get_settings()
    with session() as db:
        p = db.get(Product, pid)
        try:
            products.publish_product(db, s, p)
        except Exception as e:
            return back(request, f"/products/{pid}", f"TikTok rejected the listing: {e}", "error")
    return back(request, f"/products/{pid}", "Sent to TikTok. It goes live after TikTok's review.")


@app.post("/products/{pid}/sync")
def sync(request: Request, pid: int):
    s = get_settings()
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
        if p.tiktok_product_id:
            return back(request, f"/products/{pid}",
                        "It's live on TikTok — deactivate it in Seller Center first.", "error")
        for v in products.variants_of(db, pid):
            db.delete(v)
        db.delete(p)
        db.commit()
    return back(request, "/products", "Draft deleted.")


# -------------------------------------------------------- Settings / OAuth --
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session() as db:
        conn = {p: get_token(db, p) for p in ("tiktok", "aliexpress")}
    return render(request, "settings.html", conn=conn)


@app.get("/connect/{platform}")
def connect(request: Request, platform: str):
    if platform not in ("tiktok", "aliexpress"):
        return back(request, "/settings", "Unknown platform.", "error")
    s = get_settings()
    key = s.tiktok_app_key if platform == "tiktok" else s.aliexpress_app_key
    if not s.mock_mode and not key:
        return back(request, "/settings", f"Add your {platform} app key to .env first.", "error")
    state = secrets.token_urlsafe(16)
    request.session[f"oauth_{platform}"] = state
    return RedirectResponse(connections.oauth_client(platform, s).authorize_url(state), 303)


@app.get("/oauth/{platform}/callback")
def oauth_callback(request: Request, platform: str, code: str = "", state: str = "",
                   error: str = ""):
    s = get_settings()
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
