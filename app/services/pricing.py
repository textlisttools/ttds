"""Price, stock and profit rules."""
import math

from ..config import Settings

# Keep a small safety buffer so you don't sell the supplier's last units.
STOCK_BUFFER = 2

COUNTRY_NAMES = {
    "US": {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"},
    "GB": {"GB", "UK", "UNITED KINGDOM", "GREAT BRITAIN"},
    "CA": {"CA", "CANADA"},
    "AU": {"AU", "AUSTRALIA"},
    "DE": {"DE", "GERMANY"},
    "FR": {"FR", "FRANCE"},
    "ES": {"ES", "SPAIN"},
    "IT": {"IT", "ITALY"},
    "CN": {"CN", "CHINA", "CHINA MAINLAND"},
}


def same_country(code: str, value: str) -> bool:
    v = (value or "").strip().upper()
    return v in COUNTRY_NAMES.get(code.upper(), {code.upper()})


def selling_price(cost: float, shipping: float, s: Settings, multiplier: float | None = None) -> float:
    """(cost + shipping) * multiplier + fixed, rounded up to the next .99."""
    raw = (cost + shipping) * (multiplier or s.markup_multiplier) + s.markup_fixed
    return round(max(math.ceil(raw) - 0.01, 0.99), 2)


def listed_stock(supplier_stock: int, s: Settings) -> int:
    return max(0, min(supplier_stock - STOCK_BUFFER, s.max_listed_stock))


def profit(sale_total: float, cost_total: float, s: Settings) -> float:
    return round(sale_total * (1 - s.tiktok_fee_rate) - cost_total, 2)
