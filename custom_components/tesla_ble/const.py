"""Constants for the Tesla BLE integration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .manager import TeslaBLEManager

# Lazily evaluated alias (Python 3.12+ `type` statement): safe at runtime
# without importing manager (avoids the const <-> manager cycle).
type TeslaBLEConfigEntry = ConfigEntry[TeslaBLEManager]

DOMAIN = "tesla_ble"

# Issue registry keys
ISSUE_PAIRING_REQUIRED = "pairing_required"

# Tesla vehicle BLE GATT profile
SERVICE_UUID = "00000211-b2d1-43f0-9b88-960cebf8b91e"
WRITE_UUID = "00000212-b2d1-43f0-9b88-960cebf8b91e"
READ_UUID = "00000213-b2d1-43f0-9b88-960cebf8b91e"

# VCSEC advertisements carry an Apple-format iBeacon: the constant Tesla
# proximity UUID (major/minor identify the vehicle) so phones can detect the
# car in the background. The GATT service UUID above only appears after
# connecting, so discovery matches this beacon signature instead.
TESLA_BEACON_MANUFACTURER_ID = 76
TESLA_BEACON_DATA_PREFIX = bytes.fromhex("021574278bdab64445208f0c720eaf059935")

# Config entry data
CONF_VIN = "vin"
CONF_ADDRESS = "address"
CONF_ROLE = "role"
ROLE_LABELS = {
    "DRIVER": "Vehicle controls",
    "CHARGING_MANAGER": "Charging controls",
    "VEHICLE_MONITOR": "Read-only monitoring",
    "OWNER": "Owner (legacy)",
}
# One-shot flag set by the config flow: pair right after the entry is added.
CONF_PAIR_ON_SETUP = "pair_on_setup"

# Options
CONF_VCSEC_POLL_INTERVAL = "vcsec_poll_interval"
CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE = "infotainment_poll_interval_awake"
CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE = "infotainment_poll_interval_active"
CONF_INFOTAINMENT_SLEEP_TIMEOUT = "infotainment_sleep_timeout"
CONF_DISCONNECT_DELAY = "disconnect_delay"

DEFAULT_VCSEC_POLL_INTERVAL = 60
DEFAULT_INFOTAINMENT_POLL_INTERVAL_AWAKE = 30
DEFAULT_INFOTAINMENT_POLL_INTERVAL_ACTIVE = 10
DEFAULT_INFOTAINMENT_SLEEP_TIMEOUT = 660
DEFAULT_DISCONNECT_DELAY = 120

MIN_VCSEC_POLL_INTERVAL = 10
MAX_VCSEC_POLL_INTERVAL = 300
MIN_INFOTAINMENT_POLL_INTERVAL = 5
MAX_INFOTAINMENT_POLL_INTERVAL = 600
MIN_SLEEP_TIMEOUT = 60
MAX_SLEEP_TIMEOUT = 3600
MIN_DISCONNECT_DELAY = 10
MAX_DISCONNECT_DELAY = 3600

# BLE write fragmentation (matches the ESPHome component)
WRITE_CHUNK_SIZE = 18

# The manager tick drives Vehicle.loop()
TICK_INTERVAL = timedelta(milliseconds=500)

# Reconnect backoff after failed connection attempts
CONNECT_RETRY_BACKOFF = timedelta(seconds=30)
