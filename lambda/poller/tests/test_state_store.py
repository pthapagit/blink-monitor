"""Tests for state_store.py: SSM-backed last-seen clip tracking, including the
'first run' (ParameterNotFound) path and the don't-store-empty guard."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import state_store
from state_store import INITIAL_VALUE, StateStore


def _client_error(code):
    return ClientError({"Error": {"Code": code}}, "Op")


def _store_with_client(mock_client):
    with patch.object(state_store.boto3, "client", return_value=mock_client):
        return StateStore()


async def test_get_returns_stored_value():
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "clip-42"}}
    store = _store_with_client(client)
    assert await store.get_last_seen_clip_id() == "clip-42"


async def test_get_missing_param_is_first_run():
    client = MagicMock()
    client.get_parameter.side_effect = _client_error("ParameterNotFound")
    store = _store_with_client(client)
    assert await store.get_last_seen_clip_id() == INITIAL_VALUE


async def test_get_other_error_raises():
    client = MagicMock()
    client.get_parameter.side_effect = _client_error("AccessDenied")
    store = _store_with_client(client)
    with pytest.raises(ClientError):
        await store.get_last_seen_clip_id()


async def test_set_writes_parameter():
    client = MagicMock()
    store = _store_with_client(client)
    await store.set_last_seen_clip_id("clip-7")
    client.put_parameter.assert_called_once()
    assert client.put_parameter.call_args.kwargs["Value"] == "clip-7"


async def test_set_empty_is_skipped():
    client = MagicMock()
    store = _store_with_client(client)
    await store.set_last_seen_clip_id("")
    client.put_parameter.assert_not_called()
