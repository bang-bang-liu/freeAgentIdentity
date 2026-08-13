"""iCloud Workbench mailbox provider registration."""

from core.icloud_workbench_mailbox import ICloudWorkbenchMailbox  # noqa: F401
from providers.registry import register_provider


register_provider("mailbox", "icloud_workbench")(ICloudWorkbenchMailbox)
