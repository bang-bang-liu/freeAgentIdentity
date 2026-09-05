"""邮箱来源重建的降级矩阵测试。

对应设计 13.1 新增项：provider 已禁用、缺少 mailbox_provider_key、缺少
api_url、地址 ID 缺失，四种情况都必须落到"可手动继续"而不是抛未捕获异常。
"""
from types import SimpleNamespace

from platforms.chatgpt import mailbox_rebuild


def _graph(
    *,
    provider_name: str = "icloud_workbench",
    email: str = "old@example.com",
    address_id: str = "addr_123",
    api_url: str = "https://mail.example.com/token/abc/latest",
) -> dict:
    provider_accounts = []
    if provider_name or email:
        credentials = {"api_url": api_url} if api_url else {}
        provider_accounts.append(
            {
                "provider_type": "mailbox",
                "provider_name": provider_name,
                "login_identifier": email,
                "credentials": credentials,
                "metadata": {},
            }
        )
    provider_resources = []
    if address_id:
        provider_resources.append(
            {
                "provider_type": "mailbox",
                "provider_name": provider_name,
                "resource_type": "mailbox",
                "resource_identifier": address_id,
                "handle": email,
                "metadata": {},
            }
        )
    return {
        "overview": {},
        "credentials": [],
        "provider_accounts": provider_accounts,
        "provider_resources": provider_resources,
    }


def _factory(recorder: list):
    def factory(*, provider: str, extra: dict, proxy=None):
        recorder.append((provider, dict(extra or {}), proxy))
        return SimpleNamespace(name=f"mailbox:{provider}")

    return factory


def test_resolve_reads_all_four_values_from_saved_graph():
    source = mailbox_rebuild.resolve_mailbox_source(_graph())
    assert source == {
        "provider_name": "icloud_workbench",
        "email": "old@example.com",
        "address_id": "addr_123",
        "api_url": "https://mail.example.com/token/abc/latest",
    }


def test_rebuild_injects_mailbox_provider_key_explicitly():
    """核心约束：该键只在 get_email() 内部被设置，刷新路径必须自己注入，
    否则 FallbackMailbox._resolve() 会抛"未找到邮箱 provider 上下文"。"""
    calls: list = []
    built = mailbox_rebuild.rebuild_mailbox(_graph(), mailbox_factory=_factory(calls))

    assert built.ok is True
    assert built.account is not None
    assert built.account.extra["mailbox_provider_key"] == "icloud_workbench"
    assert built.account.email == "old@example.com"
    assert built.account.account_id == "addr_123"


def test_rebuild_carries_api_url_for_public_inbox_read():
    calls: list = []
    built = mailbox_rebuild.rebuild_mailbox(_graph(), mailbox_factory=_factory(calls))

    credentials = built.account.extra["provider_account"]["credentials"]
    assert credentials["api_url"] == "https://mail.example.com/token/abc/latest"


def test_rebuild_without_api_url_still_succeeds_for_catchall_provider():
    """域名 IMAP 靠 catch-all 收信，没有 api_url 不代表不可用。"""
    calls: list = []
    graph = _graph(provider_name="domain_imap", api_url="")
    built = mailbox_rebuild.rebuild_mailbox(graph, mailbox_factory=_factory(calls))

    assert built.ok is True
    assert "provider_account" not in built.account.extra
    assert built.account.extra["mailbox_provider_key"] == "domain_imap"


def test_rebuild_without_address_id_still_succeeds():
    calls: list = []
    built = mailbox_rebuild.rebuild_mailbox(
        _graph(address_id=""), mailbox_factory=_factory(calls)
    )

    assert built.ok is True
    assert built.account.account_id == ""
    assert "provider_resource" not in built.account.extra


def test_rebuild_soft_fails_when_provider_missing():
    built = mailbox_rebuild.rebuild_mailbox(
        _graph(provider_name="", address_id=""), mailbox_factory=_factory([])
    )

    assert built.ok is False
    assert "手动输入验证码" in built.reason


def test_rebuild_soft_fails_when_email_missing():
    built = mailbox_rebuild.rebuild_mailbox(
        _graph(email="", address_id=""), mailbox_factory=_factory([])
    )

    assert built.ok is False
    assert built.provider_name == "icloud_workbench"
    assert "手动输入验证码" in built.reason


def test_rebuild_soft_fails_when_factory_raises_disabled_provider():
    """provider 被禁用时 create_mailbox 会抛错，这里必须软失败而不是冒泡。"""

    def failing_factory(*, provider: str, extra: dict, proxy=None):
        raise RuntimeError(f"邮箱 provider 不存在、未启用或不受支持: {provider}")

    built = mailbox_rebuild.rebuild_mailbox(_graph(), mailbox_factory=failing_factory)

    assert built.ok is False
    assert built.provider_name == "icloud_workbench"
    assert built.email == "old@example.com"
    assert "手动输入验证码" in built.reason


def test_rebuild_passes_proxy_through_to_factory():
    calls: list = []
    mailbox_rebuild.rebuild_mailbox(
        _graph(), proxy="http://127.0.0.1:7897", mailbox_factory=_factory(calls)
    )

    assert calls[0][0] == "icloud_workbench"
    assert calls[0][2] == "http://127.0.0.1:7897"


def test_resolve_falls_back_to_resource_handle_and_metadata():
    graph = {
        "provider_accounts": [],
        "provider_resources": [
            {
                "provider_type": "mailbox",
                "provider_name": "icloud_workbench",
                "resource_identifier": "addr_9",
                "handle": "fallback@example.com",
                "metadata": {"api_url": "https://mail.example.com/token/z/latest"},
            }
        ],
    }
    source = mailbox_rebuild.resolve_mailbox_source(graph)

    assert source["email"] == "fallback@example.com"
    assert source["address_id"] == "addr_9"
    assert source["api_url"] == "https://mail.example.com/token/z/latest"


def test_resolve_ignores_non_mailbox_provider_entries():
    graph = {
        "provider_accounts": [
            {
                "provider_type": "sms",
                "provider_name": "some_sms",
                "login_identifier": "+100000",
                "credentials": {"api_url": "https://sms.example.com"},
            }
        ],
        "provider_resources": [],
    }
    source = mailbox_rebuild.resolve_mailbox_source(graph)

    assert source["provider_name"] == ""
    assert source["email"] == ""
    assert source["api_url"] == ""
