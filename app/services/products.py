"""Import AliExpress products, publish them to TikTok Shop, and keep them in sync."""
from sqlmodel import Session, select

from ..config import Settings
from ..db import Product, Variant, log, utcnow
from . import connections
from .pricing import listed_stock, same_country, selling_price


class ImportRejected(Exception):
    pass


def import_product(db: Session, s: Settings, url_or_id: str,
                   multiplier: float | None = None) -> Product:
    """Fetch an AliExpress product and save it as a draft (not yet on TikTok)."""
    ae = connections.aliexpress(db, s)
    data = ae.get_product(url_or_id)
    existing = db.exec(select(Product).where(Product.ae_product_id == data["product_id"])).first()
    if existing:
        raise ImportRejected(f"Already imported as product #{existing.id}.")

    kept, rejected = [], []
    for v in data["variants"]:
        ship = ae.get_shipping(data["product_id"], v["sku_id"])
        origin = v.get("ship_from") or ship.get("ship_from", "")
        if s.require_local_warehouse and not same_country(s.ship_to_country, origin):
            rejected.append(f"{v['name']}: ships from {origin or 'unknown'}")
            continue
        days = ship.get("delivery_days")
        if days is None or days > s.max_delivery_days:
            rejected.append(f"{v['name']}: {days or '?'} day delivery")
            continue
        if v["stock"] <= 0:
            rejected.append(f"{v['name']}: out of stock")
            continue
        kept.append((v, ship))

    if not kept:
        raise ImportRejected("No variant passed your rules — " + "; ".join(rejected[:6]))

    product = Product(ae_product_id=data["product_id"], ae_url=url_or_id.strip(),
                      title=data["title"][:255], description=data["description"],
                      images=data["images"][:9], ship_from=s.ship_to_country,
                      markup_multiplier=multiplier)
    db.add(product)
    db.flush()
    for v, ship in kept:
        db.add(Variant(
            product_id=product.id, ae_sku_id=v["sku_id"], ae_sku_attr=v["sku_attr"],
            name=v["name"], image=v["image"], cost=v["price"], shipping_cost=ship["cost"],
            delivery_days=ship["delivery_days"], ae_stock=v["stock"],
            price=selling_price(v["price"], ship["cost"], s, multiplier),
            listed_stock=listed_stock(v["stock"], s)))
    note = f" (skipped {len(rejected)}: {'; '.join(rejected[:3])})" if rejected else ""
    log(db, f"Imported '{product.title[:60]}' with {len(kept)} variant(s){note}", product_id=product.id)
    db.commit()
    db.refresh(product)
    return product


def variants_of(db: Session, product_id: int) -> list[Variant]:
    return list(db.exec(select(Variant).where(Variant.product_id == product_id)))


def ensure_seller_skus(db: Session, product: Product) -> list[Variant]:
    """Every variant gets a Seller SKU. You type it into TikTok so orders can be matched."""
    vs = variants_of(db, product.id)
    for v in vs:
        if not v.seller_sku:
            v.seller_sku = f"TTDS-{product.id}-{v.id}"
            db.add(v)
    db.commit()
    return vs


def mark_listed(db: Session, product: Product, tiktok_product_id: str = "") -> Product:
    """Files mode: you created the listing in Seller Center yourself."""
    for v in ensure_seller_skus(db, product):
        v.tt_price, v.tt_stock = v.price, v.listed_stock
        db.add(v)
    product.tiktok_product_id = tiktok_product_id.strip() or product.tiktok_product_id
    product.status, product.error = "listed", ""
    db.add(product)
    log(db, f"Marked '{product.title[:60]}' as listed on TikTok", product_id=product.id)
    db.commit()
    return product


