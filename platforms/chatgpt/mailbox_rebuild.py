"""从已保存的账号图谱重建收信邮箱，用于刷新登录时取邮箱验证码。

设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 6.1。

重建所需的四个值全都已经落库，不需要迁移：

====================  ==========================================
provider 名           ``provider_accounts.provider_name``
收信邮箱              ``provider_accounts.login_identifier``
地址 ID               ``provider_resources.resource_identifier``
公开收件 API          ``provider_accounts.credentials.api_url``
====================  ==========================================

两个必须遵守的约束：

1. ``extra["mailbox_provider_key"]`` 必须显式注入。该键当前只在
   ``BaseMailbox.get_email()`` 内部被设置（``core/base_mailbox.py:127``），
   而刷新流程不调 ``get_email()``。缺这个键时 ``FallbackMailbox._resolve()``
   会抛"未找到邮箱 provider 上下文"。
2. 读码不会重新占用地址。``_claim_address()`` 只被 ``get_email()`` 调用，
   ``wait_for_code()`` 走 ``_messages()``，所以刷新不消耗新地址，也不改变
   原地址归属。

任何一步失败都**不是**硬阻断：返回 ``RebuiltMailbox(ok=False, reason=...)``，
由上层转入手动 OTP 等待，运维可自行查看旧邮箱输入验证码。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.base_mailbox import MailboxAccount


MAILBOX_PROVIDER_TYPE = "mailbox"


@dataclass
class RebuiltMailbox:
    """重建结果。``ok=False`` 时 ``reason`` 是给前端看的中文软失败原因。"""

    ok: bool = False
    reason: str = ""
    provider_name: str = ""
    email: str = ""
    mailbox: Any = None
    account: MailboxAccount | None = None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mailbox_provider_accounts(graph: dict[str, Any]) -> list[dict[str, Any]]:
    items = graph.get("provider_accounts")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and _text(item.get("provider_type")) == MAILBOX_PROVIDER_TYPE
    ]


def _mailbox_provider_resources(graph: dict[str, Any]) -> list[dict[str, Any]]:
    items = graph.get("provider_resources")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and _text(item.get("provider_type")) == MAILBOX_PROVIDER_TYPE
    ]


def resolve_mailbox_source(graph: dict[str, Any]) -> dict[str, str]:
    """从账号图谱抽出重建邮箱所需的四个值。只读，不建连接。

    返回 ``{"provider_name", "email", "address_id", "api_url"}``，取不到的
    键为空串。``api_url`` 只有 iCloud 这类公开收件 provider 才有，缺失不代表
    不可用（域名 IMAP 靠 catch-all 收信，不需要 api_url）。
    """
    provider_name = ""
    email = ""
    api_url = ""
    for item in _mailbox_provider_accounts(graph):
        provider_name = provider_name or _text(item.get("provider_name"))
        email = email or _text(item.get("login_identifier"))
        credentials = item.get("credentials")
        if isinstance(credentials, dict):
            api_url = api_url or _text(credentials.get("api_url"))
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            email = email or _text(metadata.get("email"))
            api_url = api_url or _text(metadata.get("api_url"))

    address_id = ""
    for item in _mailbox_provider_resources(graph):
        provider_name = provider_name or _text(item.get("provider_name"))
        address_id = address_id or _text(item.get("resource_identifier"))
        email = email or _text(item.get("handle"))
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            address_id = address_id or _text(metadata.get("account_id"))
            email = email or _text(metadata.get("email"))
            api_url = api_url or _text(metadata.get("api_url"))

    return {
        "provider_name": provider_name,
        "email": email,
        "address_id": address_id,
        "api_url": api_url,
    }


def describe_mailbox_source(graph: dict[str, Any]) -> tuple[bool, str, str]:
    """预检用：判断自动取码来源是否可用。不建连接，不发网络请求。

    返回 ``(available, provider_name, reason)``。``available=False`` 时
    ``reason`` 是软失败原因，上层据此提示"该账号需要手动输入验证码"。
    """
    source = resolve_mailbox_source(graph)
    provider_name = source["provider_name"]
    if not provider_name:
        return False, "", "账号未保存邮箱 provider，需手动输入验证码"
    if not source["email"]:
        return False, provider_name, "账号未保存收信邮箱地址，需手动输入验证码"

    try:
        from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

        definition = ProviderDefinitionsRepository().get_by_key(MAILBOX_PROVIDER_TYPE, provider_name)
    except Exception:
        return False, provider_name, "邮箱 provider 配置读取失败，需手动输入验证码"
    if not definition or not getattr(definition, "enabled", False):
        return False, provider_name, f"邮箱 provider {provider_name} 未启用，需手动输入验证码"
    return True, provider_name, ""


def rebuild_mailbox(
    graph: dict[str, Any],
    *,
    proxy: str | None = None,
    mailbox_factory: Any = None,
) -> RebuiltMailbox:
    """重建可用于 ``wait_for_code()`` 的 mailbox 与 MailboxAccount。

    ``mailbox_factory`` 是测试注入点，签名与 ``core.base_mailbox.create_mailbox``
    一致；生产环境留空即用真实工厂。

    失败一律返回 ``ok=False`` 并带软失败原因，不抛异常——上层要能无缝转入
    手动 OTP 等待。
    """
    source = resolve_mailbox_source(graph)
    provider_name = source["provider_name"]
    email = source["email"]
    if not provider_name:
        return RebuiltMailbox(reason="账号未保存邮箱 provider，需手动输入验证码")
    if not email:
        return RebuiltMailbox(
            reason="账号未保存收信邮箱地址，需手动输入验证码",
            provider_name=provider_name,
        )

    if mailbox_factory is None:
        from core.base_mailbox import create_mailbox as mailbox_factory  # noqa: PLC0415

    try:
        mailbox = mailbox_factory(provider=provider_name, extra={}, proxy=proxy)
    except Exception as exc:
        return RebuiltMailbox(
            reason=f"邮箱 provider 不可用（{str(exc)[:80]}），需手动输入验证码",
            provider_name=provider_name,
            email=email,
        )

    # 约束 1：mailbox_provider_key 必须显式注入。缺这个键时 FallbackMailbox
    # 会因为找不到 provider 上下文而抛错（core/base_mailbox.py:111-119）。
    extra: dict[str, Any] = {"mailbox_provider_key": provider_name}
    # iCloud 公开收件靠 provider_account.credentials.api_url 定位收件箱，
    # 见 core/icloud_workbench_mailbox.py:325-332。
    if source["api_url"]:
        extra["provider_account"] = {"credentials": {"api_url": source["api_url"]}}
    if source["address_id"]:
        extra["provider_resource"] = {"resource_identifier": source["address_id"]}

    account = MailboxAccount(
        email=email,
        account_id=source["address_id"],
        extra=extra,
    )
    return RebuiltMailbox(
        ok=True,
        provider_name=provider_name,
        email=email,
        mailbox=mailbox,
        account=account,
    )
