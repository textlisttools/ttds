"""Fake TikTok Shop and AliExpress clients for MOCK_MODE.

They behave like the real clients (same method names and return shapes) but
keep everything in memory, so you can click through the whole workflow before
you have API keys. Nothing here talks to the internet or spends money.
"""
from __future__ import annotations

import itertools
import random
import time
from datetime import datetime, timedelta

from typing import Optional

from ..db import utcnow
from .aliexpress import product_id_from_url

_ids = itertools.count(1000)
STATE: dict = {"tt_products": {}, "tt_orders": [], "ae_orders": {}}


class MockAliExpressClient:
    def __init__(self, *a, **k):
        pass

    def authorize_url(self, state: str) -> str:
        return f"/oauth/aliexpress/callback?code=mock&state={state}"

    def exchange_code(self, code: str) -> dict:
        return {"access_token": "mock-ae", "refresh_token": "mock-ae-r",
                "expires_at": utcnow() + timedelta(days=30),
                "refresh_expires_at": None, "extra": {"user_nick": "mock-buyer"}}

    refresh = lambda self, rt: self.exchange_code("mock")  # noqa: E731

    def get_product(self, product_id: str) -> dict:
        product_id = product_id_from_url(product_id)
        rnd = random.Random(product_id)
        base = round(rnd.uniform(3, 15), 2)
        colors = ["Black", "White", "Blue"]
        return {
            "product_id": product_id,
            "title": f"Sample Gadget {product_id[-4:]} – Portable Mini LED Light",
            "description": "<p>Demo product from MOCK_MODE.</p>",
            "images": [f"https://picsum.photos/seed/{product_id}{i}/800" for i in range(3)],
            "variants": [{
                "sku_id": f"{product_id}{i}", "sku_attr": f"14:{i}#{c}", "name": c,
                "image": f"https://picsum.photos/seed/{product_id}{c}/400",
                "price": round(base + i * 0.5, 2), "stock": rnd.randint(0, 400),
                "ship_from": "United States" if i < 2 else "China",
            } for i, c in enumerate(colors)],
        }

    def get_shipping(self, product_id: str, sku_id: str, quantity: int = 1) -> dict:
        from_us = not sku_id.endswith("2")
        return {"cost": 0.0 if from_us else 1.99, "delivery_days": 5 if from_us else 15,
                "service": "MOCK_STANDARD", "ship_from": "US" if from_us else "CN"}

    def place_order(self, out_order_id: str, address: dict, items: list[dict]) -> list[str]:
        oid = f"80{next(_ids)}{int(time.time()) % 10000}"
        STATE["ae_orders"][oid] = {"created": time.time(), "out": out_order_id}
        return [oid]

    def get_order(self, ae_order_id: str) -> dict:
        return {"status": "WAIT_SELLER_SEND_GOODS", "logistics_status": ""}

    def get_tracking(self, ae_order_id: str) -> Optional[dict]:
        o = STATE["ae_orders"].get(ae_order_id)
        # Pretend the supplier ships ~1 minute after the order is placed.
        if not o or time.time() - o["created"] < 60:
            return None
        return {"tracking_number": f"9400{ae_order_id[-12:]:0>12}US", "carrier": "USPS"}


class MockTikTokClient:
    def __init__(self, *a, **k):
        pass

    def authorize_url(self, state: str) -> str:
        return f"/oauth/tiktok/callback?code=mock&state={state}"

    def exchange_code(self, code: str) -> dict:
        return {"access_token": "mock-tt", "refresh_token": "mock-tt-r",
                "expires_at": utcnow() + timedelta(days=7),
                "refresh_expires_at": None, "extra": {"seller_name": "Mock Shop"}}

    refresh = lambda self, rt: self.exchange_code("mock")  # noqa: E731

    def get_shops(self) -> list[dict]:
        return [{"id": "7000001", "name": "Mock Shop", "cipher": "MOCKCIPHER", "region": "US"}]

    def get_warehouses(self) -> list[dict]:
        return [{"id": "WH-MOCK-1", "name": "Mock warehouse", "is_default": True,
                 "type": "SALES_WAREHOUSE"}]

    def recommend_category(self, title: str) -> str:
        return "600001"

    def upload_image(self, image_url: str) -> str:
        return f"tos-mock/{abs(hash(image_url))}"

    def create_product(self, *, title, description, category_id, image_uris, skus,
                       warehouse_id, weight_kg=0.5) -> dict:
        pid = f"17{next(_ids)}"
        sku_map = {k["seller_sku"]: f"{pid}{i}" for i, k in enumerate(skus)}
        STATE["tt_products"][pid] = {"title": title, "skus": {
            sku_map[k["seller_sku"]]: {**k} for k in skus}}
        return {"product_id": pid, "skus": sku_map}

    def update_inventory(self, product_id, sku_id, quantity, warehouse_id) -> None:
        STATE["tt_products"].get(product_id, {}).get("skus", {}).get(sku_id, {})["quantity"] = quantity

    def update_price(self, product_id, sku_id, price) -> None:
        STATE["tt_products"].get(product_id, {}).get("skus", {}).get(sku_id, {})["price"] = price

    def simulate_order(self) -> Optional[dict]:
        """Dashboard button: create a fake TikTok sale of one of your listed products."""
        if not STATE["tt_products"]:
            return None
        pid = random.choice(list(STATE["tt_products"]))
        sku_id, sku = random.choice(list(STATE["tt_products"][pid]["skus"].items()))
        qty = random.choice([1, 1, 2])
        order = {
            "id": f"57{next(_ids)}{int(time.time())}",
            "status": "AWAITING_SHIPMENT",
            "buyer_name": random.choice(["Jordan Lee", "Sam Rivera", "Alex Kim"]),
            "address": {"full_name": "Jordan Lee", "phone": "5555550123",
                        "address": "123 Demo St", "address2": "Apt 4", "city": "Austin",
                        "province": "Texas", "zip": "78701", "country": "US"},
            "items": [{"tiktok_sku_id": sku_id, "seller_sku": sku["seller_sku"],
                       "title": STATE["tt_products"][pid]["title"], "quantity": qty,
                       "sale_price": sku["price"], "tiktok_line_ids": [f"L{next(_ids)}" for _ in range(qty)]}],
            "total": round(sku["price"] * qty, 2),
            "ship_by": utcnow() + timedelta(days=2),
            "delivery_option_id": "MOCKDO",
        }
        STATE["tt_orders"].append(order)
        return order

    def search_orders(self, status="AWAITING_SHIPMENT", created_after=None) -> list[dict]:
        return [o for o in STATE["tt_orders"] if o["status"] == status]

    def get_order_status(self, order_id: str) -> str:
        for o in STATE["tt_orders"]:
            if o["id"] == order_id:
                return o["status"]
        return ""

    def get_shipping_providers(self, delivery_option_id: str) -> list[dict]:
        return [{"id": "P-USPS", "name": "USPS"}, {"id": "P-UPS", "name": "UPS"},
                {"id": "P-FEDEX", "name": "FedEx"}]

    def mark_shipped(self, order_id, line_item_ids, tracking_number, shipping_provider_id) -> None:
        for o in STATE["tt_orders"]:
            if o["id"] == order_id:
                o["status"] = "IN_TRANSIT"
