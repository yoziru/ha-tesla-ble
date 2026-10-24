"""Config and options flow tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.components import bluetooth
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_ble import config_flow as config_flow_module
from custom_components.tesla_ble.const import CONF_ADDRESS, CONF_PAIR_ON_SETUP, CONF_ROLE, CONF_VIN, DOMAIN

VIN = "5YJ30123456789ABC"
ADDRESS = "AA:BB:CC:DD:EE:FF"
ADVERTISED = "S1a87a5a75f3df858C"  # for a different VIN
BEACON = bytes.fromhex("021574278bdab64445208f0c720eaf05993501009970c5")


def _discovered(address: str, name: str | None, **extra) -> SimpleNamespace:
    fields = {"service_uuids": set(), "service_data": {}, "manufacturer_data": {}}
    fields.update(extra)
    return SimpleNamespace(address=address, name=name, **fields)


@pytest.fixture
def settled_advert(monkeypatch):
    """Skip the discovery settle window and report the vehicle as present."""
    monkeypatch.setattr(config_flow_module, "DISCOVERY_SETTLE_INTERVAL", 0)
    monkeypatch.setattr(config_flow_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda *a, **kw: SimpleNamespace(time=1000.0, rssi=-60),
    )


@pytest.mark.parametrize("role", [None, "DRIVER", "CHARGING_MANAGER", "VEHICLE_MONITOR"])
async def test_manual_flow_creates_entry(hass, role) -> None:
    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "manual"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["step_id"] == "vin"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: VIN})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "role"
    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ROLE: "OWNER"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {} if role is None else {CONF_ROLE: role}
    )
    assert result["step_id"] == "pair"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_VIN: VIN,
        CONF_ADDRESS: ADDRESS,
        CONF_PAIR_ON_SETUP: True,
        CONF_ROLE: role or "DRIVER",
    }
    assert result["title"] == "Tesla 789ABC"


async def test_manual_flow_rejects_bad_address(hass) -> None:
    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "nonsense"})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}


async def test_manual_flow_rejects_bad_vin(hass) -> None:
    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: "SHORT"})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {CONF_VIN: "invalid_vin"}


async def test_user_flow_offers_discovered_devices(hass) -> None:
    discovered = _discovered(ADDRESS, ADVERTISED)
    with patch.object(bluetooth, "async_discovered_service_info", return_value=[discovered]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["step_id"] == "vin"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: VIN})
    # The discovered name belongs to a different VIN.
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {CONF_VIN: "name_mismatch"}


async def test_user_flow_offers_beacon_only_vehicle(hass) -> None:
    discovered = [
        _discovered(ADDRESS, None, manufacturer_data={76: BEACON}),
        _discovered("11:22:33:44:55:66", None),
    ]
    with patch.object(bluetooth, "async_discovered_service_info", return_value=discovered):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    choices = result["data_schema"].schema[next(iter(result["data_schema"].schema))].container
    assert choices[ADDRESS] == f"Tesla vehicle ({ADDRESS})"
    assert set(choices) == {ADDRESS, config_flow_module.MANUAL_ADDRESS}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["step_id"] == "vin"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: VIN})
    assert result["step_id"] == "role"


async def test_user_flow_hides_ignored_vehicle(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_IGNORE},
        data={"unique_id": ADDRESS, "title": "Ignored Tesla"},
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIN: VIN, CONF_ADDRESS: "22:33:44:55:66:77"},
        unique_id=VIN,
    ).add_to_hass(hass)
    discovered = [
        _discovered(ADDRESS, None, manufacturer_data={76: BEACON}),
        _discovered("11:22:33:44:55:66", "Tesla 123456"),
        _discovered("22:33:44:55:66:77", "Tesla 654321"),
    ]
    with patch.object(bluetooth, "async_discovered_service_info", return_value=discovered):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    choices = result["data_schema"].schema[next(iter(result["data_schema"].schema))].container
    assert set(choices) == {"11:22:33:44:55:66", config_flow_module.MANUAL_ADDRESS}
    assert choices["11:22:33:44:55:66"] == "Tesla 123456 (11:22:33:44:55:66)"


async def test_bluetooth_discovery_flow(hass, settled_advert) -> None:
    advertised = "S1a87a5a75f3df858C"
    # Derive the VIN whose advertised name this is: use the binding in reverse.
    import tesla_ble

    vin = next(v for v in ("5YJS0000000000000", VIN) if tesla_ble.get_vin_advertisement_name(v) == advertised)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_discovered(ADDRESS, advertised),
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "vin"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: vin})
    assert result["step_id"] == "role"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VIN] == vin


@pytest.mark.parametrize("name", [None, ADDRESS])
async def test_bluetooth_discovery_accepts_beacon_without_name(hass, settled_advert, monkeypatch, name) -> None:
    """Passive-only scanners may forward the iBeacon payload without a name."""
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lambda *a, **kw: SimpleNamespace(name=ADDRESS))
    info = _discovered(
        ADDRESS,
        name,
        manufacturer_data={76: BEACON},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=info,
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "vin"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: VIN})
    assert result["step_id"] == "role"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_configured_vehicle_is_not_rediscovered(hass, settled_advert):
    import tesla_ble

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_VIN: VIN, CONF_ADDRESS: ADDRESS}, unique_id=VIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_discovered(ADDRESS, tesla_ble.get_vin_advertisement_name(VIN)),
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_flow_can_be_ignored(hass, settled_advert) -> None:
    """Discovery flows need a unique ID for the Ignore action to work."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_discovered(ADDRESS, ADVERTISED),
    )
    flow = next(f for f in hass.config_entries.flow.async_progress() if f["flow_id"] == result["flow_id"])
    assert flow["context"].get("unique_id") == ADDRESS


