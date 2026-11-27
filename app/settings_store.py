"""Business rules you can edit on the Settings page.

Values saved on the page are stored in the database and override the defaults
from .env. Secrets (API keys, passwords) are deliberately NOT editable here;
they stay in .env / your host's environment variables.
"""
from dataclasses import dataclass
from typing import Any

from sqlmodel import Field, SQLModel, select

from .config import Settings, get_settings


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    help: str
    kind: type  # bool | int | float | str
    group: str
    min: float | None = None
    max: float | None = None
    step: str = "any"
    unit: str = ""


RULES: list[Rule] = [
    Rule("auto_order", "Automatic ordering",
         "On: profitable orders are bought on AliExpress automatically. Off: you click Approve on each "
         "order. Orders that break a rule are always held for you either way.", bool, "Ordering"),
    Rule("min_profit", "Minimum profit per order",
         "Orders with a lower estimated profit (after TikTok fees) are held for review.",
         float, "Ordering", 0, 10000, "0.01", "$"),
    Rule("tiktok_fee_rate", "TikTok fees",
         "Commission + payment fee, as a percentage of the sale. Check your Seller Center fee page.",
         float, "Ordering", 0, 50, "0.1", "%"),
    Rule("markup_multiplier", "Markup multiplier",
         "Price = (AliExpress cost + shipping) × this + fixed markup, rounded up to .99.",
         float, "Pricing", 1, 20, "0.05", "×"),
    Rule("markup_fixed", "Fixed markup", "Added to every price after the multiplier.",
         float, "Pricing", 0, 1000, "0.01", "$"),
    Rule("max_listed_stock", "Max stock shown on TikTok",
         "Never show more units than this, even if the supplier has more.", int, "Pricing", 0, 9999, "1"),
    Rule("require_local_warehouse", "Only import items shipping from my country",
         "Strongly recommended, so you can meet TikTok's ship-by deadlines.", bool, "Importing"),
    Rule("ship_to_country", "My country (2-letter code)",
         "Where your buyers are, e.g. US, GB, CA.", str, "Importing"),
    Rule("max_delivery_days", "Max delivery days",
         "Skip product variants that take longer than this to arrive.", int, "Importing", 1, 60, "1", "days"),
    Rule("aliexpress_logistics_service", "AliExpress shipping method",
         "Leave blank to use the cheapest/fastest option. Advanced: an exact AliExpress service code.",
         str, "Importing"),
    Rule("deadline_warning_hours", "Deadline warning",
         "Flag orders this many hours before TikTok's ship-by deadline.", int, "Schedules", 1, 168, "1", "hours"),
    Rule("order_poll_minutes", "Check for new orders every", "", int, "Schedules", 5, 1440, "1", "min"),
    Rule("tracking_poll_minutes", "Check for tracking every", "", int, "Schedules", 15, 1440, "1", "min"),
    Rule("monitor_poll_minutes", "Re-check prices & stock every",
         "AliExpress limits how many calls you can make; don't go much below 60.",
         int, "Schedules", 30, 1440, "1", "min"),
]
RULES_BY_KEY = {r.key: r for r in RULES}
# Shown as a percentage on the page but stored as a fraction.
PERCENT = {"tiktok_fee_rate"}


def _parse(rule: Rule, raw: str) -> Any:
    if rule.kind is bool:
        return raw.lower() in ("1", "true", "on", "yes")
    if rule.kind is str:
        v = raw.strip()
        return v.upper() if rule.key == "ship_to_country" else v
    return rule.kind(raw)


def current_settings() -> Settings:
    """Settings from .env, with anything saved on the Settings page applied on top."""
    from .db import session  # local import avoids a cycle

    base = get_settings()
    try:
        with session() as db:
            rows = db.exec(select(AppSetting)).all()
    except Exception:  # table not created yet
        return base
    update = {}
    for row in rows:
        rule = RULES_BY_KEY.get(row.key)
        if rule:
            try:
                update[row.key] = _parse(rule, row.value)
            except ValueError:
                pass
    return base.model_copy(update=update)


def display_value(s: Settings, rule: Rule) -> Any:
    v = getattr(s, rule.key)
    return round(v * 100, 2) if rule.key in PERCENT else v


def save(form: dict) -> list[str]:
    """Validate and store the submitted form. Returns error messages (empty = saved)."""
    from .db import session

    errors, values = [], {}
    for rule in RULES:
        if rule.kind is bool:
            values[rule.key] = "true" if form.get(rule.key) in ("on", "true", "1") else "false"
            continue
        raw = str(form.get(rule.key, "")).strip()
        if rule.kind is str:
            if rule.key == "ship_to_country" and (len(raw) != 2 or not raw.isalpha()):
                errors.append(f"{rule.label}: use a 2-letter code like US.")
            values[rule.key] = raw.upper() if rule.key == "ship_to_country" else raw
            continue
        try:
            num = rule.kind(raw)
        except ValueError:
            errors.append(f"{rule.label}: '{raw}' is not a number.")
            continue
        if (rule.min is not None and num < rule.min) or (rule.max is not None and num > rule.max):
            errors.append(f"{rule.label}: must be between {rule.min:g} and {rule.max:g}.")
            continue
        values[rule.key] = str(num / 100 if rule.key in PERCENT else num)
    if errors:
        return errors
    with session() as db:
        for k, v in values.items():
            row = db.get(AppSetting, k) or AppSetting(key=k, value=v)
            row.value = v
            db.add(row)
        db.commit()
    return []


def reset() -> None:
    from .db import session

    with session() as db:
        for row in db.exec(select(AppSetting)).all():
            db.delete(row)
        db.commit()
