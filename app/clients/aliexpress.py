"""AliExpress Open Platform (Dropshipping API) client.

Docs: https://openservice.aliexpress.com/doc/api.htm  (category "Dropshipping")

Every call is signed with HMAC-SHA256 using your App Secret. Business APIs go
to /sync?method=...; the token endpoints are "system" APIs under /rest/auth/...
and include the API path in the signature.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from ..config import Settings


class AliExpressError(Exception):
    pass


def sign(secret: str, params: dict, api_path: str = "") -> str:
    """IOP signature: HMAC-SHA256 over (api_path + sorted key+value pairs), uppercase hex."""
    base = api_path + "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def product_id_from_url(url_or_id: str) -> str:
    """Accepts '1005006123456789' or any AliExpress item link."""
    s = url_or_id.strip()
    if s.isdigit():
        return s
    m = re.search(r"/item/(?:[^/]*?)(\d+)\.html", s) or re.search(r"(\d{12,})", s)
    if not m:
        raise AliExpressError(f"Could not find an AliExpress product id in: {url_or_id}")
    return m.group(1)


def _listify(x: Any) -> list:
    """AliExpress wraps lists as {"some_d_t_o": [...]}; unwrap either form."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict) and len(x) == 1:
        (v,) = x.values()
        return v if isinstance(v, list) else [v]
    return [x]