def publish_product(db: Session, s: Settings, product: Product) -> Product:
    """Create the listing on TikTok Shop. TikTok reviews new listings before they go live."""
    tt, tok = connections.tiktok(db, s)
    warehouse = tok.extra.get("warehouse_id", "")
    variants = variants_of(db, product.id)
    try:
        uris = [tt.upload_image(u) for u in product.images[:9]]
        category = product.tiktok_category_id or tt.recommend_category(product.title)
        for v in variants:
            v.seller_sku = v.seller_sku or f"TTDS-{product.id}-{v.id}"
        res = tt.create_product(
            title=product.title, description=product.description, category_id=category,
            image_uris=uris, warehouse_id=warehouse,
            skus=[{"seller_sku": v.seller_sku, "name": v.name, "price": v.price,
                   "quantity": v.listed_stock} for v in variants])
    except Exception as e:
        product.status, product.error = "error", str(e)[:500]
        log(db, f"Publishing '{product.title[:60]}' failed: {e}", "error", product_id=product.id)
        db.add(product)
        db.commit()
        raise
    product.tiktok_product_id = res["product_id"]
    product.tiktok_category_id = category
    product.status, product.error = "listed", ""
    for v in variants:
        v.tiktok_sku_id = res["skus"].get(v.seller_sku)
        db.add(v)
    db.add(product)
    log(db, f"Published '{product.title[:60]}' to TikTok (product {res['product_id']}). "
            "TikTok will review it before it goes live.", product_id=product.id)
    db.commit()
    return product


def sync_product(db: Session, s: Settings, product: Product, ae=None, tt=None, warehouse="") -> int:
    """Re-check AliExpress price/stock and push changes to TikTok. Returns number of changes."""
    if ae is None:
        ae = connections.aliexpress(db, s)
    if tt is None and product.tiktok_product_id and s.tiktok_mode == "api":
        tt, tok = connections.tiktok(db, s)
        warehouse = tok.extra.get("warehouse_id", "")
    changes = 0
    try:
        data = ae.get_product(product.ae_product_id)
        live = {v["sku_id"]: v for v in data["variants"]}
    except Exception as e:
        # Product removed from AliExpress (or API error): stop selling it to be safe.
        live = {}
        log(db, f"Could not fetch '{product.title[:60]}' from AliExpress: {e}. Setting stock to 0.",
            "warning", product_id=product.id)

    for v in variants_of(db, product.id):
        src = live.get(v.ae_sku_id)
        new_stock = listed_stock(src["stock"], s) if src else 0
        if src:
            v.ae_stock, v.cost = src["stock"], src["price"]
        new_price = selling_price(v.cost, v.shipping_cost, s, product.markup_multiplier)
        if s.tiktok_mode != "api" and product.status in ("listed", "paused"):
            # Files mode: just record the change; it goes in the price/stock file for you to upload.
            if new_stock != v.listed_stock or abs(new_price - v.price) >= 0.01:
                log(db, f"{product.title[:40]} / {v.name}: TikTok needs updating — price "
                        f"${v.price:.2f} → ${new_price:.2f}, stock {v.listed_stock} → {new_stock}",
                    "warning" if new_stock == 0 else "info", product_id=product.id)
                changes += 1
        elif tt and product.tiktok_product_id and v.tiktok_sku_id:
            if new_stock != v.listed_stock:
                tt.update_inventory(product.tiktok_product_id, v.tiktok_sku_id, new_stock, warehouse)
                log(db, f"{product.title[:40]} / {v.name}: stock {v.listed_stock} → {new_stock}",
                    product_id=product.id)
                changes += 1
            if abs(new_price - v.price) >= 0.01:
                tt.update_price(product.tiktok_product_id, v.tiktok_sku_id, new_price)
                log(db, f"{product.title[:40]} / {v.name}: price ${v.price:.2f} → ${new_price:.2f}",
                    product_id=product.id)
                changes += 1
        v.listed_stock, v.price = new_stock, new_price
        db.add(v)

    if product.status in ("listed", "paused"):
        product.status = "paused" if all(v.listed_stock == 0 for v in variants_of(db, product.id)) else "listed"
    product.last_checked_at = utcnow()
    db.add(product)
    db.commit()
    return changes


def sync_all_products(db: Session, s: Settings) -> int:
    products = list(db.exec(select(Product).where(Product.status.in_(["listed", "paused"]))))
    if not products:
        return 0
    ae = connections.aliexpress(db, s)
    tt, warehouse = None, ""
    if s.tiktok_mode == "api":
        tt, tok = connections.tiktok(db, s)
        warehouse = tok.extra.get("warehouse_id", "")
    total = 0
    for p in products:
        try:
            total += sync_product(db, s, p, ae, tt, warehouse)
        except Exception as e:
            db.rollback()
            log(db, f"Sync failed for '{p.title[:60]}': {e}", "error", product_id=p.id)
            db.commit()
    return total
