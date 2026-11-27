"""Builds API clients from the stored OAuth tokens, refreshing them when needed."""
from datetime import datetime, timedelta

from sqlmodel import Session

from ..clients.aliexpress import AliExpressClient
from ..clients.mock import MockAliExpressClient, MockTikTokClient
from ..clients.tiktok import TikTokClient
from ..config import Settings
from ..db import Token, get_token, log, utcnow


class NotConnected(Exception):
    pass


def oauth_client(platform: str, s: Settings):
    if s.mock_mode:
        return MockTikTokClient() if platform == "tiktok" else MockAliExpressClient()
    return TikTokClient(s) if platform == "tiktok" else AliExpressClient(s)


def save_token(db: Session, platform: str, fields: dict) -> Token:
    tok = get_token(db, platform) or Token(platform=platform, access_token="")
    extra = {**(tok.extra or {}), **fields.pop("extra", {})}
    for k, v in fields.items():
        setattr(tok, k, v)
    tok.extra = extra
    tok.updated_at = utcnow()
    db.add(tok)
    db.commit()
    db.refresh(tok)
    return tok


def _fresh_token(db: Session, platform: str, s: Settings) -> Token:
    tok = get_token(db, platform)
    if not tok:
        raise NotConnected(f"{platform} is not connected yet (Settings → Connect).")
    if tok.expires_at and tok.expires_at - utcnow() < timedelta(days=1) and tok.refresh_token:
        try:
            tok = save_token(db, platform, oauth_client(platform, s).refresh(tok.refresh_token))
            log(db, f"Refreshed {platform} access token")
            db.commit()
        except Exception as e:  # keep using the old token; it may still be valid
            log(db, f"Could not refresh {platform} token: {e}", "error")
            db.commit()
    return tok


def tiktok(db: Session, s: Settings):
    tok = _fresh_token(db, "tiktok", s)
    if s.mock_mode:
        return MockTikTokClient(), tok
    return TikTokClient(s, tok.access_token, tok.extra.get("shop_cipher", "")), tok


def aliexpress(db: Session, s: Settings):
    tok = _fresh_token(db, "aliexpress", s)
    if s.mock_mode:
        return MockAliExpressClient()
    return AliExpressClient(s, tok.access_token)


def finish_tiktok_connect(db: Session, s: Settings, fields: dict) -> Token:
    """After OAuth: store the token, then look up the shop cipher and warehouse."""
    tok = save_token(db, "tiktok", fields)
    client = MockTikTokClient() if s.mock_mode else TikTokClient(s, tok.access_token)
    shops = client.get_shops()
    if not shops:
        raise NotConnected("TikTok authorized, but no shop was returned for this account.")
    shop = shops[0]
    if not s.mock_mode:
        client.shop_cipher = shop["cipher"]
    warehouses = [w for w in client.get_warehouses()
                  if w.get("type", "SALES_WAREHOUSE") == "SALES_WAREHOUSE"]
    default = next((w for w in warehouses if w.get("is_default")), warehouses[0] if warehouses else {})
    tok.extra = {**tok.extra, "shop_cipher": shop["cipher"], "shop_id": str(shop.get("id", "")),
                 "shop_name": shop.get("name", ""), "warehouse_id": str(default.get("id", ""))}
    db.add(tok)
    log(db, f"Connected TikTok Shop '{shop.get('name', '')}'")
    db.commit()
    return tok
