"""Engine stands up the router iff the HA client singleton exists."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from glados.events import get_router, set_router
from glados.events.router import EventRouter


def teardown_function():
    set_router(None)


def test_init_event_router_subscribes_to_ha_client(tmp_path):
    from glados.core import engine as engine_mod
    client = MagicMock()
    client.call_service.return_value = {}
    cache = MagicMock()
    cache.get.return_value = None
    (tmp_path / "events.yaml").write_text("enabled: true\nrules: []\n", encoding="utf-8")
    with patch.object(engine_mod, "_ha_get_client", return_value=client), \
         patch.object(engine_mod, "_ha_get_cache", return_value=cache), \
         patch.object(engine_mod, "_events_config_path", return_value=tmp_path / "events.yaml"):
        engine_mod.init_event_router(quiet_check=lambda: False)
    router = get_router()
    assert isinstance(router, EventRouter)
    client.on_state_changed.assert_called_once_with(router.handle_state_changed)


def test_init_event_router_no_ha_client_stays_off(tmp_path):
    from glados.core import engine as engine_mod
    with patch.object(engine_mod, "_ha_get_client", return_value=None):
        engine_mod.init_event_router(quiet_check=lambda: False)
    assert get_router() is None
