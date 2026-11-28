from datetime import datetime, timedelta

from app.db import utcnow

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.clients.mock import MockTikTokClient, STATE
from app.services import connections, orders, products
from app.services.pricing import listed_stock, profit, selling_price


def connect_both(s):
    with db.session() as d:
        connections.finish_tiktok_connect(d, s, MockTikTokClient().exchange_code("x"))
        connections.save_token(d, "aliexpress", {
            "access_token": "a", "refresh_token": "r",
            "expires_at": utcnow() + timedelta(days=30), "extra": {}})


def test_pricing(settings):
    assert selling_price(5.0, 1.0, settings) == 12.99  # 6*1.8+2 = 12.8 -> 12.99
    assert listed_stock(1, settings) == 0
    assert listed_stock(500, settings) == settings.max_listed_stock
    assert profit(20.0, 10.0, settings) == round(20 * (1 - settings.tiktok_fee_rate) - 10, 2)


def test_requires_connection(settings):
    with db.session() as d, pytest.raises(connections.NotConnected):
        products.import_product(d, settings, "1005006123456789")


def test_full_flow(api_settings):
    settings = api_settings
    connect_both(settings)
    with db.session() as d:
        p = products.import_product(d, settings, "https://www.aliexpress.us/item/1005006123456789.html")
        vs = products.variants_of(d, p.id)
        # The China-warehouse variant is filtered out by REQUIRE_LOCAL_WAREHOUSE.
        assert all(v.name != "Blue" for v in vs)
        products.publish_product(d, settings, p)
        assert p.status in ("listed",) and p.tiktok_product_id
        # make sure there is stock to sell
        for v in products.variants_of(d, p.id):
            v.ae_stock, v.listed_stock = 100, 20
            d.add(v)
        d.commit()

    order = MockTikTokClient().simulate_order()
    with db.session() as d:
        assert orders.sync_orders(d, settings) == 1
        assert orders.sync_orders(d, settings) == 0  # no duplicates
        o = d.exec(select(db.Order)).one()
        assert o.tiktok_order_id == order["id"]
        assert o.status in ("needs_approval", "held")
        o = orders.place_order(d, settings, o, force=True)
        assert o.status == "placed" and o.ae_order_ids
        with pytest.raises(ValueError):
            orders.place_order(d, settings, o, force=True)  # never buy twice

        # supplier hasn't shipped yet
        assert orders.sync_tracking(d, settings) == 0
        STATE["ae_orders"][o.ae_order_ids[0]]["created"] -= 120
        assert orders.sync_tracking(d, settings) == 1
        d.refresh(o)
        assert o.status == "shipped" and o.tracking_number


def test_low_profit_is_held(api_settings):
    settings = api_settings
    connect_both(settings)
    with db.session() as d:
        p = products.import_product(d, settings, "1005006123456789")
        for v in products.variants_of(d, p.id):
            v.price, v.ae_stock = v.cost + 0.5, 100  # tiny margin
            d.add(v)
        d.commit()
        products.publish_product(d, settings, p)
    MockTikTokClient().simulate_order()
    with db.session() as d:
        orders.sync_orders(d, settings)
        o = d.exec(select(db.Order)).one()
        assert o.status == "held" and "profit" in o.hold_reason
        assert orders.place_order(d, settings, o).status == "held"
        assert not o.ae_order_ids


def test_price_change_pushed(api_settings):
    settings = api_settings
    connect_both(settings)
    with db.session() as d:
        p = products.import_product(d, settings, "1005006123456789")
        products.publish_product(d, settings, p)
        v = products.variants_of(d, p.id)[0]
        v.cost = v.cost - 1  # pretend we stored an older, cheaper cost
        v.price = selling_price(v.cost, v.shipping_cost, settings)
        d.add(v)
        d.commit()
        assert products.sync_product(d, settings, p) >= 1


def test_pages_render(settings):
    from app.main import app
    from app.settings_store import AppSetting
    with TestClient(app) as c:
        with db.session() as d:  # this test covers the direct-API mode
            d.add(AppSetting(key="tiktok_mode", value="api"))
            d.commit()
        assert c.get("/", follow_redirects=False).status_code == 303
        r = c.post("/login", data={"username": "admin", "password": "test-pass"})
        assert r.status_code == 200
        for path in ("/", "/orders", "/products", "/settings"):
            assert c.get(path).status_code == 200, path
        # connect both via mock OAuth
        for plat in ("tiktok", "aliexpress"):
            r = c.get(f"/connect/{plat}")
            assert "Connected" in r.text or "connected" in r.text
        r = c.post("/products/import", data={"url": "1005006123456789"})
        assert "Imported as a draft" in r.text
        pid = r.url.path.rsplit("/", 1)[-1]
        r = c.post(f"/products/{pid}/publish")
        assert "Sent to TikTok" in r.text
        r = c.post("/mock/order")
        assert r.status_code == 200
