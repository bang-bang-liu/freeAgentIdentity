from pathlib import Path


APP_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"


def _nav_items_block() -> str:
    source = APP_TSX.read_text(encoding="utf-8")
    start = source.index("const NAV_ITEMS: NavItem[] = [")
    end = source.index("];", start)
    return source[start:end]


def test_sidebar_top_level_nav_keeps_dashboard_chatgpt_mailbox_and_settings():
    block = _nav_items_block()

    assert block.count("path:") == 4
    assert 'path: "/"' in block
    assert 'labelKey: "nav.dashboard"' in block
    assert 'path: "/accounts/chatgpt"' in block
    assert 'label: "ChatGPT"' in block
    assert 'path: "/settings?tab=mailbox"' in block
    assert 'path: "/settings"' in block
    assert 'labelKey: "nav.settings"' in block


def test_sidebar_hides_accounts_menu_and_other_business_links():
    source = APP_TSX.read_text(encoding="utf-8")

    assert "setAccountsOpen" not in source
    assert "getPlatforms" not in source
    assert "nav.accounts" not in source
    assert "nav.ctfGptPlus" not in source
    assert "nav.gopayGptPlus" not in source
    assert "nav.plusManager" not in source
    assert "nav.tasks" not in source


def test_sidebar_has_one_mailbox_entry_and_preserves_settings_tab_routing():
    source = APP_TSX.read_text(encoding="utf-8")
    block = _nav_items_block()
    assert block.count('labelKey: "nav.settings.mailbox"') == 1
    assert 'SETTINGS_NAV_ITEMS' not in source
    assert 'to="/settings?tab=mailbox"' not in source
    assert "currentTab" in source
    assert 'currentTab === "mailbox"' in source
    assert 'path: "/settings"' in block
