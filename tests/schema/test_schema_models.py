import pytest

from reflect.schema.events import RawEvent


def test_raw_event_allows_unknown_attrs_but_not_unknown_fields():
    evt = RawEvent(
        id="e1",
        source_id="src",
        source_type="otlp",
        event_type="span",
        observed_at="2026-01-01T00:00:00Z",
        received_at="2026-01-01T00:00:01Z",
        attrs={"gen_ai.memory.scope": "repo", "unknown": 1},
        content_hash="abc",
    )
    assert evt.attrs.model_dump()["unknown"] == 1

    with pytest.raises(ValueError):
        RawEvent(
            id="e2",
            source_id="src",
            source_type="otlp",
            event_type="span",
            observed_at="2026-01-01T00:00:00Z",
            received_at="2026-01-01T00:00:01Z",
            content_hash="abc",
            unexpected="boom",
        )
