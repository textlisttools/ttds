import hashlib
import hmac

from app.clients import aliexpress, tiktok


def test_tiktok_sign_matches_spec():
    params = {"app_key": "k", "timestamp": "1", "shop_cipher": "c", "sign": "x", "access_token": "t"}
    body = '{"a":1}'
    expected_base = "secret/product/202309/productsapp_keykshop_cipherctimestamp1" + body + "secret"
    expected = hmac.new(b"secret", expected_base.encode(), hashlib.sha256).hexdigest()
    assert tiktok.sign("secret", "/product/202309/products", params, body) == expected


def test_aliexpress_sign_business_and_system():
    params = {"method": "m", "app_key": "k", "timestamp": "1", "sign": "ignored"}
    base = "app_keykmethodmtimestamp1"
    assert aliexpress.sign("s", params) == hmac.new(b"s", base.encode(), hashlib.sha256).hexdigest().upper()
    sys_base = "/auth/token/create" + base
    assert aliexpress.sign("s", params, "/auth/token/create") == \
        hmac.new(b"s", sys_base.encode(), hashlib.sha256).hexdigest().upper()


def test_product_id_from_url():
    f = aliexpress.product_id_from_url
    assert f("1005006123456789") == "1005006123456789"
    assert f("https://www.aliexpress.us/item/1005006123456789.html?spm=a2g0o") == "1005006123456789"
    assert f("https://www.aliexpress.com/item/3256805.html") == "3256805"


def test_normalize_tiktok_order_groups_units():
    o = tiktok.normalize_order({
        "id": "1", "status": "AWAITING_SHIPMENT", "rts_sla_time": 1767225600,
        "payment": {"total_amount": "39.98"},
        "recipient_address": {"name": "A B", "phone_number": "555", "address_line1": "1 St",
                              "postal_code": "78701", "region_code": "US",
                              "district_info": [{"address_level_name": "State", "address_name": "Texas"},
                                                {"address_level_name": "City", "address_name": "Austin"}]},
        "line_items": [{"id": "l1", "sku_id": "s1", "seller_sku": "X", "sale_price": "19.99"},
                       {"id": "l2", "sku_id": "s1", "seller_sku": "X", "sale_price": "19.99"}],
    })
    assert o["items"][0]["quantity"] == 2 and o["items"][0]["tiktok_line_ids"] == ["l1", "l2"]
    assert o["address"]["city"] == "Austin" and o["address"]["province"] == "Texas"
    assert o["total"] == 39.98 and o["ship_by"].year == 2026
