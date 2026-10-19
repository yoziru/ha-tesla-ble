"""TeslaBLE manager: owns the Vehicle state machine and the BLE connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

import tesla_ble

from .ble import HaBleAdapter, TeslaBleTransport
from .const import (
    CONF_ADDRESS,
    CONF_DISCONNECT_DELAY,
    CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE,
    CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE,
    CONF_INFOTAINMENT_SLEEP_TIMEOUT,
    CONF_PAIR_ON_SETUP,
    CONF_ROLE,
    CONF_VCSEC_POLL_INTERVAL,
    CONF_VIN,
    CONNECT_RETRY_BACKOFF,
    DEFAULT_DISCONNECT_DELAY,
    DEFAULT_INFOTAINMENT_POLL_INTERVAL_ACTIVE,
    DEFAULT_INFOTAINMENT_POLL_INTERVAL_AWAKE,
    DEFAULT_INFOTAINMENT_SLEEP_TIMEOUT,
    DEFAULT_VCSEC_POLL_INTERVAL,
    DOMAIN,
    ISSUE_PAIRING_REQUIRED,
    ROLE_LABELS,
    TICK_INTERVAL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SEAT_COOL_FIELDS = {
    tesla_ble.SeatPosition.FRONT_LEFT: "seat_fan_front_left",
    tesla_ble.SeatPosition.FRONT_RIGHT: "seat_fan_front_right",
}

CHARGING_ACTIVE_STATES = ("Charging", "Starting")
# HA's total_increasing statistics only treat a drop below 90% of the previous
# reading as a reset; smaller dips are meter jitter/revisions (see sensor/recorder.py).
ENERGY_RESET_RATIO = 0.9
# AC energy is integrated from power samples; only the start of a gap is trusted
# because a stop or restart while unobserved is indistinguishable from a
# continuing session.
AC_ENERGY_MAX_GAP_SECONDS = 300

# Advert RSSI is only meaningful for a few minutes; stale history from
# habluetooth must not look like the vehicle is still nearby.
RSSI_MAX_AGE_SECONDS = 300

# Presence is not activity. A proven return gets a short, non-renewable probe
# window; reconnects/reloads share a persisted cooldown, even if probes fail.
ARRIVAL_ABSENCE_SECONDS = 300
ARRIVAL_WINDOW_SECONDS = 180
REFRESH_COOLDOWN_SECONDS = 660
ARRIVAL_MAX_POLLS = 6

# Infotainment data arrives on the current poll cadence; the vehicle status
# arrives from VCSEC, and update state only refreshes on demand.
INFOTAINMENT_STATES = (
    "charge_state",
    "climate_state",
    "closures_state",
    "drive_state",
    "tire_pressure_state",
)
TELEMETRY_STATES = ("vehicle_status", "software_update_state", *INFOTAINMENT_STATES)
POLL_STATE_NAMES = {
    "VCSEC Poll": "vehicle_status",
    "Charge State Poll": "charge_state",
    "Climate State Poll": "climate_state",
    "Drive State Poll": "drive_state",
    "Closures State Poll": "closures_state",
    "Tire Pressure Poll": "tire_pressure_state",
    "Software Update Poll": "software_update_state",
}


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def charger_power_w(state: dict[str, Any]) -> float | None:
    """Return derived watts, or reported watts when derived fields are absent."""
    if "charger_power_w" in state:
        return state["charger_power_w"]
    reported = state.get("charger_power")
    return reported * 1000 if isinstance(reported, (int, float)) and math.isfinite(reported) and reported >= 0 else None


class TeslaBLEManager:
    """Drives the C++ Vehicle state machine from the HA event loop."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.vin: str = entry.data[CONF_VIN]
        self.address: str = entry.data[CONF_ADDRESS]
        # Entries created before role selection enrolled OWNER keys. Treat them
        # as Vehicle controls and never request OWNER again; a re-pair enrolls
        # the entry's role, so legacy entries repair with DRIVER access.
        self.role: str = entry.data.get(CONF_ROLE, "DRIVER")
        if self.role not in ROLE_LABELS:
            raise HomeAssistantError(f"Unsupported vehicle key role: {self.role}")
        self.device_name = f"Tesla {self.vin[-6:]}"

        options = entry.options
        self.vcsec_poll_interval: int = options.get(CONF_VCSEC_POLL_INTERVAL, DEFAULT_VCSEC_POLL_INTERVAL)
        self.disconnect_delay: int = options.get(CONF_DISCONNECT_DELAY, DEFAULT_DISCONNECT_DELAY)

        # Observed state (plain dicts produced by the bindings)
        self.vehicle_status: dict[str, Any] = {}
        self.charge_state: dict[str, Any] = {}
        self.energy_total = 0.0
        self._energy_last_session: float | None = None
        self.energy_tracking_since: str | None = None
        self.ac_energy_total = 0.0
        self._ac_energy_last_power: float | None = None
        self._ac_energy_last_time = 0.0
        self.climate_state: dict[str, Any] = {}
        self.drive_state: dict[str, Any] = {}
        self.closures_state: dict[str, Any] = {}
        self.tire_pressure_state: dict[str, Any] = {}
        self.software_update_state: dict[str, Any] = {}
        self.last_command: str | None = None
        self.connected = False
        self.rssi: int | None = None
        self.polling = True
        self.capabilities: set[str] = set()

        self._listeners: list[Callable[[], None]] = []
        self._last_activity = 0.0
        self._connect_blocked_until = 0.0
        self._work_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False
        self._state_received_at: dict[str, float] = {}
        self.state_observed_at: dict[str, datetime] = {}
        self.paired = False
        self._key_rejected = False
        self._repairing = False
        self._repair_rejection_seen = False
        self._issue_id = f"{ISSUE_PAIRING_REQUIRED}_{entry.entry_id}"
        self._tick_cancel: Callable[[], None] | None = None
        self._stop_cancel: Callable[[], None] | None = None
        self._next_vcsec_poll = 0.0
        self._refresh_pending = False
        self._unreachable_since: float | None = None
        self._arrival_until = 0.0
        self._arrival_polls = 0
        self._last_genuine_activity: float | None = None
        self._poll_decision: dict[str, Any] = {}
        self._poll_attempts: dict[str, dict[str, Any]] = {}
        self._last_poll_error: str | None = None
        self._pairing_notification_id = f"{DOMAIN}_pairing_{entry.entry_id}"
        self._pairing_pending = False
        self._pairing_tasks: set[asyncio.Task] = set()

        from .storage import TeslaStore

        self._store = TeslaStore(hass, entry.entry_id)
        self._transport = TeslaBleTransport(hass, self.address, self.device_name, self._handle_disconnect)
        self._ble_adapter = HaBleAdapter(self._transport)
        # Created in async_setup() after the store is loaded, so the C++
        # Vehicle sees the persisted private key and sessions.
        self._vehicle: tesla_ble.Vehicle | None = None

        self._poll_policy = tesla_ble.InfotainmentPollPolicy()
        self._poll_policy.set_awake_interval_ms(
            options.get(CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE, DEFAULT_INFOTAINMENT_POLL_INTERVAL_AWAKE) * 1000
        )
        self._poll_policy.set_active_interval_ms(
            options.get(CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE, DEFAULT_INFOTAINMENT_POLL_INTERVAL_ACTIVE) * 1000
        )
        self._poll_policy.set_sleep_timeout_ms(
            options.get(CONF_INFOTAINMENT_SLEEP_TIMEOUT, DEFAULT_INFOTAINMENT_SLEEP_TIMEOUT) * 1000
        )

    @property
    def can_control_vehicle(self) -> bool:
        return self.role in ("OWNER", "DRIVER")

    @property
    def can_control_charging(self) -> bool:
        return self.can_control_vehicle or self.role == "CHARGING_MANAGER"

    @property
    def can_wake(self) -> bool:
        return self.can_control_charging

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._store.async_load()
        await self._store.async_flush()
        self.polling = self._store.load("polling") != b"0"
        self.capabilities = set(json.loads(self._store.load("capabilities") or b"[]"))
        saved = self._store.load("telemetry")
        if saved is not None:
            try:
                observations = json.loads(saved)
                restored = {}
                for state in TELEMETRY_STATES:
                    if state not in observations:
                        continue
                    observation = observations[state]
                    observed_at = dt_util.parse_datetime(observation["observed_at"])
                    if observed_at is None or observed_at.tzinfo is None or not isinstance(observation["data"], dict):
                        raise ValueError("Invalid observation")
                    restored[state] = (observation["data"], observed_at)
                for state, (data, observed_at) in restored.items():
                    setattr(self, state, data)
                    self.state_observed_at[state] = observed_at
            except ValueError, TypeError, KeyError:
                _LOGGER.warning("Could not restore last-known telemetry; waiting for fresh observations")
        saved_energy = self._store.load("energy")
        if saved_energy is not None:
            try:
                energy = json.loads(saved_energy)
                total, last = energy["total"], energy["last_session"]
                if (
                    not math.isfinite(total)
                    or total < 0
                    or (last is not None and (not math.isfinite(last) or last < 0))
                ):
                    raise ValueError("Invalid energy counter")
                self.energy_total = total
                self._energy_last_session = last
                self.energy_tracking_since = energy["tracking_since"]
            except (ValueError, TypeError, KeyError) as err:
                raise HomeAssistantError(
                    "Could not restore Tesla BLE energy counter; stored data was preserved"
                ) from err
        else:
            self.energy_tracking_since = dt_util.utcnow().isoformat()
            self._save_energy()
        saved_ac_energy = self._store.load("energy_ac")
        if saved_ac_energy is not None:
            try:
                total = json.loads(saved_ac_energy)["total"]
                if not math.isfinite(total) or total < 0:
                    raise ValueError("Invalid AC energy counter")
                self.ac_energy_total = total
            except (ValueError, TypeError, KeyError) as err:
                raise HomeAssistantError(
                    "Could not restore Tesla BLE AC energy counter; stored data was preserved"
                ) from err
        self._create_vehicle()
        # A reload is not evidence of activity. Start backed off, allowing only
        # the bounded initial probe until live activity is actually observed.
        self._poll_policy.update(
            (monotonic_ms() - self._poll_policy.sleep_timeout_ms - 1) & 0xFFFFFFFF, False, False, False
        )
        self._poll_policy.on_poll(monotonic_ms())
        self._key_rejected = self._store.load("key_rejected") == b"1"
        self._sync_pairing()
        self._update_rssi()
        self._transport.set_notify_callback(self._on_notify)
        self._tick_cancel = async_track_time_interval(self.hass, self._async_tick, TICK_INTERVAL)
        self._stop_cancel = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        self._next_vcsec_poll = time.monotonic()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PAIRING_REQUIRED)
        self._async_update_pairing_issue()
        if self.entry.data.get(CONF_PAIR_ON_SETUP):
            self._consume_pair_on_setup()

    def _create_vehicle(self) -> None:
        self._vehicle = tesla_ble.Vehicle(self._ble_adapter, self._store)
        self._vehicle.set_vin(self.vin)
        self._vehicle.set_vehicle_status_callback(self._on_vehicle_status)
        self._vehicle.set_charge_state_callback(self._on_charge_state)
        self._vehicle.set_climate_state_callback(self._on_climate_state)
        self._vehicle.set_drive_state_callback(self._on_drive_state)
        self._vehicle.set_tire_pressure_state_callback(self._on_tire_pressure_state)
        self._vehicle.set_closures_state_callback(self._on_closures_state)
        self._vehicle.set_software_update_state_callback(self._on_software_update_state)
        self._vehicle.set_key_not_paired_callback(self._on_key_not_paired)
        self._vehicle.set_poll_result_callback(self._on_poll_result)

    def _key_id(self) -> bytes | None:
        key = self._store.load("private_key")
        return hashlib.sha256(key).hexdigest().encode() if key else None

    @asynccontextmanager
    async def _work(self) -> AsyncIterator[None]:
        if self._stopped or self.hass.is_stopping:
            raise HomeAssistantError("Tesla BLE integration is unloaded")
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            async with self._work_lock:
                if self._stopped or self.hass.is_stopping:
                    raise HomeAssistantError("Tesla BLE integration is unloaded")
                yield
        finally:
            self._tasks.discard(task)

    async def async_shutdown(self) -> None:
        self._stopped = True
        async with self._shutdown_lock:
            await self._async_shutdown()

    async def _async_shutdown(self) -> None:
        if self._stop_cancel is not None:
            self._stop_cancel()
            self._stop_cancel = None
        if self._tick_cancel is not None:
            self._tick_cancel()
            self._tick_cancel = None
        tasks = list(self._tasks - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        pairing_tasks = list(self._pairing_tasks)
        for task in pairing_tasks:
            task.cancel()
        await asyncio.gather(*tasks, *pairing_tasks, return_exceptions=True)
        try:
            await self._async_disconnect()
            await self._store.async_flush()
        finally:
            self._transport.set_notify_callback(None)
            if self._vehicle is not None:
                self._vehicle.clear_callbacks()
            self._vehicle = None
            self._listeners.clear()
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            if self._pairing_pending:
                self._pairing_pending = False
                persistent_notification.async_dismiss(self.hass, self._pairing_notification_id)

    async def _async_stop(self, _event: Any) -> None:
        await self.async_shutdown()

    def _async_update_pairing_issue(self) -> None:
        """Surface a repair issue while no key is paired with the vehicle."""
        if self._vehicle is None:
            return
        if self.paired or self._key_rejected:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_PAIRING_REQUIRED,
                translation_placeholders={"vehicle": self.device_name},
            )

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    def _async_notify_pairing(self, message: str) -> None:
        persistent_notification.async_create(
            self.hass,
            message,
            title=f"{self.device_name}: pairing",
            notification_id=self._pairing_notification_id,
        )

    def _async_notify_paired(self) -> None:
        self._pairing_pending = False
        self._async_notify_pairing(
            f"{self.device_name} is paired with Home Assistant with {ROLE_LABELS[self.role]} access over Bluetooth."
        )

    def _consume_pair_on_setup(self) -> None:
        """Pair once right after the entry is added, when the flow requested it.

        The flag stays in the entry data; the key check makes it inert after
        the first attempt (which always generates the vehicle key first), so
        a restart never re-triggers a pairing session.
        """
        if self.paired or self._vehicle is None or self._vehicle.has_private_key or self._stopped:
            return
        task = self.hass.async_create_task(self._async_pair_on_setup())
        self._pairing_tasks.add(task)
        task.add_done_callback(self._pairing_tasks.discard)

    async def _async_pair_on_setup(self) -> None:
        try:
            await self.async_pair()
        except Exception as err:  # noqa: BLE001 - background task reports its own failure
            _LOGGER.warning("Initial pairing could not start: %s", err)
            self._pairing_pending = False
            self._async_notify_pairing(
                f"Pairing could not start: {err}. Make sure the vehicle is awake and in "
                "range of Bluetooth, then press the Pair button on the device page."
            )

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def available(self) -> bool:
        return not self._stopped and (
            self.connected or any(self.state_available(state) for state in self._state_received_at)
        )

    def state_fresh(self, state: str) -> bool:
        """Whether telemetry for this state was received recently enough."""
        received = self._state_received_at.get(state)
        if received is None or self._stopped:
            return False
        if state == "vehicle_status":
            interval = self.vcsec_poll_interval
        elif state in INFOTAINMENT_STATES:
            # Freshness is an observation contract, not the scheduler's promise.
            # Idle backoff can intentionally leave awake telemetry unavailable.
            interval = self._poll_policy.awake_interval_ms / 1000
        else:
            interval = self._poll_policy.sleep_timeout_ms / 1000
        return time.monotonic() - received <= max(120, interval * 2)

    def state_available(self, state: str) -> bool:
        """Whether entities should expose this state.

        The vehicle status is the reachability signal: while it is fresh the
        car can be talked to (not proof of location). A sleeping vehicle stops sending
        infotainment data by design, so its last known values stay visible;
        once status itself is stale all telemetry expires. Advertisements
        alone cannot confirm that the car is still asleep.
        """
        if self._stopped or self._key_rejected:
            return False
        status_fresh = self.state_fresh("vehicle_status")
        if state == "vehicle_status":
            return status_fresh
        if not status_fresh:
            return False
        if self.state_fresh(state):
            return True
        return bool(self.vehicle_status.get("asleep", False))

    def _received_state(self, state: str) -> None:
        self._state_received_at[state] = time.monotonic()
        self.state_observed_at[state] = dt_util.utcnow()
        # Restore observations, never monotonic freshness or Bluetooth reachability.
        self._store.save(
            "telemetry",
            json.dumps(
                {
                    key: {"observed_at": observed_at.isoformat(), "data": getattr(self, key)}
                    for key, observed_at in self.state_observed_at.items()
                }
            ).encode(),
        )
        self._sync_pairing()
        self._notify()

    def _sync_pairing(self) -> None:
        confirmed = (
            not self._key_rejected
            and self._vehicle.has_private_key
            and self._store.load("paired_key") == self._key_id()
        )
        if self.paired != confirmed:
            self.paired = confirmed
            if confirmed and self._pairing_pending:
                self._async_notify_paired()
            self._async_update_pairing_issue()

    def _mark_paired(self) -> None:
        self._key_rejected = False
        self._store.remove("key_rejected")
        self.paired = True
        self._store.save("paired_key", self._key_id())
        self._refresh_pending = True
        self._next_vcsec_poll = 0.0
        self._async_update_pairing_issue()

    @property
    def pairing_command_pending(self) -> bool:
        """Whether the vehicle still has a queued pairing request."""
        return self._vehicle is not None and self._vehicle.pending_command_count > 0

    @property
    def repair_rejection_seen(self) -> bool:
        """Whether the car rejected the key again while the user repaired it."""
        return self._repair_rejection_seen

    def begin_repair(self) -> None:
        """Let the car's answers decide pairing while the user repairs the key."""
        self._repairing = True
        self._repair_rejection_seen = False
        self._key_rejected = False
        self._store.remove("key_rejected")
        # Pairing must be re-proven by a real session, not a cached marker.
        self.paired = False
        self._store.remove("paired_key")
        self._refresh_pending = True
        self._next_vcsec_poll = 0.0
        self._notify()

    def end_repair(self) -> None:
        """Stop repairing; a rejection the car still reports stays in effect."""
        self._repairing = False
        if self._repair_rejection_seen and not self.paired:
            self._repair_rejection_seen = False
            self._on_key_not_paired()
        self._repair_rejection_seen = False

    def _on_key_not_paired(self) -> None:
        if self._stopped:
            return
        if self._repairing:
            self._repair_rejection_seen = True
            return
        if self._key_rejected:
            return
        self._key_rejected = True
        self.paired = False
        self._store.remove("paired_key")
        self._store.save("key_rejected", b"1")
        self._state_received_at.clear()
        self.last_command = "Pairing required: the vehicle rejected Home Assistant's key"
        self._async_update_pairing_issue()
        self.entry.async_start_reauth(self.hass)
        self._notify()

    # ------------------------------------------------------------------
    # C++ callbacks (event loop thread)
    # ------------------------------------------------------------------

    def _on_vehicle_status(self, status: dict[str, Any]) -> None:
        # Presence alone can be a nearby phone. Require a live passenger-door
        # transition too; restored/first-seen values are not transitions.
        if (
            self.state_fresh("vehicle_status")
            and status.get("user_present") is True
            and status.get("asleep") is False
            and any(
                self.vehicle_status.get("closures", {}).get(door) == "closed"
                and status.get("closures", {}).get(door) in ("open", "ajar")
                for door in ("front_driver_door", "front_passenger_door", "rear_driver_door", "rear_passenger_door")
            )
        ):
            self._record_genuine_activity()
        self.vehicle_status = status
        if status.get("asleep") is True:
            self._arrival_until = 0.0
        self._received_state("vehicle_status")

    def _record_genuine_activity(self) -> None:
        """Only live activity or a successful user action buys idle time."""
        self._last_genuine_activity = time.monotonic()
        self._poll_policy.reset()

    def _on_poll_result(self, name: str, result: tesla_ble.CommandResult) -> None:
        if state := POLL_STATE_NAMES.get(name):
            attempt = self._poll_attempts.setdefault(state, {})
            attempt.update(
                outcome=result.outcome,
                reason=result.terminal_reason,
                error=result.error_message,
                completed_at=dt_util.utcnow().isoformat(),
            )

    def _on_charge_state(self, state: dict[str, Any]) -> None:
        session_energy = state.get("charge_energy_added")
        if isinstance(session_energy, (int, float)) and math.isfinite(session_energy) and session_energy >= 0:
            session_energy = round(session_energy, 2)
            if self._energy_last_session is None:
                self._energy_last_session = session_energy
            else:
                delta = round(session_energy - self._energy_last_session, 2)
                if delta >= 0:
                    self.energy_total = round(self.energy_total + delta, 2)
                    self._energy_last_session = session_energy
                elif session_energy < self._energy_last_session * ENERGY_RESET_RATIO:
                    # New session: the meter restarted well below the previous reading.
                    # ponytail: a >10% downward revision of an ended session would still
                    # double-count; BLE reports no session ID to distinguish it.
                    self.energy_total = round(self.energy_total + session_energy, 2)
                    self._energy_last_session = session_energy
            self._save_energy()
        self._integrate_ac_energy(state)
        self.charge_state = state
        self._received_state("charge_state")

    def _integrate_ac_energy(self, state: dict[str, Any]) -> None:
        """Accumulate AC (wall-side) energy from the car's own power measurements."""
        power = charger_power_w(state)
        is_ac = state.get("charger_is_ac")
        if is_ac is None and "fast_charger_present" in state:
            is_ac = not state["fast_charger_present"]
        if power is None or (is_ac is not True and state.get("charger_power_source") != "disconnected"):
            self._ac_energy_last_power = None
            return
        now = time.monotonic()
        if self._ac_energy_last_power is not None:
            elapsed = now - self._ac_energy_last_time
            if elapsed > 0:
                area = (self._ac_energy_last_power + power) / 2 * min(elapsed, AC_ENERGY_MAX_GAP_SECONDS) / 3_600_000
                self.ac_energy_total = round(self.ac_energy_total + area, 4)
                self._save_ac_energy()
        self._ac_energy_last_power = power
        self._ac_energy_last_time = now

    def _save_ac_energy(self) -> None:
        self._store.save("energy_ac", json.dumps({"total": self.ac_energy_total}).encode())

    def _save_energy(self) -> None:
        self._store.save(
            "energy",
            json.dumps(
                {
                    "total": self.energy_total,
                    "last_session": self._energy_last_session,
                    "tracking_since": self.energy_tracking_since,
                }
            ).encode(),
        )

    def _on_climate_state(self, state: dict[str, Any]) -> None:
        self._remember_capabilities(state)
        self.climate_state = state
        self._received_state("climate_state")

    def _on_drive_state(self, state: dict[str, Any]) -> None:
        self.drive_state = state
        if state.get("shift_state") in ("D", "R", "N"):
            self._record_genuine_activity()
        self._received_state("drive_state")

    def _on_tire_pressure_state(self, state: dict[str, Any]) -> None:
        self.tire_pressure_state = state
        self._received_state("tire_pressure_state")

    def _on_closures_state(self, state: dict[str, Any]) -> None:
        self._remember_capabilities(state)
        self.closures_state = state
        self._received_state("closures_state")

    def _remember_capabilities(self, state: dict[str, Any]) -> None:
        observed = {
            key
            for key in state
            if key.startswith(("seat_heater_", "seat_fan_", "auto_seat_climate_"))
            or key
            in {
                "steering_wheel_heater",
                "auto_steering_wheel_heat",
                "steering_wheel_heat_level",
                "cabin_overheat_protection",
                "cop_activation_temperature",
                "sun_roof_percent_open",
            }
        }
        if not observed.issubset(self.capabilities):
            self.capabilities.update(observed)
            self._store.save("capabilities", json.dumps(sorted(self.capabilities)).encode())

    def _on_software_update_state(self, state: dict[str, Any]) -> None:
        self.software_update_state = state
        self._received_state("software_update_state")

    def _on_notify(self, data: bytes) -> None:
        if self._stopped or self._vehicle is None or not self.connected:
            return
        self._last_activity = time.monotonic()
        self._vehicle.on_rx_data(data)

    def _handle_disconnect(self) -> None:
        self.connected = False
        if self._vehicle is not None:
            self._vehicle.set_connected(False)
        self._notify()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def async_ensure_connected(self) -> None:
        async with self._work():
            await self._async_ensure_connected()

    async def _async_ensure_connected(self) -> None:
        """Connect if needed; raises HomeAssistantError when unreachable."""
        if self._stopped or self.hass.is_stopping:
            raise HomeAssistantError("Tesla BLE integration is unloaded")
        if self._transport.is_connected:
            return
        if time.monotonic() < self._connect_blocked_until:
            raise HomeAssistantError(f"Tesla {self.vin[-6:]} is not reachable over Bluetooth; retrying soon")
        await self._store.async_flush()
        if self._stopped or self.hass.is_stopping:
            raise HomeAssistantError("Tesla BLE integration is unloaded")
        try:
            await self._transport.connect()
        except Exception as err:  # noqa: BLE001 - surface any connect failure
            self._connect_blocked_until = time.monotonic() + CONNECT_RETRY_BACKOFF.total_seconds()
            raise HomeAssistantError(f"Could not connect to Tesla {self.vin[-6:]}: {err}") from err
        if self._stopped or self.hass.is_stopping:
            await self._transport.disconnect()
            raise HomeAssistantError("Tesla BLE integration is unloaded")
        self._vehicle.set_connected(True)
        self.connected = True
        self._last_activity = time.monotonic()
        self._connect_blocked_until = 0.0
        self._next_vcsec_poll = 0.0
        self._request_passive_refresh(False)
        self._notify()

    async def _async_disconnect(self) -> None:
        if self.connected or self._transport.is_connected:
            self.connected = False
            if self._vehicle is not None:
                self._vehicle.set_connected(False)
        await self._transport.disconnect()
        self._notify()

    # ------------------------------------------------------------------
    # Periodic work
    # ------------------------------------------------------------------

    async def _async_tick(self, _now: Any) -> None:
        if self._stopped or self.hass.is_stopping or self._work_lock.locked():
            return
        async with self._work():
            try:
                await self._async_run_tick()
            except HomeAssistantError, ConnectionError:
                _LOGGER.debug("Vehicle unavailable during tick", exc_info=True)
            self._sync_pairing()
            self._notify()

    async def _async_run_tick(self) -> None:
        if self._transport.is_connected:
            if self._vehicle is not None and not self._vehicle.is_connected:
                # The library reset its sessions (e.g. auth stall) while the
                # GATT link is still up. Cycle the link so the next poll
                # re-authenticates on a fresh connection.
                _LOGGER.debug("Vehicle reset its session; cycling the BLE link")
                await self._async_disconnect()
            else:
                self._vehicle.loop()
                await self._transport.async_flush()
        self._update_rssi()
        await self._async_handle_polls()
        await self._async_handle_idle_disconnect()

    @property
    def reachable(self) -> bool:
        """Whether the vehicle is currently within Bluetooth range."""
        return self.state_fresh("vehicle_status") or self.rssi is not None

    def _update_rssi(self) -> None:
        from homeassistant.components import bluetooth

        was_reachable = self.reachable
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is not None and time.monotonic() - info.time > RSSI_MAX_AGE_SECONDS:
            info = None  # stale advert: the vehicle is out of range
        rssi = info.rssi if info is not None else None
        if rssi != self.rssi:
            self.rssi = rssi
            self._notify()
        now = time.monotonic()
        if not self.reachable:
            if self._unreachable_since is None:
                self._unreachable_since = now
        else:
            if not was_reachable or self._unreachable_since is not None:
                absent = self._unreachable_since
                self._request_passive_refresh(absent is not None and now - absent >= ARRIVAL_ABSENCE_SECONDS)
                self._next_vcsec_poll = 0.0
                self._connect_blocked_until = 0.0
            self._unreachable_since = None

    def _request_passive_refresh(self, arrival: bool) -> None:
        """Coalesce adverts/links; persist the budget so reloads cannot renew it."""
        saved = self._store.load("passive_refresh_at")
        try:
            last = dt_util.parse_datetime(saved.decode()) if saved else None
            elapsed = (dt_util.utcnow() - last).total_seconds() if last else None
        except ValueError, TypeError, UnicodeError:
            elapsed = None
        if elapsed is not None and 0 <= elapsed < REFRESH_COOLDOWN_SECONDS:
            return
        self._store.save("passive_refresh_at", dt_util.utcnow().isoformat().encode())
        self._refresh_pending = True
        if arrival:
            self._arrival_until = time.monotonic() + ARRIVAL_WINDOW_SECONDS
            self._arrival_polls = 0

    @property
    def polling_diagnostics(self) -> dict[str, Any]:
        """Do not confuse a queued request, a cached value and an observation."""
        now = time.monotonic()
        utcnow = dt_util.utcnow()
        return {
            "decision": self._poll_decision,
            "arrival_remaining_seconds": max(0, self._arrival_until - now),
            "arrival_polls": self._arrival_polls,
            "genuine_activity_age_seconds": (
                now - self._last_genuine_activity if self._last_genuine_activity is not None else None
            ),
            "last_error": self._last_poll_error,
            "groups": {
                state: {
                    "observation_age_seconds": (
                        max(0, (utcnow - self.state_observed_at[state]).total_seconds())
                        if state in self.state_observed_at
                        else None
                    ),
                    "fresh": self.state_fresh(state),
                    "available": self.state_available(state),
                    "poll": self._poll_attempts.get(state),
                }
                for state in TELEMETRY_STATES
            },
        }

    def _infotainment_poll_decision(self, now_ms: int) -> dict[str, Any]:
        is_asleep = self.state_fresh("vehicle_status") and bool(self.vehicle_status.get("asleep", False))
        is_charging = (
            self.state_fresh("charge_state") and self.charge_state.get("charging_state") in CHARGING_ACTIVE_STATES
        )
        is_sentry = self.state_fresh("closures_state") and tesla_ble.sentry_mode_active(
            self.closures_state.get("sentry_mode_state", 0)
        )
        decision = self._poll_policy.update(now_ms, is_asleep, is_charging, is_sentry)
        arrival = time.monotonic() < self._arrival_until and self._arrival_polls < ARRIVAL_MAX_POLLS
        if arrival and not is_asleep:
            decision["interval_ms"] = min(decision["interval_ms"], self._poll_policy.awake_interval_ms)
        self._poll_decision = {
            "effective_interval_seconds": decision["interval_ms"] / 1000,
            "idle_backoff": decision["wake_policy"] == tesla_ble.WakePolicy.NO_WAKE_SKIP and not is_asleep,
            "asleep": is_asleep,
            "charging": is_charging,
            "sentry": is_sentry,
            "arrival_window": arrival and not is_asleep,
            "automatic_wake_policy": "NO_WAKE_SKIP",
        }
        return decision

    async def _async_handle_polls(self) -> None:
        if not self.polling or self._key_rejected or self._vehicle.pending_command_count:
            return
        now = time.monotonic()
        now_ms = monotonic_ms()
        vcsec_due = self._refresh_pending or now >= self._next_vcsec_poll
        decision = self._infotainment_poll_decision(now_ms)
        infotainment_due = self._refresh_pending or self._poll_policy.should_poll(now_ms, decision["interval_ms"])
        if not (vcsec_due or infotainment_due):
            return

        if not self._transport.is_connected:
            try:
                await self._async_ensure_connected()
            except HomeAssistantError:
                # Back off; try again on a later tick.
                self._last_poll_error = "connect_failed"
                self._next_vcsec_poll = now + CONNECT_RETRY_BACKOFF.total_seconds()
                return

        if vcsec_due or self._refresh_pending:
            self._poll_attempts["vehicle_status"] = {"outcome": "pending"}
            self._vehicle.vcsec_poll(tesla_ble.WakePolicy.NO_WAKE_SKIP)
            self._next_vcsec_poll = now + self.vcsec_poll_interval
        if infotainment_due or self._refresh_pending:
            self._last_poll_error = None
            self._poll_attempts.update({state: {"outcome": "pending"} for state in INFOTAINMENT_STATES})
            self._vehicle.infotainment_poll(tesla_ble.WakePolicy.NO_WAKE_SKIP)
            self._poll_policy.on_poll(now_ms)
            if now < self._arrival_until:
                self._arrival_polls += 1
        self._refresh_pending = False
        await self._transport.async_flush()

    async def _async_handle_idle_disconnect(self) -> None:
        if not self._transport.is_connected:
            return
        if self._vehicle is not None and self._vehicle.pending_command_count > 0:
            return
        if time.monotonic() - self._last_activity >= self.disconnect_delay:
            await self._async_disconnect()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _make_result_callback(self, name: str) -> Callable[[tesla_ble.CommandResult], None]:
        def callback(result: tesla_ble.CommandResult) -> None:
            if result.success:
                text = f"{name}: ok"
                if name not in ("Pair", "Regenerate Key"):
                    self._record_genuine_activity()
            elif result.skipped:
                text = f"{name}: skipped"
            else:
                error = result.error_message or "No error details were reported by the vehicle"
                if any(reason in error.upper() for reason in ("UNAUTHORIZED", "INSUFFICIENT_PRIVILEGES")):
                    if name == "Wake" and self.role == "CHARGING_MANAGER":
                        error = "Vehicle denied wake. Wake the car manually; check its software and key permissions"
                    else:
                        error = "Vehicle denied this action. Check the key's permissions in the car"
                    _LOGGER.debug("Vehicle permission error for %s: %s", name, result.error_message)
                text = f"{name}: failed ({error})"
                if result.may_have_succeeded:
                    text += "; outcome unknown"
            self.last_command = text
            _LOGGER.debug("Command result: %s", text)
            if name == "Pair":
                if result.success:
                    self._mark_paired()
                    self._async_notify_paired()
                else:
                    self._pairing_pending = False
                    self._async_notify_pairing(
                        "Pairing failed. Keep the car awake and nearby. Enable Pair vehicle on "
                        "the device's entity page, press it, then tap your key card and approve "
                        f"on the car's screen. Details: {result.error_message}"
                    )
                self._async_update_pairing_issue()
            self._notify()

        return callback

    async def async_command(
        self,
        name: str,
        action: Callable[[Callable], None],
        *,
        required_access: Literal["vehicle", "charging", "wake"] = "vehicle",
    ) -> None:
        """Check access before connecting; unclassified actions require vehicle controls."""
        if self._key_rejected:
            raise HomeAssistantError("Pairing required. Reauthenticate Tesla BLE in Settings → Devices & services")
        allowed = {
            "vehicle": self.can_control_vehicle,
            "charging": self.can_control_charging,
            "wake": self.can_wake,
        }.get(required_access, False)
        if not allowed:
            raise HomeAssistantError(
                f"{name} is not allowed with {ROLE_LABELS[self.role]}. "
                "Pair a new key with Vehicle controls to use this action."
            )
        async with self._work():
            await self._async_ensure_connected()
            self._last_activity = time.monotonic()
            self.last_command = f"{name}: pending"
            self._notify()
            action(self._make_result_callback(name))
            await self._transport.async_flush()

    async def async_pair(self) -> None:
        """Send the whitelist-add request; tap a key card on the console next."""
        async with self._work():
            if self._pairing_pending and self.pairing_command_pending:
                raise HomeAssistantError("Pairing is already waiting for key-card approval")
            self._pairing_pending = False
            if not self._vehicle.has_private_key:
                self._vehicle.regenerate_key()
                if not self._vehicle.has_private_key:
                    raise HomeAssistantError("Could not generate a vehicle private key")
            await self._store.async_flush()
            await self._async_ensure_connected()
            self._last_activity = time.monotonic()
            self.last_command = "Pair: waiting for key card tap"
            self._pairing_pending = True
            self._async_notify_pairing(
                f"Tap a key card on the console of {self.device_name}, confirm the key on "
                "the touchscreen, and remove the card. Keep the car awake and nearby; the "
                "request retries for a few minutes."
            )
            self._notify()
            self._vehicle.pair(getattr(tesla_ble.KeysRole, self.role), self._make_result_callback("Pair"))
            await self._transport.async_flush()

    async def async_force_update(self) -> None:
        if self._key_rejected:
            raise HomeAssistantError("Pairing required. Reauthenticate Tesla BLE in Settings → Devices & services")
        async with self._work():
            await self._async_ensure_connected()
            self._last_activity = time.monotonic()
            self._vehicle.vcsec_poll()
            wake_policy = tesla_ble.WakePolicy.WAKE_IF_NEEDED if self.can_wake else tesla_ble.WakePolicy.NO_WAKE_SKIP
            self._vehicle.infotainment_poll(wake_policy)
            self._vehicle.software_update_poll(wake_policy)
            self._poll_policy.on_poll(monotonic_ms())
            self._refresh_pending = False
            self._next_vcsec_poll = time.monotonic() + self.vcsec_poll_interval
            if not self.can_wake:
                self.last_command = "Refresh: requested without waking. Wake the car manually if data stays unchanged."
                self._notify()
            await self._transport.async_flush()

    async def async_wake(self) -> None:
        await self.async_command("Wake", lambda cb: self._vehicle.wake(cb), required_access="wake")

    async def async_set_polling(self, enabled: bool) -> None:
        """Pause or resume polling the vehicle."""
        if self.polling == enabled:
            return
        self.polling = enabled
        self._store.save("polling", b"1" if enabled else b"0")
        if enabled:
            self._next_vcsec_poll = 0.0
        self._notify()

    async def async_lock_doors(self, lock: bool) -> None:
        await self.async_command(
            "Lock" if lock else "Unlock",
            lambda cb: self._vehicle.lock(cb) if lock else self._vehicle.unlock(cb),
        )

    async def async_set_charging(self, enable: bool) -> None:
        await self.async_command(
            "Start Charging" if enable else "Stop Charging",
            lambda cb: self._vehicle.set_charging_state(enable, cb),
            required_access="charging",
        )

    async def async_set_charging_amps(self, amps: int) -> None:
        await self.async_command(
            "Set Charging Amps", lambda cb: self._vehicle.set_charging_amps(amps, cb), required_access="charging"
        )

    async def async_set_charging_limit(self, limit: int) -> None:
        await self.async_command(
            "Set Charging Limit", lambda cb: self._vehicle.set_charging_limit(limit, cb), required_access="charging"
        )

    async def async_set_climate(self, enable: bool) -> None:
        await self.async_command(
            "Climate On" if enable else "Climate Off", lambda cb: self._vehicle.set_climate(enable, cb)
        )

    async def async_set_climate_temp(self, temp_celsius: float) -> None:
        await self.async_command("Set Climate Temp", lambda cb: self._vehicle.set_climate_temp(temp_celsius, cb))

    async def async_set_climate_keeper(self, mode: int) -> None:
        names = {0: "Climate Keeper Off", 1: "Climate Keeper On", 2: "Climate Keeper Dog", 3: "Climate Keeper Camp"}
        await self.async_command(
            names.get(mode, "Climate Keeper"), lambda cb: self._vehicle.set_climate_keeper(mode, cb)
        )

    async def async_set_preconditioning_max(self, enable: bool) -> None:
        await self.async_command(
            "Defrost On" if enable else "Defrost Off",
            lambda cb: self._vehicle.set_preconditioning_max(enable, cb),
        )

    async def async_set_bioweapon_mode(self, enable: bool) -> None:
        await self.async_command(
            "Bioweapon On" if enable else "Bioweapon Off",
            lambda cb: self._vehicle.set_bioweapon_mode(enable, cb),
        )

    async def async_set_steering_wheel_heat(self, enable: bool) -> None:
        await self.async_command(
            "Steering Heat On" if enable else "Steering Heat Off",
            lambda cb: self._vehicle.set_steering_wheel_heat(enable, cb),
        )

    async def async_set_seat_climate(self, seat: tesla_ble.SeatPosition, level: int, cooling: bool) -> None:
        """The library owns the Off sequence and cancellation."""
        cool_field = SEAT_COOL_FIELDS.get(seat)
        await self.async_command(
            "Set Seat Climate",
            lambda cb: self._vehicle.set_seat_climate(
                seat,
                level,
                cooling,
                cool_field in self.capabilities,
                cb,
            ),
        )

    async def async_set_auto_seat_climate(self, seat: tesla_ble.SeatPosition, enable: bool) -> None:
        await self.async_command(
            "Set Auto Seat Climate", lambda cb: self._vehicle.set_auto_seat_climate(seat, enable, cb)
        )

    async def async_set_cabin_overheat_protection(self, mode: int) -> None:
        await self.async_command(
            "Cabin Overheat Protection", lambda cb: self._vehicle.set_cabin_overheat_protection(mode, cb)
        )

    async def async_set_cabin_overheat_temperature(self, level: int) -> None:
        await self.async_command(
            "Cabin Overheat Temp", lambda cb: self._vehicle.set_cabin_overheat_protection_temp(level, cb)
        )

    async def async_set_sunroof_position(self, percent: int) -> None:
        await self.async_command("Set Sunroof Position", lambda cb: self._vehicle.set_sunroof_position(percent, cb))

    async def async_set_sentry_mode(self, enable: bool) -> None:
        await self.async_command(
            "Sentry On" if enable else "Sentry Off",
            lambda cb: self._vehicle.set_sentry_mode(enable, cb),
        )

    async def async_regenerate_key(self) -> None:
        async with self._work():
            await self._async_disconnect()
            self.paired = False
            self._store.remove("paired_key")
            self.last_command = "Regenerate Key: pending"
            self._notify()
            results = []
            self._vehicle.regenerate_key(results.append)
            self._async_update_pairing_issue()
            try:
                await self._store.async_flush()
            except HomeAssistantError:
                self.last_command = "Regenerate Key: failed (credentials not persisted)"
                self._notify()
                raise
            self._make_result_callback("Regenerate Key")(results[0])

    async def async_unlatch_driver_door(self) -> None:
        await self.async_command("Unlatch Driver Door", lambda cb: self._vehicle.unlatch_driver_door(cb))

    async def async_open_trunk(self) -> None:
        await self.async_command("Open Trunk", lambda cb: self._vehicle.open_trunk(cb))

    async def async_close_trunk(self) -> None:
        await self.async_command("Close Trunk", lambda cb: self._vehicle.close_trunk(cb))

    async def async_open_frunk(self) -> None:
        await self.async_command("Open Frunk", lambda cb: self._vehicle.open_frunk(cb))

    async def async_open_charge_port(self) -> None:
        await self.async_command("Open Charge Port", lambda cb: self._vehicle.open_charge_port(cb))

    async def async_close_charge_port(self) -> None:
        await self.async_command("Close Charge Port", lambda cb: self._vehicle.close_charge_port(cb))

    async def async_unlock_charge_port(self) -> None:
        await self.async_command("Unlock Charge Port", lambda cb: self._vehicle.unlock_charge_port(cb))

    async def async_vent_windows(self) -> None:
        await self.async_command("Vent Windows", lambda cb: self._vehicle.vent_windows(cb))

    async def async_close_windows(self) -> None:
        await self.async_command("Close Windows", lambda cb: self._vehicle.close_windows(cb))

    async def async_flash_lights(self) -> None:
        await self.async_command("Flash Lights", lambda cb: self._vehicle.flash_lights(cb))

    async def async_honk_horn(self) -> None:
        await self.async_command("Honk Horn", lambda cb: self._vehicle.honk_horn(cb))

    async def async_install_software_update(self) -> None:
        """Start installing a downloaded update immediately."""
        if not self.state_fresh("software_update_state") or self.software_update_state.get("status") != 4:
            raise HomeAssistantError("No freshly reported, ready-to-install vehicle update")
        await self.async_command("Schedule Software Update", lambda cb: self._vehicle.schedule_software_update(0, cb))

    async def async_cancel_software_update(self) -> None:
        if not self.state_fresh("software_update_state") or self.software_update_state.get("status") not in (3, 5, 6):
            raise HomeAssistantError("No freshly reported, cancellable vehicle update")
        await self.async_command("Cancel Software Update", lambda cb: self._vehicle.cancel_software_update(cb))
