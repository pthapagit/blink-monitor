"""Tests for the SSM delivery log: legacy ids, JSON round-trip, and the cap."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import state_store
from state_store import (
    MAX_SEEN_CLIPS,
    DeliveryState,
    StateStore,
    parse_parameter_value,
    serialize_state,
)


def _client_error(code):
    return ClientError({"Error": {"Code": code}}, "Op")


def _store_with_client(mock_client):
    with patch.object(state_store.boto3, "client", return_value=mock_client):
        return StateStore()


def test_none_is_first_run():
    state = parse_parameter_value("NONE")
    assert state.first_run is True
    assert state.seen == []


def test_plain_clip_id_is_a_legacy_cursor():
    state = parse_parameter_value("1796171000")
    assert state.legacy_cursor == "1796171000"
    assert state.first_run is False


def test_json_round_trip_keeps_seen_and_failures():
    original = DeliveryState(seen=["7:100", "8:200"], failures={"9:300": 2})
    restored = parse_parameter_value(serialize_state(original))
    assert restored.seen == ["7:100", "8:200"]
    assert restored.failures == {"9:300": 2}
    assert restored.legacy_cursor is None
    assert restored.first_run is False


def test_remember_drops_oldest_past_the_cap():
    state = DeliveryState()
    for index in range(MAX_SEEN_CLIPS + 5):
        state.remember(f"{index}:{index}")
    assert len(state.seen) == MAX_SEEN_CLIPS
    assert "0:0" not in state.seen_set()
    assert f"{MAX_SEEN_CLIPS + 4}:{MAX_SEEN_CLIPS + 4}" in state.seen_set()


def test_corrupt_json_does_not_replay():
    state = parse_parameter_value("{not-json")
    assert state.first_run is True


async def test_get_missing_param_is_first_run():
    client = MagicMock()
    client.get_parameter.side_effect = _client_error("ParameterNotFound")
    store = _store_with_client(client)
    state = await store.get_delivery_state()
    assert state.first_run is True


async def test_get_other_error_raises():
    client = MagicMock()
    client.get_parameter.side_effect = _client_error("AccessDenied")
    store = _store_with_client(client)
    with pytest.raises(ClientError):
        await store.get_delivery_state()


async def test_save_writes_json_not_a_bare_id():
    client = MagicMock()
    store = _store_with_client(client)
    await store.save_delivery_state(DeliveryState(seen=["7:100"]))
    written = client.put_parameter.call_args.kwargs["Value"]
    assert written.startswith("{")
    assert "7:100" in written
