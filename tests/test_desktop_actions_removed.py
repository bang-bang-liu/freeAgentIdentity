from core.capability_registry import STANDARD_CAPABILITIES
from platforms.cursor import switch as cursor_api
from platforms.cursor.plugin import CursorPlatform
from platforms.kiro import switch as kiro_api
from platforms.kiro.plugin import KiroPlatform


def test_cursor_and_kiro_do_not_expose_desktop_actions():
    for platform in (CursorPlatform(), KiroPlatform()):
        action_ids = {action["id"] for action in platform.get_platform_actions()}

        assert "switch_account" not in action_ids
        assert "switch_desktop" not in platform.get_platform_capabilities()


def test_desktop_capability_and_local_mutators_are_removed():
    assert "switch_desktop" not in STANDARD_CAPABILITIES

    for module, names in (
        (
            cursor_api,
            ("switch_cursor_account", "restart_cursor_ide", "read_current_cursor_account"),
        ),
        (
            kiro_api,
            ("switch_kiro_account", "restart_kiro_ide", "read_current_kiro_account"),
        ),
    ):
        for name in names:
            assert not hasattr(module, name)
