import io
from datetime import timedelta

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlmodel import select

from app import db
from app.clients.mock import STATE
from app.db import utcnow
from app.services import connections, orders, products, tiktok_files
from app.services.sheets import read_rows, simple_csv

HEAD = ["Order ID", "Order Status", "SKU ID", "Seller SKU", "Product Name", "Variation", "Quantity",
        "SKU Unit Original Price", "SKU Subtotal Before Discount", "SKU Seller Discount",
        "Shipping Fee After Discount", "Created Time", "Paid Time", "Recipient", "Phone #", "Zipcode",
        "Country", "State", "City", "Detail Address", "Additional address information"]


def row(oid, sku, qty=1, price="29.99", status="Awaiting shipment", name="Jordan Lee", addr="1 Main St"):
    return [oid, status, "1729000000000000001", sku, "Gadget", "Black", qty, price,
            f"{float(price) * qty:.2f}", "0.00", "0.00", "11/27/2026 8:07:03 PM", "11/27/2026 8:07:10 PM",
            name, "(+1)5555550123", "78701", "United States", "Texas", "Austin", addr, ""]


def setup_listed(settings):
    with db.session() as d:
        connections.save_token(d, "aliexpress", {"access_token": "a", "expires_at": utcnow() + timedelta(days=30)})
        p = products.import_product(d, settings, "1005006123456789")
        for v in products.variants_of(d, p.id):
            v.ae_stock = 100
            d.add(v)
        d.commit()
        products.mark_listed(d, p, "")
        return p.id, [v.seller_sku for v in products.variants_of(d, p.id)]


def test_import_orders_csv(settings):
    pid, skus = setup_listed(settings)
    data = simple_csv(HEAD, [row("576800000000000001", skus[0], 2), row("576800000000000002", "UNKNOWN"),
                             row("576800000000000003", skus[0], status="Shipped"),
                             row("576800000000000004", skus[0], name="J*** L***", addr="1** M***")])
    with db.session() as d:
        r = tiktok_files.import_orders(d, settings, data, "orders.csv")
        assert r == {"new": 1, "held": 2, "cancelled": 0, "skipped": 1, "already": 0}
        o = d.exec(select(db.Order).where(db.Order.tiktok_order_id == "576800000000000001")).one()
        assert o.status == "needs_approval" and o.items[0]["quantity"] == 2 and o.sale_total == 59.98
        assert o.address["country"] == "US" and o.address["phone"] == "5555550123"
        assert o.ship_by is not None
        masked = d.exec(select(db.Order).where(db.Order.tiktok_order_id == "576800000000000004")).one()
        assert "address" in masked.hold_reason
        orders.update_address(d, settings, masked, {"full_name": "Jordan Lee", "address": "1 Main St"})
        assert masked.status == "needs_approval"
        unknown = d.exec(select(db.Order).where(db.Order.tiktok_order_id == "576800000000000002")).one()
        assert unknown.status == "held" and "not a product" in unknown.hold_reason
        vid = products.variants_of(d, pid)[0].id
        orders.link_item(d, settings, unknown, 0, vid)
        assert unknown.status == "needs_approval"
        # Re-importing is safe, and a cancellation is picked up.
        r = tiktok_files.import_orders(d, settings, simple_csv(HEAD, [
            row("576800000000000001", skus[0]), row("576800000000000002", "X", status="Cancelled")]), "o.csv")
        assert r["already"] == 1 and r["cancelled"] == 1


def test_import_xlsx_with_numeric_ids_and_title_rows(settings):
    _, skus = setup_listed(settings)
    wb = Workbook()
    ws = wb.active
    ws.append(["Orders export"])  # junk row above the header
    ws.append(HEAD)
    ws.append(row("576800000000123456", skus[0]))  # TikTok stores ids as text
    r = row("x", skus[0])
    r[0] = 5768001234.0  # a numeric cell must not come back as "5768001234.0"
    ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    with db.session() as d:
        tiktok_files.import_orders(d, settings, buf.getvalue(), "export.xlsx")
        ids = {o.tiktok_order_id for o in d.exec(select(db.Order))}
        assert ids == {"576800000000123456", "5768001234"}


def test_tracking_and_price_files(settings):
    pid, skus = setup_listed(settings)
    with db.session() as d:
        tiktok_files.import_orders(d, settings, simple_csv(HEAD, [row("576800000000000009", skus[0])]), "o.csv")
        o = d.exec(select(db.Order)).one()
        orders.place_order(d, settings, o)
        assert o.status == "placed"
        STATE["ae_orders"][o.ae_order_ids[0]]["created"] -= 120
        assert orders.sync_tracking(d, settings) == 1
        d.refresh(o)
        assert o.status == "tracking_ready" and o.tracking_number

        # Generic tracking file
        data, name = tiktok_files.tracking_file(d)
        rows = read_rows(data, name)
        assert rows[0] == ["Order ID", "Tracking ID", "Shipping Provider Name"]
        assert rows[1] == ["576800000000000009", o.tracking_number, "USPS"]

        # TikTok's own template: header + instruction row + an order we don't handle
        wb = Workbook()
        ws = wb.active
        ws.append(["Order ID", "Package ID", "Shipping Provider Name", "Tracking ID"])
        ws.append(["Required", "Optional", "Pick from list", "Required"])
        ws.append(["576899999999999999", "", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        data, _ = tiktok_files.tracking_file(d, (buf.getvalue(), "template.xlsx"))
        out = [[c.value for c in r] for r in load_workbook(io.BytesIO(data)).active.iter_rows()]
        assert out[1][0] == "Required"  # instructions kept
        assert ["576800000000000009", None, "USPS", o.tracking_number] in out
        assert not any(r[0] == "576899999999999999" for r in out)

        assert tiktok_files.mark_tracking_uploaded(d, [o.id]) == 1
        d.refresh(o)
        assert o.status == "shipped"

        # Price/stock: nothing pending right after listing; a cost change creates an update.
        assert tiktok_files.pending_updates(d) == []
        p = d.get(db.Product, pid)
        v = products.variants_of(d, pid)[0]
        v.cost += 0  # cost comes from mock; force a stale TikTok price instead
        v.tt_price = v.price - 3
        d.add(v)
        d.commit()
        assert len(tiktok_files.pending_updates(d)) == 1
        data, name = tiktok_files.price_stock_file(d)
        assert v.seller_sku in str(read_rows(data, name))
        assert tiktok_files.mark_updates_done(d) == 1
        assert tiktok_files.pending_updates(d) == []
        products.sync_product(d, settings, p)  # files mode never calls TikTok


def test_pages_in_files_mode(settings):
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pass"})
        assert "Daily routine" in c.get("/").text
        assert "Files mode" in c.get("/settings").text
        c.get("/connect/aliexpress")
        r = c.post("/products/import", data={"url": "1005006123456789"})
        pid = r.url.path.rsplit("/", 1)[-1]
        assert "List it on TikTok" in r.text and "TTDS-" in r.text
        r = c.post(f"/products/{pid}/listed", data={"tiktok_product_id": ""})
        assert "Marked as listed" in r.text
        sample = c.get("/files/sample-orders")
        assert sample.status_code == 200 and b"Awaiting shipment" in sample.content
        r = c.post("/files/orders", files={"file": ("orders.csv", sample.content, "text/csv")})
        assert "Orders file imported" in r.text
        r = c.post("/files/orders", files={"file": ("junk.csv", b"a,b\n1,2\n", "text/csv")})
        assert "read that file" in r.text
        oid = c.get("/orders").text.split('href="/orders/')[1].split('"')[0]
        assert c.get(f"/orders/{oid}").status_code == 200
