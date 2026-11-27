"""TikTok Shop Open API client (API version 202309).

Docs: https://partner.tiktokshop.com/docv2  ->  API reference

Every request carries app_key, timestamp and a HMAC-SHA256 signature; requests
for a shop also carry shop_cipher and the x-tts-access-token header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from ..config import Settings

VERSION = "202309"


def _ts(x) -> datetime:
    return datetime.fromtimestamp(int(x), timezone.utc).replace(tzinfo=None)


class TikTokError(Exception):
    pass


def sign(secret: str, path: str, params: dict, body: str = "") -> str:
    """secret + path + sorted(key+value, excluding sign/access_token) + body + secret."""
    pairs = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base = f"{secret}{path}{pairs}{body}{secret}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


class TikTokClient:
    def __init__(self, settings: Settings, access_token: str = "", shop_cipher: str = ""):
        self.s = settings
        self.access_token = access_token
        self.shop_cipher = shop_cipher
        self.http = httpx.Client(timeout=30)

    # ---------------------------------------------------------------- OAuth --
    def authorize_url(self, state: str) -> str:
        return (f"{self.s.tiktok_authorize_base}/open/authorize?"
                + urlencode({"service_id": self.s.tiktok_service_id, "state": state}))

    def _token_request(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{self.s.tiktok_auth_base}{path}", params={
            "app_key": self.s.tiktok_app_key, "app_secret": self.s.tiktok_app_secret, **params})
        data = r.json()
        if data.get("code") != 0:
            raise TikTokError(f"{path}: {data.get('code')} {data.get('message')}")
        d = data["data"]
        return {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", ""),
            "expires_at": _ts(d["access_token_expire_in"]),
            "refresh_expires_at": _ts(d.get("refresh_token_expire_in", 0))
            if d.get("refresh_token_expire_in") else None,
            "extra": {"seller_name": d.get("seller_name", "")},
        }

    def exchange_code(self, code: str) -> dict:
        return self._token_request("/api/v2/token/get",
                                   {"auth_code": code, "grant_type": "authorized_code"})

    def refresh(self, refresh_token: str) -> dict:
        return self._token_request("/api/v2/token/refresh",
                                   {"refresh_token": refresh_token, "grant_type": "refresh_token"})

    # ------------------------------------------------------------ Core call --
    def request(self, method: str, path: str, query: Optional[dict] = None,
                body: Optional[dict] = None, files: Optional[dict] = None,
                with_shop: bool = True) -> Any:
        params = {"app_key": self.s.tiktok_app_key, "timestamp": str(int(time.time()))}
        if with_shop and self.shop_cipher:
            params["shop_cipher"] = self.shop_cipher
        for k, v in (query or {}).items():
            params[k] = ",".join(v) if isinstance(v, list) else str(v)
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        params["sign"] = sign(self.s.tiktok_app_secret, path, params, "" if files else raw)
        headers = {"x-tts-access-token": self.access_token}
        if files:
            r = self.http.request(method, self.s.tiktok_api_base + path, params=params,
                                  headers=headers, files=files)
        else:
            headers["content-type"] = "application/json"
            r = self.http.request(method, self.s.tiktok_api_base + path, params=params,
                                  headers=headers, content=raw or None)
        data = r.json()
        if data.get("code") != 0:
            raise TikTokError(f"{method} {path}: {data.get('code')} {data.get('message')}")
        return data.get("data") or {}

    # ----------------------------------------------------------------- Shop --
    def get_shops(self) -> list[dict]:
        d = self.request("GET", f"/authorization/{VERSION}/shops", with_shop=False)
        return d.get("shops", [])

    def get_warehouses(self) -> list[dict]:
        return self.request("GET", f"/logistics/{VERSION}/warehouses").get("warehouses", [])

    # ------------------------------------------------------------- Products --
    def recommend_category(self, title: str) -> str:
        d = self.request("POST", f"/product/{VERSION}/categories/recommend",
                         body={"product_title": title[:255]})
        return str(d.get("leaf_category_id", ""))

    def upload_image(self, image_url: str) -> str:
        img = self.http.get(image_url, follow_redirects=True)
        img.raise_for_status()
        d = self.request("POST", f"/product/{VERSION}/images/upload",
                         files={"data": ("image.jpg", img.content, "image/jpeg"),
                                "use_case": (None, "MAIN_IMAGE")})
        return d["uri"]

    def create_product(self, *, title: str, description: str, category_id: str,
                       image_uris: list[str], skus: list[dict], warehouse_id: str,
                       weight_kg: float = 0.5) -> dict:
        """skus: [{seller_sku, name, price, quantity}] -> returns product_id + sku ids."""
        body = {
            "title": title[:255],
            "description": description or title,
            "category_id": category_id,
            "main_images": [{"uri": u} for u in image_uris[:9]],
            "package_weight": {"value": f"{weight_kg:.2f}", "unit": "KILOGRAM"},
            "skus": [{
                "seller_sku": k["seller_sku"],
                "sales_attributes": [{"name": "Variant", "value_name": k["name"][:50]}]
                if len(skus) > 1 else [],
                "price": {"amount": f"{k['price']:.2f}", "currency": self.s.tiktok_currency},
                "inventory": [{"warehouse_id": warehouse_id, "quantity": int(k["quantity"])}],
            } for k in skus],
        }
        d = self.request("POST", f"/product/{VERSION}/products", body=body)
        return {"product_id": str(d["product_id"]),
                "skus": {s["seller_sku"]: str(s["id"]) for s in d.get("skus", [])}}

    def update_inventory(self, product_id: str, sku_id: str, quantity: int, warehouse_id: str) -> None:
        self.request("POST", f"/product/{VERSION}/products/{product_id}/inventory/update", body={
            "skus": [{"id": sku_id, "inventory": [{"warehouse_id": warehouse_id, "quantity": quantity}]}]})

    def update_price(self, product_id: str, sku_id: str, price: float) -> None:
        self.request("POST", f"/product/{VERSION}/products/{product_id}/prices/update", body={
            "skus": [{"id": sku_id, "price": {"amount": f"{price:.2f}",
                                              "currency": self.s.tiktok_currency}}]})

    # --------------------------------------------------------------- Orders --
    def search_orders(self, status: str = "AWAITING_SHIPMENT",
                      created_after: Optional[int] = None) -> list[dict]:
        orders, token = [], ""
        while True:
            q = {"page_size": 50, "sort_field": "create_time", "sort_order": "ASC"}
            if token:
                q["page_token"] = token
            body: dict = {"order_status": status}
            if created_after:
                body["create_time_ge"] = created_after
            d = self.request("POST", f"/order/{VERSION}/orders/search", query=q, body=body)
            orders += [normalize_order(o) for o in d.get("orders", [])]
            token = d.get("next_page_token") or ""
            if not token:
                return orders

    def get_order_status(self, order_id: str) -> str:
        d = self.request("GET", f"/order/{VERSION}/orders", query={"ids": [order_id]})
        orders = d.get("orders", [])
        return orders[0].get("status", "") if orders else ""

    def get_shipping_providers(self, delivery_option_id: str) -> list[dict]:
        d = self.request("GET", f"/logistics/{VERSION}/delivery_options/{delivery_option_id}/shipping_providers")
        return d.get("shipping_providers", [])

    def mark_shipped(self, order_id: str, line_item_ids: list[str], tracking_number: str,
                     shipping_provider_id: str) -> None:
        self.request("POST", f"/fulfillment/{VERSION}/orders/{order_id}/packages", body={
            "order_line_item_ids": line_item_ids,
            "tracking_number": tracking_number,
            "shipping_provider_id": shipping_provider_id,
        })


def _money(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def normalize_order(o: dict) -> dict:
    """Turn a TikTok order into the simple shape the rest of the app uses."""
    addr = o.get("recipient_address", {}) or {}
    levels = {d.get("address_level_name", "").lower(): d.get("address_name", "")
              for d in addr.get("district_info", []) or []}
    # TikTok returns one line item per unit; group them by SKU.
    grouped: dict[str, dict] = {}
    for li in o.get("line_items", []) or []:
        key = str(li.get("sku_id"))
        g = grouped.setdefault(key, {
            "tiktok_sku_id": key, "seller_sku": li.get("seller_sku", ""),
            "title": f"{li.get('product_name', '')} {li.get('sku_name', '')}".strip(),
            "quantity": 0, "sale_price": _money(li.get("sale_price")), "tiktok_line_ids": []})
        g["quantity"] += int(li.get("quantity") or 1)
        g["tiktok_line_ids"].append(str(li.get("id")))
    deadline = o.get("rts_sla_time") or o.get("shipping_due_time")
    return {
        "id": str(o["id"]),
        "status": o.get("status", ""),
        "buyer_name": addr.get("name", ""),
        "address": {
            "full_name": addr.get("name", ""),
            "phone": addr.get("phone_number", ""),
            "address": addr.get("address_line1", ""),
            "address2": " ".join(x for x in (addr.get("address_line2"), addr.get("address_line3")) if x),
            "city": levels.get("city", "") or levels.get("l3", ""),
            "province": levels.get("state", "") or levels.get("province", "") or levels.get("l1", ""),
            "zip": addr.get("postal_code", ""),
            "country": addr.get("region_code", ""),
        },
        "items": list(grouped.values()),
        "total": _money((o.get("payment") or {}).get("total_amount")),
        "ship_by": _ts(deadline) if deadline else None,
        "delivery_option_id": str(o.get("delivery_option_id", "")),
    }
