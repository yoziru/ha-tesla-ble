"""Native TeslaBLE logs are routed through Python logging once, with debug filtered by default."""

import logging

import pytest
import tesla_ble
from test_manager import setup_entry

from custom_components import tesla_ble as integration


@pytest.fixture(autouse=True)
def reset_log_bridge():
    """The install cache is process-wide; clear it so each test installs again."""
    integration._install_log_bridge.cache_clear()
    yield
    integration._install_log_bridge.cache_clear()


async def test_setup_entry_installs_log_bridge(hass, sim, monkeypatch, hass_storage):
    installed = []
    native_set_callback = tesla_ble.set_log_callback

    def record_callback(callback):
        installed.append(callback)
        native_set_callback(callback)

    monkeypatch.setattr(integration.tesla_ble, "set_log_callback", record_callback)

    await setup_entry(hass, sim, monkeypatch, hass_storage)

    assert installed == [integration._log_native]


def test_native_logs_are_mapped_and_installed_once(monkeypatch, caplog):
    installed = []
    monkeypatch.setattr(integration.tesla_ble, "set_log_callback", installed.append)

    integration._install_log_bridge()
    integration._install_log_bridge()

    assert len(installed) == 1
    with caplog.at_level(logging.DEBUG, logger="tesla_ble"):
        for level in range(5):
            installed[0](level, "message", "TeslaBLE", 0)

    records = [r for r in caplog.records if r.name == "tesla_ble"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.ERROR, "TeslaBLE: message"),
        (logging.WARNING, "TeslaBLE: message"),
        (logging.INFO, "TeslaBLE: message"),
        (logging.DEBUG, "TeslaBLE: message"),
        (logging.DEBUG, "TeslaBLE: message"),
    ]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tesla_ble"):
        installed[0](3, "debug message", "TeslaBLE", 0)
        installed[0](0, "error message", "TeslaBLE", 0)

    assert [r.getMessage() for r in caplog.records if r.name == "tesla_ble"] == ["TeslaBLE: error message"]


@pytest.mark.parametrize("failure", ["missing", "raises"])
def test_log_bridge_guard_never_breaks_setup(monkeypatch, caplog, failure):
    def raising_callback(callback):
        raise RuntimeError("no callback")

    if failure == "missing":
        monkeypatch.delattr(integration.tesla_ble, "set_log_callback")
    else:
        monkeypatch.setattr(integration.tesla_ble, "set_log_callback", raising_callback)

    with caplog.at_level(logging.WARNING):
        integration._install_log_bridge()

    warnings = [r for r in caplog.records if r.name == "custom_components.tesla_ble"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
