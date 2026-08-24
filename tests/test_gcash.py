from types import SimpleNamespace

from platforms.chatgpt import gcash
from platforms.chatgpt import plugin as plugin_module
from platforms.chatgpt import subscription
from core.base_platform import RegisterConfig
from core.proxy_pool import proxy_pool


def test_gcash_method_requires_the_known_openai_payment_method_id():
    assert gcash._gcash_method([
        {"id": "cpmt_other", "name": "gcash"},
    ]) == ""
    assert gcash._gcash_method([
        {"id": next(iter(gcash.KNOWN_GCASH_METHOD_IDS))},
    ])


def test_check_gcash_creates_ph_php_checkout_and_only_reads_methods(monkeypatch):
    calls = []
    known_id = next(iter(gcash.KNOWN_GCASH_METHOD_IDS))

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.cookies = {}

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            if "/api/auth/csrf" in url:
                return FakeResponse({})
            return FakeResponse({"custom_payment_methods": [{"id": known_id}]})

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return FakeResponse({"checkout_session_id": "oaics_test_session", "processor_entity": "openai_ie"})

        def close(self):
            return None

    class FakeSentinel:
        def __init__(self, *args, **kwargs):
            pass

        def build_headers(self, device_id, flow):
            return {"openai-sentinel-token": "{}"}

        def close(self):
            return None

    monkeypatch.setattr(gcash, "_session", lambda proxy: FakeSession())
    monkeypatch.setattr(gcash, "OpenAISentinelClient", FakeSentinel)

    account = SimpleNamespace(
        access_token="token",
        cookies="",
        account_id="acct-123",
    )
    result = gcash.check_gcash(account, "ph.example:8080")

    assert result["gcash_eligible"] is True
    checkout = next(call for call in calls if call[0] == "post" and "payments/checkout" in call[1])
    assert checkout[2]["json"]["billing_details"] == {"country": "PH", "currency": "PHP"}
    assert not any("confirm" in call[1] or "start" in call[1] for call in calls)


def test_chatgpt_check_does_not_run_gcash_qualification(monkeypatch):
    monkeypatch.setattr(
        subscription,
        "fetch_subscription_status_details",
        lambda account, proxy=None: {"status": "free", "source": "test"},
    )
    monkeypatch.setattr(
        subscription,
        "fetch_plus_trial_checkout_chain",
        lambda account, proxy=None: {"plus_trial_checkout_state": "unavailable"},
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "ph.example:8080")
    monkeypatch.setattr(gcash, "check_gcash", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GCash must be a separate task")))

    plugin = plugin_module.ChatGPTPlatform.__new__(plugin_module.ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = SimpleNamespace(
        token="token",
        region="",
        extra={"access_token": "token"},
    )

    assert plugin.check_valid(account) is True
    overview = plugin.get_last_check_overview()
    assert "gcash_eligible" not in overview
    assert "GCash" not in overview["chips"]
