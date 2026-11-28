"""Files mode: work with TikTok Seller Center spreadsheets instead of the API.

* import_orders      - read the Orders export you download from Seller Center
* tracking_file      - tracking numbers to upload with Seller Center's bulk shipping
* price_stock_file   - price/stock changes to upload with Seller Center's bulk edit
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlmodel import Session, select

from ..config import Settings
from ..db import Order, Product, Variant, log, utcnow
from . import orders as order_svc
from .pricing import COUNTRY_NAMES
from .sheets import SheetError, fill_template, find_col, read_table, simple_csv, simple_xlsx

# ----------------------------------------------------------------- Orders --
ORDER_KEY = ["Order ID", "Order Number"]
ORDER_COLS = {
    "order_id": ORDER_KEY,
    "status": ["Order Status"],
    "sku_id": ["SKU ID"],
    "seller_sku": ["Seller SKU"],
    "product": ["Product Name"],
    "variation": ["Variation", "SKU Name"],
    "qty": ["Quantity", "Qty"],
    "unit_price": ["SKU Unit Original Price", "Unit Price", "SKU Price"],
    "before": ["SKU Subtotal Before Discount"],
    "seller_discount": ["SKU Seller Discount"],
    "after": ["SKU Subtotal After Discount"],
    "shipping_fee": ["Shipping Fee After Discount"],
    "created": ["Created Time", "Order Created Time"],
    "paid": ["Paid Time"],
    "deadline": ["Ship By", "Shipping Due Time", "Ship By Time", "Latest Ship Time", "Ship By Date"],
    "recipient": ["Recipient", "Recipient Name", "Buyer Name"],
    "phone": ["Phone #", "Phone Number", "Phone", "Recipient Phone"],
    "zip": ["Zipcode", "Zip Code", "Postal Code", "Zip"],
    "country": ["Country", "Country/Region"],
    "state": ["State", "Province"],
    "city": ["City"],
    "address1": ["Detail Address", "Address Line 1", "Street Address", "Address"],
    "address2": ["Additional address information", "Address Line 2"],
}
AWAITING = {"awaitingshipment", "toship", "tobeshipped", "awaitingfulfillment", "readytoship"}


def _num(v) -> float:
    m = re.search(r"-?[\d,]*\.?\d+", str(v or ""))
    return float(m.group().replace(",", "")) if m else 0.0


def _date(v) -> Optional[datetime]:
    v = str(v or "").strip()
    if not v:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            pass
    return None


def _country(v: str, default: str) -> str:
    v = (v or "").strip().upper()
    if len(v) == 2:
        return v
    for code, names in COUNTRY_NAMES.items():
        if v in names:
            return code
    return default


def _phone(v: str) -> str:
    digits = re.sub(r"\D", "", v or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def import_orders(db: Session, s: Settings, data: bytes, filename: str) -> dict:
    """Read a Seller Center Orders export. Returns counts for the summary message."""
    headers, rows = read_table(data, filename, ORDER_KEY)
    col = {k: find_col(headers, a) for k, a in ORDER_COLS.items()}
    if col["qty"] is None or not any(col[k] is not None for k in ("seller_sku", "sku_id", "product")):
        raise SheetError("This doesn't look like a TikTok Orders export (no Quantity / Seller SKU columns). "
                         "Columns found: " + ", ".join(h for h in headers if h))

    def get(r, k):
        i = col[k]
        return r[i].strip() if i is not None and i < len(r) else ""

    groups: dict[str, list] = {}
    for r in rows:
        oid = get(r, "order_id")
        if oid:
            groups.setdefault(oid, []).append(r)

    summary = {"new": 0, "held": 0, "cancelled": 0, "skipped": 0, "already": 0}
    for oid, rs in groups.items():
        first = rs[0]
        status = re.sub(r"[^a-z]", "", get(first, "status").lower())
        existing = db.exec(select(Order).where(Order.tiktok_order_id == oid)).first()

        if "cancel" in status:
            if existing and existing.status in order_svc.OPEN:
                existing.status, existing.updated_at = "cancelled", utcnow()
                db.add(existing)
                log(db, f"TikTok order {oid} was cancelled — nothing ordered.", order_id=existing.id)
                summary["cancelled"] += 1
            elif existing and existing.ae_order_ids and existing.status not in ("cancelled",):
                existing.error = ("Cancelled on TikTok after the AliExpress order was placed — "
                                  "cancel AliExpress order(s) " + ", ".join(existing.ae_order_ids) + " if you can.")
                db.add(existing)
                log(db, f"TikTok order {oid}: {existing.error}", "warning", order_id=existing.id)
            continue
        if existing:
            summary["already"] += 1
            continue
        if status and status not in AWAITING:
            summary["skipped"] += 1
            continue

        items, total, shipping = [], 0.0, 0.0
        for r in rs:
            qty = int(_num(get(r, "qty")) or 1)
            if get(r, "before"):
                line = _num(get(r, "before")) - abs(_num(get(r, "seller_discount")))
            elif get(r, "after"):
                line = _num(get(r, "after"))
            else:
                line = _num(get(r, "unit_price")) * qty
            shipping = max(shipping, _num(get(r, "shipping_fee")))
            total += line
            item = {"tiktok_sku_id": get(r, "sku_id"), "seller_sku": get(r, "seller_sku"),
                    "title": f"{get(r, 'product')} {get(r, 'variation')}".strip()[:120] or get(r, "seller_sku"),
                    "quantity": qty, "sale_price": round(line / qty, 2), "tiktok_line_ids": []}
            v = order_svc._find_variant(db, item)
            item["variant_id"] = v.id if v else None
            items.append(item)

        address = {
            "full_name": get(first, "recipient"), "phone": _phone(get(first, "phone")),
            "address": get(first, "address1"), "address2": get(first, "address2"),
            "city": get(first, "city"), "province": get(first, "state"), "zip": get(first, "zip"),
            "country": _country(get(first, "country"), s.ship_to_country),
        }
        start = _date(get(first, "paid")) or _date(get(first, "created")) or utcnow()
        ship_by = _date(get(first, "deadline")) or start + timedelta(days=s.handling_days)
        order = Order(tiktok_order_id=oid, buyer_name=address["full_name"], address=address, items=items,
                      sale_total=round(total + shipping, 2), ship_by=ship_by, ae_order_ids=[])
        order_svc._evaluate(db, s, order)
        order.status = "held" if order.hold_reason else "needs_approval"
        db.add(order)
        db.flush()
        log(db, f"Imported TikTok order {oid} — ${order.sale_total:.2f}, est. profit ${order.est_profit:.2f}"
                + (f" — HELD: {order.hold_reason}" if order.hold_reason else ""),
            "warning" if order.hold_reason else "info", order_id=order.id)
        summary["held" if order.hold_reason else "new"] += 1
        db.commit()
        if s.auto_order and order.status == "needs_approval":
            order_svc.place_order(db, s, order)
    db.commit()
    return summary


# --------------------------------------------------------------- Tracking --
TRACK_KEY = ["Order ID"]
TRACK_COLS = {"tracking": ["Tracking ID", "Tracking Number", "Tracking No"],
              "carrier": ["Shipping Provider Name", "Shipping Provider", "Carrier", "Logistics Provider"]}

CARRIERS = [("usps", "USPS"), ("ups", "UPS"), ("fedex", "FedEx"), ("dhl", "DHL eCommerce"),
            ("ontrac", "OnTrac"), ("lasership", "LaserShip"), ("amazon", "Amazon Logistics"),
            ("yun", "YunExpress"), ("4px", "4PX"), ("cainiao", "Cainiao"), ("uniuni", "UniUni"),
            ("gofo", "GOFO Express"), ("speedx", "SpeedX"), ("cirro", "Cirro E-Commerce")]


def tiktok_carrier(name: str) -> str:
    n = (name or "").lower().replace(" ", "")
    for key, label in CARRIERS:
        if key in n:
            return label
    return name


def tracking_orders(db: Session) -> list[Order]:
    return list(db.exec(select(Order).where(Order.status == "tracking_ready").order_by(Order.ship_by)))


def tracking_file(db: Session, template: Optional[tuple[bytes, str]] = None) -> tuple[bytes, str]:
    """Returns (file bytes, filename)."""
    rows = tracking_orders(db)
    records = {o.tiktok_order_id: {"tracking": o.tracking_number, "carrier": tiktok_carrier(o.carrier)}
               for o in rows}
    if template:
        data, name = template
        out = fill_template(data, name, TRACK_KEY, TRACK_COLS, records, drop_unmatched=True)
        return out, "tiktok-tracking-" + name.rsplit("/", 1)[-1]
    out = simple_xlsx(["Order ID", "Tracking ID", "Shipping Provider Name"],
                      [[k, v["tracking"], v["carrier"]] for k, v in records.items()], [24, 30, 26])
    return out, f"tiktok-tracking-{utcnow():%Y%m%d-%H%M}.xlsx"


def mark_tracking_uploaded(db: Session, order_ids: list[int]) -> int:
    n = 0
    for oid in order_ids:
        o = db.get(Order, oid)
        if o and o.status == "tracking_ready":
            o.status, o.error, o.updated_at = "shipped", "", utcnow()
            db.add(o)
            log(db, f"Tracking {o.tracking_number} marked as uploaded to TikTok", order_id=o.id)
            n += 1
    db.commit()
    return n


# ------------------------------------------------------------ Price/stock --
PRICE_KEY = ["Seller SKU"]
PRICE_COLS = {"price": ["Retail Price", "Price", "SKU Price", "Selling Price"],
              "quantity": ["Quantity", "Available Stock", "Stock", "Inventory", "Available Quantity"]}


def pending_updates(db: Session) -> list[tuple[Product, Variant]]:
    out = []
    for p in db.exec(select(Product).where(Product.status.in_(["listed", "paused"]))):
        for v in db.exec(select(Variant).where(Variant.product_id == p.id)):
            if v.tt_price is None or abs(v.tt_price - v.price) >= 0.01 or v.tt_stock != v.listed_stock:
                out.append((p, v))
    return out


def price_stock_file(db: Session, template: Optional[tuple[bytes, str]] = None) -> tuple[bytes, str]:
    pend = pending_updates(db)
    records = {v.seller_sku: {"price": f"{v.price:.2f}", "quantity": v.listed_stock} for _, v in pend}
    if template:
        data, name = template
        out = fill_template(data, name, PRICE_KEY, PRICE_COLS, records, drop_unmatched=False)
        return out, "tiktok-prices-" + name.rsplit("/", 1)[-1]
    rows = [[p.title[:60], v.name, v.seller_sku, f"{v.tt_price:.2f}" if v.tt_price is not None else "",
             f"{v.price:.2f}", v.tt_stock if v.tt_stock is not None else "", v.listed_stock] for p, v in pend]
    out = simple_xlsx(["Product", "Variant", "Seller SKU", "Old price", "New price", "Old stock", "New stock"],
                      rows, [40, 18, 18, 11, 11, 11, 11])
    return out, f"tiktok-price-stock-{utcnow():%Y%m%d-%H%M}.xlsx"


def mark_updates_done(db: Session) -> int:
    pend = pending_updates(db)
    for _, v in pend:
        v.tt_price, v.tt_stock = v.price, v.listed_stock
        db.add(v)
    if pend:
        log(db, f"Marked {len(pend)} price/stock update(s) as done on TikTok")
    db.commit()
    return len(pend)


# ---------------------------------------------------------------- Listing --
def images_zip(product: Product) -> bytes:
    buf = io.BytesIO()
    failed = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, httpx.Client(timeout=20) as http:
        for i, url in enumerate(product.images, 1):
            try:
                r = http.get(url, follow_redirects=True)
                r.raise_for_status()
                ext = ".png" if "png" in r.headers.get("content-type", "") else ".jpg"
                z.writestr(f"{i:02d}{ext}", r.content)
            except Exception:
                failed.append(url)
        if failed:
            z.writestr("could-not-download.txt", "\n".join(failed))
    return buf.getvalue()


# ------------------------------------------------------------------- Mock --
def sample_export(db: Session) -> bytes:
    """MOCK_MODE: a fake Seller Center Orders export using your listed products."""
    import random

    vs = [v for p in db.exec(select(Product).where(Product.status == "listed"))
          for v in db.exec(select(Variant).where(Variant.product_id == p.id))]
    headers = ["Order ID", "Order Status", "SKU ID", "Seller SKU", "Product Name", "Variation", "Quantity",
               "SKU Unit Original Price", "SKU Subtotal Before Discount", "SKU Seller Discount",
               "Shipping Fee After Discount", "Created Time", "Paid Time", "Recipient", "Phone #",
               "Zipcode", "Country", "State", "City", "Detail Address", "Additional address information"]
    rows = []
    now = datetime.now()
    for n in range(2 if vs else 0):
        v = random.choice(vs)
        p = db.get(Product, v.product_id)
        qty = random.choice([1, 1, 2])
        rows.append([f"5768{random.randint(10**13, 10**14 - 1)}", "Awaiting shipment", "", v.seller_sku,
                     p.title, v.name, qty, f"{v.price:.2f}", f"{v.price * qty:.2f}", "0.00", "0.00",
                     now.strftime("%m/%d/%Y %I:%M:%S %p"), now.strftime("%m/%d/%Y %I:%M:%S %p"),
                     random.choice(["Jordan Lee", "Sam Rivera", "Alex Kim"]), "(+1)5555550123", "78701",
                     "United States", "Texas", "Austin", "123 Demo St", "Apt 4"])
    return simple_csv(headers, rows)
