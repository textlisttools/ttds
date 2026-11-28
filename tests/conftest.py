import os

os.environ["TTDS_NO_SCHEDULER"] = "1"
os.environ["MOCK_MODE"] = "true"
os.environ["DASHBOARD_PASSWORD"] = "test-pass"

import pytest
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import db
from app.clients import mock
from app.config import get_settings


@pytest.fixture(autouse=True)
def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db.set_engine(engine)
    SQLModel.metadata.create_all(engine)
    mock.STATE.update({"tt_products": {}, "tt_orders": [], "ae_orders": {}})
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def api_settings():
    """Settings for the direct TikTok API mode (files mode is the default)."""
    return get_settings().model_copy(update={"tiktok_mode": "api"})