def _find(obj: Any, *keys: str) -> Any:
    """Depth-first search for the first non-empty value under any of `keys`."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, "", [], {}):
                return obj[k]
        for v in obj.values():
            r = _find(v, *keys)
            if r not in (None, "", [], {}):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, *keys)
            if r not in (None, "", [], {}):
                return r
    return None


def _money(v: Any) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d.]+", str(v).replace(",", ""))
    return float(m.group()) if m else 0.0


class AliExpressClient:
    def __init__(self, settings: Settings, access_token: str = ""):
        self.s = settings
        self.access_token = access_token
        self.http = httpx.Client(timeout=30)

    # ---------------------------------------------------------------- OAuth --
    def authorize_url(self, state: str) -> str:
        q = urlencode({
            "response_type": "code",
            "force_auth": "true",
            "redirect_uri": f"{self.s.base_url}/oauth/aliexpress/callback",
            "client_id": self.s.aliexpress_app_key,
            "state": state,
        })
        return f"{self.s.aliexpress_api_base}/oauth/authorize?{q}"

    def _system_call(self, api_path: str, extra: dict) -> dict:
        params = {
            "app_key": self.s.aliexpress_app_key,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
            **extra,
        }
        params["sign"] = sign(self.s.aliexpress_app_secret, params, api_path)
        r = self.http.post(f"{self.s.aliexpress_api_base}/rest{api_path}", data=params)
        data = r.json()
        if str(data.get("code", "0")) != "0":
            raise AliExpressError(f"{api_path}: {data.get('code')} {data.get('message')}")
        return data

    @staticmethod
    def _token_fields(d: dict) -> dict:
        def ms_to_dt(ms):
            return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).replace(tzinfo=None) if ms else None
        expires = ms_to_dt(d.get("expire_time")) or (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(d.get("expires_in", 0))))
        return {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", ""),
            "expires_at": expires,
            "refresh_expires_at": ms_to_dt(d.get("refresh_token_valid_time")),
            "extra": {"user_nick": d.get("user_nick") or d.get("account", "")},
        }

    def exchange_code(self, code: str) -> dict:
        return self._token_fields(self._system_call("/auth/token/create", {"code": code}))

    def refresh(self, refresh_token: str) -> dict:
        return self._token_fields(
            self._system_call("/auth/token/refresh", {"refresh_token": refresh_token}))

    # ------------------------------------------------------------ Core call --
    def call(self, method: str, **biz: Any) -> dict:
        params = {
            "app_key": self.s.aliexpress_app_key,
            "method": method,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
            "session": self.access_token,
        }
        for k, v in biz.items():
            params[k] = json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)
        params["sign"] = sign(self.s.aliexpress_app_secret, params)
        r = self.http.post(f"{self.s.aliexpress_api_base}/sync", data=params)
        data = r.json()
        if "error_response" in data:
            e = data["error_response"]
            raise AliExpressError(f"{method}: {e.get('code')} {e.get('msg') or e.get('message')}")
        # Responses look like {"aliexpress_ds_product_get_response": {"result": {...}}}
        body = next(iter(data.values())) if len(data) == 1 else data
        return body.get("result", body) if isinstance(body, dict) else body

    # ------------------------------------------------------------- Products --
    def get_product(self, product_id: str) -> dict:
        res = self.call(
            "aliexpress.ds.product.get",
            product_id=product_id,
            ship_to_country=self.s.ship_to_country,
            target_currency=self.s.aliexpress_currency,
            target_language=self.s.aliexpress_language,
        )
        base = res.get("ae_item_base_info_dto", {})
        media = res.get("ae_multimedia_info_dto", {})
        images = [u for u in (media.get("image_urls") or "").split(";") if u]
        variants = []
        for sku in _listify(res.get("ae_item_sku_info_dtos")):
            props = _listify(sku.get("ae_sku_property_dtos"))
            ship_from = ""
            names, image = [], ""
            for p in props:
                pname = (p.get("sku_property_name") or "").lower()
                value = p.get("property_value_definition_name") or p.get("sku_property_value") or ""
                if "ship" in pname:  # the "Ships From" property
                    ship_from = value
                else:
                    names.append(value)
                image = image or p.get("sku_image", "")
            variants.append({
                "sku_id": str(sku.get("sku_id")),
                "sku_attr": sku.get("id") or sku.get("sku_attr") or "",
                "name": " / ".join(n for n in names if n) or "Default",
                "image": image,
                "price": _money(sku.get("offer_sale_price") or sku.get("sku_price")),
                "stock": int(sku.get("sku_available_stock") or sku.get("ipm_sku_stock") or 0),
                "ship_from": ship_from,
            })
        return {
            "product_id": str(base.get("product_id", product_id)),
            "title": base.get("subject", ""),
            "description": base.get("detail", "") or base.get("mobile_detail", ""),
            "images": images,
            "variants": variants,
        }

    def get_shipping(self, product_id: str, sku_id: str, quantity: int = 1) -> dict:
        """Cheapest shipping option to ship_to_country. Returns cost, days, service name."""
        res = self.call(
            "aliexpress.ds.freight.query",
            queryDeliveryReq={
                "productId": product_id,
                "selectedSkuId": sku_id,
                "quantity": quantity,
                "shipToCountry": self.s.ship_to_country,
                "currency": self.s.aliexpress_currency,
                "language": self.s.aliexpress_language,
                "locale": "en_US",
            },
        )
        options = _listify(_find(res, "delivery_options"))
        best = None
        for o in options:
            if str(o.get("free_shipping")).lower() == "true":
                cost = 0.0
            else:
                # shipping_fee_format is e.g. "$3.21"; fall back to the raw fee field.
                cost = _money(o.get("shipping_fee_format") or o.get("shipping_fee_cent"))
            days = int(o.get("max_delivery_days") or o.get("min_delivery_days") or 99)
            opt = {"cost": cost, "delivery_days": days, "service": o.get("code", ""),
                   "ship_from": o.get("ship_from_country", "")}
            if self.s.aliexpress_logistics_service and opt["service"] == self.s.aliexpress_logistics_service:
                return opt
            if best is None or (cost, days) < (best["cost"], best["delivery_days"]):
                best = opt
        return best or {"cost": 0.0, "delivery_days": None, "service": "", "ship_from": ""}

    # --------------------------------------------------------------- Orders --
    def place_order(self, out_order_id: str, address: dict, items: list[dict]) -> list[str]:
        """Create an AliExpress order shipped to the TikTok buyer.

        address: full_name, phone, address, address2, city, province, zip, country
        items:   product_id, sku_attr, quantity, logistics_service (optional)
        Returns the AliExpress order ids (one order per seller).
        """
        res = self.call(
            "aliexpress.ds.order.create",
            param_place_order_request4_open_api_d_t_o={
                "out_order_id": out_order_id,
                "logistics_address": {
                    "full_name": address.get("full_name", ""),
                    "contact_person": address.get("full_name", ""),
                    "mobile_no": address.get("phone", ""),
                    "phone_country": address.get("phone_country", "+1"),
                    "address": address.get("address", ""),
                    "address2": address.get("address2", ""),
                    "city": address.get("city", ""),
                    "province": address.get("province", ""),
                    "zip": address.get("zip", ""),
                    "country": address.get("country", self.s.ship_to_country),
                },
                "product_items": [{
                    "product_id": int(i["product_id"]),
                    "product_count": int(i["quantity"]),
                    "sku_attr": i.get("sku_attr", ""),
                    "logistics_service_name": i.get("logistics_service", ""),
                    "order_memo": "Please do not include invoices or promotional material.",
                } for i in items],
            },
            # try_to_pay asks AliExpress to charge your saved payment method automatically.
            ds_extend_request={"payment": {"pay_currency": self.s.aliexpress_currency,
                                           "try_to_pay": "true"}},
        )
        if str(res.get("is_success", "true")).lower() != "true":
            raise AliExpressError(f"Order not created: {res.get('error_code')} {res.get('error_msg', '')}")
        ids = _listify(res.get("order_list"))
        return [str(i) for i in ids]

    def get_order(self, ae_order_id: str) -> dict:
        res = self.call("aliexpress.trade.ds.order.get", single_order_query={"order_id": ae_order_id})
        return {"status": res.get("order_status", ""), "logistics_status": res.get("logistics_status", "")}

    def get_tracking(self, ae_order_id: str) -> Optional[dict]:
        """Returns {'tracking_number', 'carrier'} once the supplier has shipped, else None."""
        try:
            res = self.call("aliexpress.ds.order.tracking.get", ae_order_id=ae_order_id,
                            language="en_US")
        except AliExpressError:
            return None
        number = _find(res, "mail_no", "logistics_no", "tracking_number")
        if not number:
            return None
        carrier = _find(res, "carrier_name", "logistics_company", "service_name") or ""
        return {"tracking_number": str(number), "carrier": str(carrier)}
