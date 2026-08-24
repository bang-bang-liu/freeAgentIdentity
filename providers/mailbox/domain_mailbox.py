"""Catch-all domain IMAP mailbox provider registry entry."""
from core.domain_mailbox import DomainImapMailbox  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "domain_mailbox")(DomainImapMailbox)
