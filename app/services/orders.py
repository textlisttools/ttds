"""TikTok order intake, AliExpress order placement, and tracking upload."""
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..config import Settings
from ..db import Order, Product, Variant, log, utcnow
from . import connections
from .pricing import profit

OPEN = ("needs_approval", "held", "error")


def _find_variant(db: Session, item: dict) -> Variant | None:
    if item.get("seller_sku"):
        v = db.exec(select(Variant).where(Variant.seller_sku == item["seller_sku"])).first()
        if v:
            return v
    if item.get("tiktok_sku_id"):
        return db.exec(select(Variant).where(Variant.tiktok_sku_id == item["tiktok_sku_id"])).first()
    return None


def _evaluate(db: Session, s: Settings, order: Order) -> None:
    """Fill in cost/profit and decide if the order is held."""
    cost, reasons = 0.0, []
    for it in order.items:
        v = db.get(Variant, it["variant_id"]) if it.get("variant_id") else None
        if not v:
            reasons.append(f"'{it['title'][:40]}' is not a product imported here")
            continue
        cost += (v.cost + v.shipping_cost) * it["quantity"]
        if v.ae_stock < it["quantity"]:
            reasons.append(f"AliExpress stock for '{v.name}' is {v.ae_stock}")
    a = order.address or {}
    if not a.get("address") or any("*" in str(a.get(k, "")) for k in ("full_name", "address", "zip")):
        reasons.append("the buyer's address is hidden or missing — copy it from the order in "
                       "Seller Center into 'Ship to' below")
    order.est_cost = round(cost, 2)
    order.est_profit = profit(order.sale_total, cost, s)
    if not reasons and order.est_profit < s.min_profit:
        reasons.append(f"estimated profit ${order.est_profit:.2f} is below your ${s.min_profit:.2f} minimum")
    order.hold_reason = "; ".join(reasons)


def sync_orders(db: Session, s: Settings) -> int:
    """Pull new 'awaiting shipment' orders from TikTok. Returns how many were new."""
    if s.tiktok_mode != "api":
        return 0  # files mode: orders arrive when you upload the Seller Center export
    tt, _ = connections.tiktok(db, s)
    since = int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp())
    remote = tt.search_orders("AWAITING_SHIPMENT", created_after=since)
    remote_ids = {o["id"] for o in remote}
    new = 0
    for o in remote:
        if db.exec(select(Order).where(Order.tiktok_order_id == o["id"])).first():
            continue
        items = []
        for it in o["items"]:
            v = _find_variant(db, it)
            items.append({**it, "variant_id": v.id if v else None})
        order = Order(tiktok_order_id=o["id"], buyer_name=o["buyer_name"], address=o["address"],
                      items=items, sale_total=o["total"], ship_by=o["ship_by"],
                      ae_order_ids=[], carrier="", tracking_number="")
        order.address["delivery_option_id"] = o.get("delivery_option_id", "")
        _evaluate(db, s, order)
        order.status = "held" if order.hold_reason else "needs_approval"
        db.add(order)
        db.flush()
        log(db, f"New TikTok order {o['id']} — ${o['total']:.2f}, est. profit ${order.est_profit:.2f}"
                + (f" — HELD: {order.hold_reason}" if order.hold_reason else ""),
            "warning" if order.hold_reason else "info", order_id=order.id)
        db.commit()
        new += 1
        if s.auto_order and order.status == "needs_approval":
            place_order(db, s, order)

    # Orders we still have open that left "awaiting shipment" may have been cancelled.
    for order in db.exec(select(Order).where(Order.status.in_(OPEN))):
        if order.tiktok_order_id not in remote_ids:
            status = tt.get_order_status(order.tiktok_order_id)
            if status == "CANCELLED":
                order.status, order.updated_at = "cancelled", utcnow()
                db.add(order)
                log(db, f"TikTok order {order.tiktok_order_id} was cancelled — nothing ordered.",
                    order_id=order.id)
    db.commit()
    return new


def place_order(db: Session, s: Settings, order: Order, force: bool = False) -> Order:
    """Buy the item(s) on AliExpress, shipped to the TikTok buyer.

    force=True places a held order anyway (the "Approve anyway" button).
    """
    if order.ae_order_ids:
        raise ValueError("This order already has an AliExpress order — not placing it twice.")
    if order.status not in OPEN:
        raise ValueError(f"Order is '{order.status}', it can't be placed.")
    ae = connections.aliexpress(db, s)

    # Re-check the live supplier price right before paying.
    for it in order.items:
        v = db.get(Variant, it["variant_id"]) if it.get("variant_id") else None
        if v:
            p = db.get(Product, v.product_id)
            live = {x["sku_id"]: x for x in ae.get_product(p.ae_product_id)["variants"]}
            if v.ae_sku_id in live:
                v.cost, v.ae_stock = live[v.ae_sku_id]["price"], live[v.ae_sku_id]["stock"]
                db.add(v)
    _evaluate(db, s, order)
    if order.hold_reason and not force:
        order.status = "held"
        db.add(order)
        log(db, f"Order {order.tiktok_order_id} held: {order.hold_reason}", "warning", order_id=order.id)
        db.commit()
        return order
    if any(not it.get("variant_id") for it in order.items):
        raise ValueError("Order has items that aren't linked to an AliExpress product.")

    order.status = "placing"
    db.add(order)
    db.commit()
    lines = []
    for it in order.items:
        v = db.get(Variant, it["variant_id"])
        p = db.get(Product, v.product_id)
        lines.append({"product_id": p.ae_product_id, "sku_attr": v.ae_sku_attr,
                      "quantity": it["quantity"], "logistics_service": s.aliexpress_logistics_service})
    try:
        ids = ae.place_order(order.tiktok_order_id, order.address, lines)
    except Exception as e:
        order.status, order.error = "error", str(e)[:500]
        db.add(order)
        log(db, f"AliExpress order for {order.tiktok_order_id} failed: {e}", "error", order_id=order.id)
        db.commit()
        return order
    order.ae_order_ids, order.status, order.error = ids, "placed", ""
    order.updated_at = utcnow()
    db.add(order)
    log(db, f"Placed AliExpress order {', '.join(ids)} for TikTok order {order.tiktok_order_id}. "
            "If it shows 'Awaiting payment' on AliExpress, pay it there.", order_id=order.id)
    db.commit()
    return order


