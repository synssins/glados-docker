"""Regression test: saving a default speaker in the Audio & Speakers config
must sync to HA's maintenance-speaker entity.

Bug: the WebUI Audio & Speakers page writes ``speakers.yaml:default`` when
the operator clicks Save, but the runtime engine routes maintenance-mode
audio via ``input_text.glados_maintenance_speaker`` in Home Assistant.
Before this fix those two were never connected — saving Living Room 2 in
the config had no effect on runtime routing, which kept playing from
whatever speaker HA last stored (e.g. Master Bedroom).

Fix: ``_put_config_section("speakers")`` now calls
``_sync_maintenance_speaker_to_ha`` which POSTs the new default to
HA's ``input_text/set_value`` service.

These tests verify the sync logic directly, without spinning up the full
HTTP server, by calling the method on a minimal handler stub.
"""
from __future__ import annotations

from unittest.mock import patch


class TestSyncMaintenanceSpeakerToHa:
    """Unit tests for Handler._sync_maintenance_speaker_to_ha."""

    def _make_handler(self):
        """Return a minimal Handler instance without calling __init__."""
        from glados.webui.tts_ui import Handler
        return object.__new__(Handler)

    def test_non_empty_default_posts_to_ha(self) -> None:
        """Saving a non-empty default speaker must call _ha_post."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post", return_value=True) as mock_post, \
             patch("glados.webui.tts_ui._cfg") as mock_cfg:
            mock_cfg.mode_entities.maintenance_speaker = "input_text.glados_maintenance_speaker"
            handler._sync_maintenance_speaker_to_ha({"default": "media_player.living_room_2"})
        mock_post.assert_called_once_with(
            "/api/services/input_text/set_value",
            {
                "entity_id": "input_text.glados_maintenance_speaker",
                "value": "media_player.living_room_2",
            },
        )

    def test_empty_default_does_not_post(self) -> None:
        """When the default is empty (-- none -- selected), no HA call is made."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post") as mock_post, \
             patch("glados.webui.tts_ui._cfg"):
            handler._sync_maintenance_speaker_to_ha({"default": ""})
        mock_post.assert_not_called()

    def test_missing_default_key_does_not_post(self) -> None:
        """A speakers payload without a ``default`` key is a no-op."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post") as mock_post, \
             patch("glados.webui.tts_ui._cfg"):
            handler._sync_maintenance_speaker_to_ha({"available": ["media_player.living_room_2"]})
        mock_post.assert_not_called()

    def test_none_default_does_not_post(self) -> None:
        """Explicit None default is treated as empty -- no HA call."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post") as mock_post, \
             patch("glados.webui.tts_ui._cfg"):
            handler._sync_maintenance_speaker_to_ha({"default": None})
        mock_post.assert_not_called()

    def test_ha_failure_is_logged_not_raised(self) -> None:
        """HA POST failure must not propagate as an exception -- just a log warning."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post", return_value=False) as mock_post, \
             patch("glados.webui.tts_ui._cfg") as mock_cfg:
            mock_cfg.mode_entities.maintenance_speaker = "input_text.glados_maintenance_speaker"
            # Should not raise.
            handler._sync_maintenance_speaker_to_ha({"default": "media_player.bedroom"})
        mock_post.assert_called_once()

    def test_uses_configured_entity_id(self) -> None:
        """The entity ID comes from cfg.mode_entities.maintenance_speaker, not hardcoded."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post", return_value=True) as mock_post, \
             patch("glados.webui.tts_ui._cfg") as mock_cfg:
            mock_cfg.mode_entities.maintenance_speaker = "input_text.custom_speaker_entity"
            handler._sync_maintenance_speaker_to_ha({"default": "media_player.office"})
        posted_payload = mock_post.call_args[0][1]
        assert posted_payload["entity_id"] == "input_text.custom_speaker_entity"
        assert posted_payload["value"] == "media_player.office"

    def test_whitespace_only_default_does_not_post(self) -> None:
        """Whitespace-only default is treated as empty -- no HA call."""
        handler = self._make_handler()
        with patch("glados.webui.tts_ui._ha_post") as mock_post, \
             patch("glados.webui.tts_ui._cfg"):
            handler._sync_maintenance_speaker_to_ha({"default": "   "})
        mock_post.assert_not_called()
