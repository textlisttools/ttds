"""All settings come from environment variables or the .env file.

Copy .env.example to .env and fill it in. Never commit .env.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General -------------------------------------------------------------
    # MOCK_MODE=true uses fake TikTok/AliExpress clients so you can try the
    # dashboard before you have API keys. Set to false to use the real APIs.
    mock_mode: bool = True
    # Public URL where this app is reachable (used for OAuth redirect URLs).
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///ttds.db"
    # Dashboard login. CHANGE THESE before putting the app on the internet.
    dashboard_username: str = "admin"
    dashboard_password: str = "change-me"
    session_secret: str = "change-this-to-a-long-random-string"

    # --- TikTok Shop (Partner Center -> your app) -----------------------------
    tiktok_app_key: str = ""
    tiktok_app_secret: str = ""
    # The "service id" shown on your app page; used to build the authorize link.
    tiktok_service_id: str = ""
    tiktok_api_base: str = "https://open-api.tiktokglobalshop.com"
    tiktok_auth_base: str = "https://auth.tiktok-shops.com"
    # US sellers: https://services.us.tiktokshop.com ; other regions: https://services.tiktokshop.com
    tiktok_authorize_base: str = "https://services.us.tiktokshop.com"
    tiktok_currency: str = "USD"

    # --- AliExpress Open Platform (openservice.aliexpress.com) ----------------
    aliexpress_app_key: str = ""
    aliexpress_app_secret: str = ""
    aliexpress_api_base: str = "https://api-sg.aliexpress.com"
    # Country your TikTok buyers are in (used for shipping/stock lookups).
    ship_to_country: str = "US"
    aliexpress_currency: str = "USD"
    aliexpress_language: str = "en"
    # Name of the AliExpress shipping method to request; blank lets AliExpress choose.
    aliexpress_logistics_service: str = ""

    # --- Business rules -------------------------------------------------------
    # Selling price = (supplier cost + shipping) * markup_multiplier + markup_fixed
    markup_multiplier: float = 1.8
    markup_fixed: float = 2.0
    # TikTok Shop fees as a fraction of the sale (commission + payment), for profit checks.
    tiktok_fee_rate: float = 0.08
    # Orders with an estimated profit below this are held for review, never auto-placed.
    min_profit: float = 3.0
    # false = every AliExpress order waits for you to click Approve (recommended).
    auto_order: bool = False
    # Only import products that ship from this country (protects your ship-by deadline).
    require_local_warehouse: bool = True
    # Max delivery days you accept from a supplier when importing.
    max_delivery_days: int = 8
    # Hours before TikTok's ship-by deadline when an order is flagged "at risk".
    deadline_warning_hours: int = 24
    # How many units to show on TikTok when AliExpress has plenty (never show more than this).
    max_listed_stock: int = 20

    # --- Schedules (minutes) --------------------------------------------------
    order_poll_minutes: int = 10
    tracking_poll_minutes: int = 60
    monitor_poll_minutes: int = 180


@lru_cache
def get_settings() -> Settings:
    return Settings()