def _match_provider(providers: list[dict], carrier: str) -> str | None:
    c = (carrier or "").lower().replace(" ", "")
    if not c:
        return None
    for p in providers:
        name = p.get("name", "").lower().replace(" ", "")
        if name and (name in c or c in name):
            return str(p["id"])
    return None


def upload_tracking(db: Session, s: Settings, order: Order, tracking_number: str,
                    carrier: str, provider_id: str | None = None) -> bool:
    if s.tiktok_mode != "api":
        # Files mode: queue it for the tracking file you upload in Seller Center.
        order.tracking_number, order.carrier = tracking_number, carrier
        order.status, order.error, order.updated_at = "tracking_ready", "", utcnow()
        db.add(order)
        log(db, f"Tracking {tracking_number} ({carrier}) ready to upload to TikTok", order_id=order.id)
        db.commit()
        return True
    tt, _ = connections.tiktok(db, s)
    if not provider_id:
        providers = tt.get_shipping_providers(order.address.get("delivery_option_id", ""))
        provider_id = _match_provider(providers, carrier)
    order.tracking_number, order.carrier = tracking_number, carrier
    if not provider_id:
        order.error = (f"Carrier '{carrier}' isn't in TikTok's carrier list. "
                       "Enter the tracking manually on the order page.")
        db.add(order)
        log(db, f"Order {order.tiktok_order_id}: {order.error}", "warning", order_id=order.id)
        db.commit()
        return False
    line_ids = [lid for it in order.items for lid in it.get("tiktok_line_ids", [])]
    tt.mark_shipped(order.tiktok_order_id, line_ids, tracking_number, provider_id)
    order.status, order.error, order.updated_at = "shipped", "", utcnow()
    db.add(order)
    log(db, f"Uploaded tracking {tracking_number} ({carrier}) to TikTok order {order.tiktok_order_id}",
        order_id=order.id)
    db.commit()
    return True


def sync_tracking(db: Session, s: Settings) -> int:
    """For placed orders, fetch AliExpress tracking and upload it to TikTok."""
    placed = list(db.exec(select(Order).where(Order.status == "placed")))
    if not placed:
        return 0
    ae = connections.aliexpress(db, s)
    done = 0
    for order in placed:
        if order.error and order.tracking_number:
            continue  # waiting for manual carrier selection
        for ae_id in order.ae_order_ids:
            info = ae.get_tracking(ae_id)
            if info and s.tiktok_mode != "api":
                order.tracking_number, order.carrier = info["tracking_number"], info["carrier"]
                order.status, order.updated_at = "tracking_ready", utcnow()
                db.add(order)
                log(db, f"Supplier shipped TikTok order {order.tiktok_order_id}: {info['carrier']} "
                        f"{info['tracking_number']} — ready to upload to TikTok", order_id=order.id)
                db.commit()
                done += 1
                break
            if info:
                try:
                    done += upload_tracking(db, s, order, info["tracking_number"], info["carrier"])
                except Exception as e:
                    db.rollback()
                    log(db, f"Tracking upload failed for {order.tiktok_order_id}: {e}", "error",
                        order_id=order.id)
                    db.commit()
                break
    return done


def link_item(db: Session, s: Settings, order: Order, index: int, variant_id: int) -> Order:
    """Connect an order line to one of your imported variants (and remember it for next time)."""
    v = db.get(Variant, variant_id)
    if not v or not (0 <= index < len(order.items)):
        raise ValueError("Unknown item or variant.")
    items = [dict(i) for i in order.items]
    items[index]["variant_id"] = v.id
    if items[index].get("tiktok_sku_id") and not v.tiktok_sku_id:
        v.tiktok_sku_id = items[index]["tiktok_sku_id"]
        db.add(v)
    order.items = items
    _evaluate(db, s, order)
    if order.status in ("held", "needs_approval", "error") and not order.ae_order_ids:
        order.status = "held" if order.hold_reason else "needs_approval"
    db.add(order)
    log(db, f"Linked '{items[index]['title'][:40]}' to {v.name} (seller SKU {v.seller_sku})", order_id=order.id)
    db.commit()
    return order


def update_address(db: Session, s: Settings, order: Order, fields: dict) -> Order:
    if order.ae_order_ids:
        raise ValueError("Already ordered on AliExpress — change the address there.")
    order.address = {**order.address, **{k: v.strip() for k, v in fields.items()}}
    order.buyer_name = order.address.get("full_name", order.buyer_name)
    _evaluate(db, s, order)
    if order.status in ("held", "needs_approval"):
        order.status = "held" if order.hold_reason else "needs_approval"
    db.add(order)
    log(db, "Shipping address updated", order_id=order.id)
    db.commit()
    return order


def at_risk(order: Order, s: Settings) -> bool:
    if order.status in ("shipped", "cancelled") or not order.ship_by:
        return False
    return order.ship_by - utcnow() < timedelta(hours=s.deadline_warning_hours)
