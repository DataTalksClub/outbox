import pytest
from starlette.testclient import TestClient

from outbox.app import create_app
from outbox.config import Settings

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def make_client(tmp_path):
    def make(**overrides):
        settings = Settings(data_dir=tmp_path / "data", **overrides)
        return TestClient(create_app(settings))

    return make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def auth():
    return dict(AUTH)
