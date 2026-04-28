"""Tests for the first-run wizard framework."""
import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock


@dataclass(frozen=True)
class _FakeStep:
    name: str = "fake"
    order: int = 100
    _required: bool = True
    _title: str = "fake"

    @property
    def title(self) -> str:
        return self._title

    def is_required(self, cfg) -> bool:
        return self._required

    def render(self, handler) -> str:
        return "<form></form>"

    def process(self, handler, form):
        from glados.webui.setup.wizard import StepResult
        return StepResult.DONE


def test_resolve_next_step_with_one_required():
    from glados.webui.setup import wizard as wiz
    steps = (_FakeStep(name="a", order=10),)
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt is not None and nxt.name == "a"


def test_resolve_next_step_skips_non_required():
    from glados.webui.setup import wizard as wiz
    steps = (
        _FakeStep(name="a", order=10, _required=False),
        _FakeStep(name="b", order=20, _required=True),
    )
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt is not None and nxt.name == "b"


def test_resolve_next_step_orders_by_order_field():
    from glados.webui.setup import wizard as wiz
    steps = (
        _FakeStep(name="b", order=20),
        _FakeStep(name="a", order=10),
    )
    nxt = wiz.resolve_next_step(steps, cfg=None)
    assert nxt is not None and nxt.name == "a"


def test_resolve_next_step_returns_none_when_done():
    from glados.webui.setup import wizard as wiz
    steps = (_FakeStep(name="a", _required=False),)
    assert wiz.resolve_next_step(steps, cfg=None) is None


def test_step_result_values():
    from glados.webui.setup.wizard import StepResult
    assert StepResult.DONE.value == "done"
    assert StepResult.ERROR.value == "error"
    assert StepResult.NEXT.value == "next"


# ── SetAdminPasswordStep ────────────────────────────────────────

import io
from unittest.mock import MagicMock


@pytest.fixture
def fresh_configs(tmp_path, monkeypatch):
    """Empty configs dir + tmp data dir; AuthGlobal at fresh-install state."""
    configs = tmp_path / "configs"
    data = tmp_path / "data"
    configs.mkdir()
    data.mkdir()

    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(configs))
    monkeypatch.setenv("GLADOS_DATA", str(data))

    from glados.auth import db as auth_db
    monkeypatch.setattr(auth_db, "_db_path", lambda: data / "auth.db")
    auth_db.ensure_schema()

    from glados.core.config_store import cfg
    cfg.load(configs_dir=str(configs))
    yield {"configs": configs, "data": data}


def _mock_handler():
    h = MagicMock()
    h.client_address = ("127.0.0.1", 0)
    h.headers = {}
    return h


def test_admin_password_step_required_when_fresh():
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep()

    class FakeAuth:
        users = []
        bootstrap_allowed = True

    class FakeCfg:
        auth = FakeAuth()

    assert s.is_required(FakeCfg) is True


def test_admin_password_step_not_required_when_admin_exists():
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep()

    class FakeUser:
        role = "admin"

    class FakeAuth:
        users = [FakeUser()]
        bootstrap_allowed = False

    class FakeCfg:
        auth = FakeAuth()

    assert s.is_required(FakeCfg) is False


def test_admin_password_step_not_required_when_bootstrap_disabled():
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep()

    class FakeAuth:
        users = []
        bootstrap_allowed = False

    class FakeCfg:
        auth = FakeAuth()

    assert s.is_required(FakeCfg) is False


def test_admin_password_step_process_rejects_short_password(fresh_configs):
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    from glados.webui.setup.wizard import StepResult
    s = SetAdminPasswordStep()
    h = _mock_handler()
    form = {"username": "residenta", "password": "abc", "confirm": "abc"}
    assert s.process(h, form) == StepResult.ERROR


def test_admin_password_step_process_rejects_mismatched_confirm(fresh_configs):
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    from glados.webui.setup.wizard import StepResult
    s = SetAdminPasswordStep()
    h = _mock_handler()
    form = {"username": "residenta", "password": "hunter2goes",
            "confirm": "different"}
    assert s.process(h, form) == StepResult.ERROR


def test_admin_password_step_process_rejects_empty_username(fresh_configs):
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    from glados.webui.setup.wizard import StepResult
    s = SetAdminPasswordStep()
    h = _mock_handler()
    form = {"username": "  ", "password": "hunter2goes",
            "confirm": "hunter2goes"}
    assert s.process(h, form) == StepResult.ERROR


