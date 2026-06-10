"""Origin.EVENT_RULE exists and is a member of Origin.ALL."""
from glados.observability.audit import Origin


def test_event_rule_origin_exists():
    assert Origin.EVENT_RULE == "event_rule"


def test_event_rule_in_all():
    assert "event_rule" in Origin.ALL
