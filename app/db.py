"""Database tables (SQLite by default)."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Token(SQLModel, table=True):
    """OAuth tokens for TikTok Shop and AliExpress (one row per platform)."""
    platform: str = Field(primary_key=True)  # "tiktok" | "aliexpress"
    access_token: str
    refresh_token: str = ""
    expires_at: Optional[datetime] = None
    refresh_expires_at: Optional[datetime] = None
    # TikTok: shop_cipher, shop_id, shop_name, warehouse_id. AliExpress: user_nick.
    extra: dict = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ae_product_id: str = Field(index=True)
    ae_url: str = ""
    title: str
    description: str = ""
    images: list = Field(default_factory=list, sa_column=Column(JSON))
    ship_from: str = ""
    tiktok_product_id: Optional[str] = Field(default=None, index=True)
    tiktok_category_id: Optional[str] = None
    # draft -> listed | paused (out of stock / unprofitable) | error
    status: str = "draft"
    error: str = ""
    markup_multiplier: Optional[float] = None  # per-product override
    created_at: datetime = Field(default_factory=utcnow)
    last_checked_at: Optional[datetime] = None


class Variant(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    ae_sku_id: str
    ae_sku_attr: str = ""  # passed to AliExpress when ordering, e.g. "14:200006156#Red"
    name: str = "Default"
    image: str = ""
    cost: float  # supplier item price
    shipping_cost: float = 0.0
    delivery_days: Optional[int] = None
    ae_stock: int = 0
    price: float  # our TikTok selling price
    listed_stock: int = 0
    tiktok_sku_id: Optional[str] = Field(default=None, index=True)
    seller_sku: str = ""  # we use this to map TikTok order lines back to the variant


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tiktok_order_id: str = Field(index=True, unique=True)
    # needs_approval -> placing -> placed (awaiting AliExpress payment/shipping)
    #   -> shipped (tracking uploaded to TikTok)
    # held = blocked by a rule (low profit, unknown item...) and needs you to look
    # cancelled | error
    status: str = "needs_approval"
    hold_reason: str = ""
    buyer_name: str = ""
    address: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # [{tiktok_line_ids, seller_sku, tiktok_sku_id, quantity, sale_price, variant_id, title}]
    items: list = Field(default_factory=list, sa_column=Column(JSON))
    sale_total: float = 0.0
    est_cost: float = 0.0
    est_profit: float = 0.0
    ship_by: Optional[datetime] = None
    ae_order_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    tracking_number: str = ""
    carrier: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    """Activity log shown on the dashboard."""
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    level: str = "info"  # info | warning | error
    message: str
    order_id: Optional[int] = None
    product_id: Optional[int] = None


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=args)
    return _engine


def set_engine(engine) -> None:
    """Used by tests to point the app at a throwaway database."""
    global _engine
    _engine = engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


def session() -> Session:
    return Session(get_engine())


def log(s: Session, message: str, level: str = "info", order_id=None, product_id=None) -> None:
    s.add(Event(message=message, level=level, order_id=order_id, product_id=product_id))


def get_token(s: Session, platform: str) -> Optional[Token]:
    return s.exec(select(Token).where(Token.platform == platform)).first()