def test_admin_password_step_process_creates_admin(fresh_configs):
    """Successful process: writes a single admin user to global.yaml."""
    import yaml
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    from glados.webui.setup.wizard import StepResult

    s = SetAdminPasswordStep()
    h = _mock_handler()
    form = {"username": "ResidentA", "display_name": "ResidentA",
            "password": "hunter2goes", "confirm": "hunter2goes"}
    result = s.process(h, form)
    assert result == StepResult.DONE

    raw = yaml.safe_load((fresh_configs["configs"] / "global.yaml").read_text())
    assert len(raw["auth"]["users"]) == 1
    u = raw["auth"]["users"][0]
    assert u["username"] == "ResidentA"
    assert u["role"] == "admin"        # HARD-CODED — never from form
    assert u["password_hash"].startswith("$argon2id$")
    assert raw["auth"]["bootstrap_allowed"] is False
    assert raw["auth"]["session_secret"]   # generated when missing


def test_admin_password_step_render_includes_all_fields(fresh_configs):
    from glados.webui.setup.steps.admin_password import SetAdminPasswordStep
    s = SetAdminPasswordStep()
    h = _mock_handler()
    html = s.render(h)
    assert 'name="username"' in html
    assert 'name="password"' in html
    assert 'name="confirm"' in html
    assert 'type="password"' in html
    # NO role field — first user is always admin
    assert 'name="role"' not in html


# ── Routing — mock-handler integration ──────────────────────────

import io
from urllib.parse import urlencode


def _make_get_handler(path):
    h = MagicMock()
    h.path = path
    h.command = "GET"
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Cookie": ""}
    h._sent = []
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: h._sent.append(("end_headers",))
    h.wfile = io.BytesIO()
    return h


def _make_post_handler(path, form):
    body_bytes = urlencode(form).encode("utf-8")
    h = MagicMock()
    h.path = path
    h.command = "POST"
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Content-Length": str(len(body_bytes)),
                 "User-Agent": "pytest", "Cookie": ""}
    h.rfile = io.BytesIO(body_bytes)
    h._sent = []
    h.send_response = lambda c: h._sent.append(("status", c))
    h.send_header = lambda k, v: h._sent.append(("header", k, v))
    h.end_headers = lambda: h._sent.append(("end_headers",))
    h.wfile = io.BytesIO()
    return h


def _location_header(handler):
    for entry in handler._sent:
        if entry[0] == "header" and entry[1] == "Location":
            return entry[2]
    return None


def test_setup_get_redirects_to_first_step(fresh_configs):
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/setup")
    Handler._dispatch_setup(h)
    assert ("status", 302) in h._sent
    assert _location_header(h) == "/setup/admin-password"


def test_setup_blocks_after_bootstrap_complete(fresh_configs):
    """When users[] populated AND bootstrap_allowed=False, /setup redirects to /login."""
    import yaml
    from glados.auth import hashing
    (fresh_configs["configs"] / "global.yaml").write_text(yaml.safe_dump({
        "auth": {
            "enabled": True,
            "session_secret": "s" * 64,
            "bootstrap_allowed": False,
            "users": [{
                "username": "admin", "display_name": "admin", "role": "admin",
                "password_hash": hashing.hash_password("hunter2goes"),
                "hash_algorithm": "argon2id", "disabled": False, "created_at": 0,
            }],
        },
    }))
    from glados.core.config_store import cfg
    cfg.reload()

    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/setup")
    Handler._dispatch_setup(h)
    assert _location_header(h) == "/login"


def test_setup_post_creates_admin_and_redirects_home(fresh_configs):
    """Successful POST issues session cookie + 302 to /."""
    from glados.webui.tts_ui import Handler
    h = _make_post_handler("/setup/admin-password", {
        "username": "ResidentA", "display_name": "ResidentA",
        "password": "hunter2goes", "confirm": "hunter2goes",
    })
    Handler._dispatch_setup(h)
    # Final redirect: /
    assert _location_header(h) == "/"
    # Session cookie set
    cookies = [e[2] for e in h._sent
               if e[0] == "header" and e[1] == "Set-Cookie"]
    assert any("glados_session=" in c for c in cookies)


def test_setup_post_invalid_rerenders_with_error(fresh_configs):
    from glados.webui.tts_ui import Handler
    h = _make_post_handler("/setup/admin-password", {
        "username": "ResidentA", "password": "abc", "confirm": "abc",
    })
    Handler._dispatch_setup(h)
    # 200 response with HTML body
    assert ("status", 200) in h._sent
    body = h.wfile.getvalue()
    assert b"error" in body.lower() or b"8 characters" in body


def test_login_redirects_to_setup_on_fresh_install(fresh_configs):
    """do_GET("/login") on a fresh install must 302 to /setup before
    rendering the login form."""
    from glados.webui.tts_ui import Handler
    h = _make_get_handler("/login")
    Handler.do_GET(h)
    assert ("status", 302) in h._sent
    assert _location_header(h) == "/setup"
