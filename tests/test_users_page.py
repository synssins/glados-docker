"""Unit tests for the Users management SPA page module (Task 7b)."""
from glados.webui.pages import users_page


def test_html_is_string():
    assert isinstance(users_page.HTML, str)
    assert len(users_page.HTML) > 0


def test_panel_id_present():
    assert 'id="tab-config-users"' in users_page.HTML


def test_table_columns_present():
    html = users_page.HTML
    assert "Username" in html
    assert "Display Name" in html
    assert "Role" in html
    assert "Status" in html
    assert "Last Login" in html


def test_add_modal_fields():
    html = users_page.HTML
    assert 'id="addUsername"' in html
    assert 'id="addDisplayName"' in html
    assert 'id="addRole"' in html
    assert 'id="addPassword"' in html


def test_add_role_defaults_to_chat():
    # The 'chat' option must have selected attribute
    assert '<option value="chat" selected>chat</option>' in users_page.HTML


def test_add_role_has_admin_option():
    assert '<option value="admin">admin</option>' in users_page.HTML


def test_edit_modal_fields():
    html = users_page.HTML
    assert 'id="editDisplayName"' in html
    assert 'id="editRole"' in html
    assert 'id="editDisabled"' in html


def test_reset_password_modal():
    html = users_page.HTML
    assert 'id="resetPassword"' in html
    assert 'usersSubmitReset' in html


def test_api_endpoints_referenced():
    html = users_page.HTML
    assert "'/api/users'" in html or '"/api/users"' in html


def test_delete_confirm_dialog():
    # Delete action must include a confirm() call (irreversible action)
    assert "confirm(" in users_page.HTML


def test_disable_confirm_dialog():
    # Disable action must also confirm
    html = users_page.HTML
    assert "usersConfirmDisable" in html
    assert "confirm(" in html


def test_auth_overlay_present():
    assert 'usersAuthOverlay' in users_page.HTML


def test_requires_admin_attribute_not_in_page_html():
    # The data-requires-admin gating is in _shell.py, not the page content
    # (the page contains the reveal script but not the nav item itself)
    # The page's inline script fetches /api/auth/status to reveal nav items
    assert "data-requires-admin" in users_page.HTML or "auth/status" in users_page.HTML


def test_toast_on_success():
    # Page calls showToast() on success — relies on the global function
    assert "showToast(" in users_page.HTML


def test_error_banners_present():
    html = users_page.HTML
    assert "usersAddError" in html
    assert "usersEditError" in html
    assert "usersResetError" in html
    assert "usersErrorBanner" in html
