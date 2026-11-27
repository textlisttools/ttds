from fastapi.testclient import TestClient

from app import settings_store
from app.settings_store import current_settings


def form(**over):
    s = current_settings()
    f = {r.key: str(settings_store.display_value(s, r)) for r in settings_store.RULES
         if r.kind is not bool}
    f.update(over)
    return f


def test_save_overrides_env_defaults():
    assert current_settings().auto_order is False
    errors = settings_store.save(form(auto_order="on", markup_multiplier="2.5", tiktok_fee_rate="10",
                                      ship_to_country="gb"))
    assert errors == []
    s = current_settings()
    assert s.auto_order is True and s.markup_multiplier == 2.5
    assert s.tiktok_fee_rate == 0.1  # entered as a percentage
    assert s.ship_to_country == "GB"
    # Unchecked checkbox = off
    settings_store.save(form())
    assert current_settings().auto_order is False
    settings_store.reset()
    assert current_settings().markup_multiplier == 1.8


def test_invalid_values_save_nothing():
    errors = settings_store.save(form(markup_multiplier="abc", max_delivery_days="999",
                                      ship_to_country="USA", min_profit="7"))
    assert len(errors) == 3
    assert current_settings().min_profit == 3.0  # nothing saved


def test_settings_page_round_trip():
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "test-pass"})
        r = c.get("/settings")
        assert "Your rules" in r.text and "Automatic ordering" in r.text
        r = c.post("/settings", data=form(min_profit="5.5", auto_order="on"))
        assert "Settings saved" in r.text and "Automatic ordering is ON" in r.text
        assert current_settings().min_profit == 5.5
        assert 'value="5.5"' in r.text
        r = c.post("/settings", data=form(min_profit="-1"))
        assert "Nothing was saved" in r.text
        r = c.post("/settings/reset")
        assert current_settings().auto_order is False