async def test_bluetooth_discovery_ignores_passing_vehicle(hass, monkeypatch) -> None:
    """A car that only passes by must not create a setup flow."""
    monkeypatch.setattr(config_flow_module, "DISCOVERY_SETTLE_INTERVAL", 0)
    monkeypatch.setattr(bluetooth, "async_last_service_info", lambda *a, **kw: None)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_discovered(ADDRESS, ADVERTISED),
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "not_seen"


async def test_bluetooth_discovery_ignores_other_devices(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_discovered(ADDRESS, "SomeSpeaker"),
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_duplicate_vin_aborts(hass) -> None:
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Tesla 789ABC",
        data={CONF_VIN: VIN, CONF_ADDRESS: ADDRESS},
        source=config_entries.SOURCE_USER,
        unique_id=VIN,
        discovery_keys={},
        options={},
        subentries_data=None,
    )
    await hass.config_entries.async_add(entry)

    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "11:22:33:44:55:66"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: VIN})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_apply_after_reload_and_reject_invalid_intervals(hass, sim, monkeypatch, hass_storage):
    from test_manager import setup_entry

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    old = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(result["flow_id"], {"vcsec_poll_interval": 0})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"vcsec_poll_interval": 120})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert old._stopped and entry.runtime_data is not old
    assert entry.runtime_data.vcsec_poll_interval == 120


async def test_reconfigure_preserves_vin_and_credentials(hass, sim, monkeypatch, hass_storage):
    from test_manager import setup_entry

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    old_key = entry.runtime_data._store.load("private_key")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"
    with patch.object(bluetooth, "async_ble_device_from_address", return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "11-22-33-44-55-66"})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data == {CONF_VIN: VIN, CONF_ADDRESS: "11:22:33:44:55:66"}
    assert entry.runtime_data._store.load("private_key") == old_key


async def test_duplicate_address_aborts(hass) -> None:
    """A second vehicle cannot claim the same Bluetooth address."""
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Tesla 789ABC",
        data={CONF_VIN: VIN, CONF_ADDRESS: ADDRESS},
        source=config_entries.SOURCE_USER,
        unique_id=VIN,
        discovery_keys={},
        options={},
        subentries_data=None,
    )
    await hass.config_entries.async_add(entry)

    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VIN: "5YJ30123456789DEF"})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
