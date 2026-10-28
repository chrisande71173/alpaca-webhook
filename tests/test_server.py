import base64
from types import SimpleNamespace

import pytest

import server
from settings import SettingsStore

PASSWORD = "correct horse"


def basic_auth(password, username="admin"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKE_PROFIT_PERCENT", raising=False)
    monkeypatch.delenv("SELL_LIMIT_PERCENT", raising=False)
    fresh = SettingsStore(str(tmp_path / "settings.json"))
    monkeypatch.setattr(server, "settings", fresh)
    return fresh


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_PASSWORD", PASSWORD)
    server.app.config["TESTING"] = True
    return server.app.test_client()


# --- Password protection ---------------------------------------------------

def test_settings_page_is_off_without_an_admin_password(client, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_PASSWORD", None)

    for path in ("/settings", "/api/settings"):
        assert client.get(path, headers=basic_auth("anything")).status_code == 403


def test_settings_page_sends_you_to_the_login_page(client):
    response = client.get("/settings")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=/settings")
    # No browser password pop-up any more.
    assert "WWW-Authenticate" not in response.headers


def test_api_answers_401_instead_of_redirecting(client):
    response = client.get("/api/settings")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Sign in required"


def test_wrong_password_is_refused(client):
    assert client.get("/settings", headers=basic_auth("nope")).status_code == 302
    assert client.post(
        "/api/settings", json={"TRADE_PERCENT": 0.5}, headers=basic_auth("nope")
    ).status_code == 401


def test_right_password_shows_the_page(client):
    response = client.get("/settings", headers=basic_auth(PASSWORD))

    assert response.status_code == 200
    assert b"Trader settings" in response.data


# --- Login page --------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAILURE_DELAY", 0)


def test_login_page_shows_a_password_field(client):
    response = client.get("/login?next=/settings")

    assert response.status_code == 200
    assert b'type="password"' in response.data
    assert b'value="/settings"' in response.data


def test_signing_in_opens_the_page_you_asked_for(client):
    response = client.post("/login", data={"password": PASSWORD, "next": "/settings"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")
    assert client.get("/settings").status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_the_session_is_remembered(client):
    response = client.post("/login", data={"password": PASSWORD})

    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Expires=" in cookie  # kept across browser restarts (30 days)


def test_a_wrong_password_shows_an_error(client):
    response = client.post("/login", data={"password": "nope", "next": "/settings"})

    assert response.status_code == 401
    assert b"Wrong password." in response.data
    assert client.get("/settings").status_code == 302


def test_a_wrong_password_is_slowed_down(client, monkeypatch):
    delays = []
    monkeypatch.setattr(server, "LOGIN_FAILURE_DELAY", 1.0)
    monkeypatch.setattr(server.time, "sleep", delays.append)

    client.post("/login", data={"password": "nope"})

    assert delays == [1.0]


def test_login_only_redirects_within_the_site(client):
    for target in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
        response = client.post("/login", data={"password": PASSWORD, "next": target})
        assert response.headers["Location"].endswith("/settings"), target


def test_already_signed_in_skips_the_login_page(client):
    client.post("/login", data={"password": PASSWORD})

    response = client.get("/login")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")


def test_signing_out_ends_the_session(client):
    client.post("/login", data={"password": PASSWORD})

    response = client.get("/logout")

    assert response.headers["Location"].endswith("/login")
    assert client.get("/settings").status_code == 302


def test_login_page_says_when_the_settings_page_is_off(client, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_PASSWORD", None)

    get = client.get("/login")
    post = client.post("/login", data={"password": ""})

    assert get.status_code == 403
    assert b"ADMIN_PASSWORD" in get.data
    assert post.status_code == 403


def test_the_settings_page_has_a_sign_out_link(client):
    client.post("/login", data={"password": PASSWORD})

    assert b'href="/logout"' in client.get("/settings").data


def test_the_bare_domain_opens_the_settings_page(client):
    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/settings")


def test_any_username_works(client):
    assert client.get("/settings", headers=basic_auth(PASSWORD, "chris")).status_code == 200


def test_account_needs_the_password(client, monkeypatch):
    fake_account = SimpleNamespace(
        status="ACTIVE", cash="100", buying_power="200", portfolio_value="300"
    )
    monkeypatch.setattr(
        server, "trading_client", SimpleNamespace(get_account=lambda: fake_account)
    )

    assert client.get("/account").status_code == 401
    assert client.get("/account", headers=basic_auth("nope")).status_code == 401

    response = client.get("/account", headers=basic_auth(PASSWORD))
    assert response.status_code == 200
    assert response.get_json()["portfolio_value"] == "300"


def test_health_stays_open_for_uptime_checks(client):
    assert client.get("/health").status_code == 200


# --- Settings API ------------------------------------------------------------

def test_lists_every_setting_with_its_value_and_range(client):
    data = client.get("/api/settings", headers=basic_auth(PASSWORD)).get_json()

    assert data["paper"] is True
    trade = next(s for s in data["settings"] if s["key"] == "TRADE_PERCENT")
    assert trade == {
        "key": "TRADE_PERCENT",
        "label": "Trade size",
        "help": trade["help"],
        "kind": "percent",
        "integer": False,
        "minimum": 0.0001,
        "maximum": 1.0,
        "default": 0.01,
        "default_from_env": False,
        "value": 0.01,
    }


def test_a_default_set_by_an_env_var_is_shown_as_such(client, store, monkeypatch):
    # e.g. SELL_LIMIT_PERCENT=0.1 set on Railway before the settings page existed
    monkeypatch.setenv("SELL_LIMIT_PERCENT", "0.1")

    data = client.get("/api/settings", headers=basic_auth(PASSWORD)).get_json()

    sell_limit = next(s for s in data["settings"] if s["key"] == "SELL_LIMIT_PERCENT")
    assert sell_limit["default"] == 0.1
    assert sell_limit["default_from_env"] is True
    take_profit = next(s for s in data["settings"] if s["key"] == "TAKE_PROFIT_PERCENT")
    assert take_profit["default_from_env"] is False


def test_saving_changes_the_value_and_persists_it(client, store):
    response = client.post(
        "/api/settings", json={"TRADE_PERCENT": 0.02}, headers=basic_auth(PASSWORD)
    )

    assert response.status_code == 200
    assert store.get("TRADE_PERCENT") == 0.02
    assert SettingsStore(store.path).get("TRADE_PERCENT") == 0.02


def test_a_bad_value_is_rejected_and_nothing_changes(client, store):
    response = client.post(
        "/api/settings",
        json={"TRADE_PERCENT": 0.02, "POLL_INTERVAL": -1},
        headers=basic_auth(PASSWORD),
    )

    assert response.status_code == 400
    assert "Fill check interval" in response.get_json()["error"]
    assert store.get("TRADE_PERCENT") == 0.01


def test_only_json_is_accepted(client, store):
    # A cross-site HTML form can only send form data, so this blocks CSRF.
    response = client.post(
        "/api/settings", data={"TRADE_PERCENT": "0.5"}, headers=basic_auth(PASSWORD)
    )

    assert response.status_code == 415
    assert store.get("TRADE_PERCENT") == 0.01


def test_an_empty_change_is_rejected(client):
    response = client.post("/api/settings", json={}, headers=basic_auth(PASSWORD))

    assert response.status_code == 400


# --- The webhook uses the saved settings -------------------------------------

class FakeTradingClient:
    def __init__(self, buying_power=10_000):
        self.buying_power = buying_power
        self.orders = []

    def get_open_position(self, symbol):
        raise Exception("position does not exist")

    def get_orders(self, filter=None):
        return []

    def get_account(self):
        return SimpleNamespace(buying_power=str(self.buying_power))

    def submit_order(self, order):
        self.orders.append(order)
        return SimpleNamespace(id=f"order-{len(self.orders)}")


@pytest.fixture
def trading(monkeypatch):
    fake = FakeTradingClient()
    monkeypatch.setattr(server, "trading_client", fake)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    return fake


def buy(client, price=10.0):
    return client.post("/webhook", json={
        "secret": "test-webhook-secret", "symbol": "AAPL", "action": "buy", "price": price,
    })


def test_buy_size_follows_the_saved_trade_percent(client, store, trading, monkeypatch):
    monkeypatch.setattr(server, "is_market_open", lambda: True)
    store.update({"TRADE_PERCENT": 0.05})

    response = buy(client, price=10.0)

    assert response.status_code == 200
    # 5% of $10,000 = $500, at $10 a share = 50 shares.
    assert trading.orders[0].qty == 50


def test_extended_hours_buy_uses_the_saved_limit_buffer(client, store, trading, monkeypatch):
    monkeypatch.setattr(server, "is_market_open", lambda: False)
    store.update({"LIMIT_PRICE_BUFFER": 0.02})

    buy(client, price=10.0)

    assert trading.orders[0].limit_price == 10.2


def test_take_profit_uses_the_saved_percent(client, store, trading, monkeypatch):
    monkeypatch.setattr(server, "is_market_open", lambda: True)
    store.update({"TAKE_PROFIT_PERCENT": 0.1})

    response = buy(client, price=10.0)

    take_profit = trading.orders[1]
    assert take_profit.limit_price == 11.0
    assert response.get_json()["take_profit_order_id"] == "order-2"


def test_minimum_order_follows_the_saved_value(client, store, trading, monkeypatch):
    monkeypatch.setattr(server, "is_market_open", lambda: True)
    # 1% of $10,000 = $100, below a $150 minimum.
    store.update({"MIN_NOTIONAL": 150})

    response = buy(client, price=10.0)

    assert response.get_json()["status"] == "skipped"
    assert trading.orders == []
