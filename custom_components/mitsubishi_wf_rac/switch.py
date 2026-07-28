"""for switch component used for Home Leave (vacant) mode."""
# pylint: disable = too-few-public-methods

import logging

from . import MitsubishiWfRacConfigEntry
from homeassistant.components.switch import SwitchEntity

from .wfrac.models.aircon import AirconCommands
from .wfrac.device import Device
from .const import DOMAIN, HVAC_TRANSLATION
from homeassistant.components.climate.const import HVACMode

_LOGGER = logging.getLogger(__name__)

# Confirmed against real hardware (see #67, #187): "Vacant"/Home Leave mode is
# not a directly settable flag - the unit derives it itself from the heat
# target temperature, entering it below this threshold and leaving it above.
# Writing the raw Vacant bit in a command has no effect on its own.
HOME_LEAVE_TEMP = 10.0
# Temperature to restore when leaving Home Leave mode. There's no reliable way
# to recall whatever temperature was set before Home Leave was turned on (the
# unit itself doesn't report it), so this is a plain, reasonable default.
NORMAL_TEMP = 21.0


async def async_setup_entry(_hass, entry: MitsubishiWfRacConfigEntry, async_add_entities):
    """Setup switch entries"""

    device: Device = entry.runtime_data.device
    _LOGGER.info("Setup Home Leave switch: %s, %s", device.device_name, device.airco_id)
    # Only confirmed to work on ModelNr 1 units so far - see rac_parser.py's
    # own ModelNr-gated handling of this same bit.
    if device.airco.ModelNr == 1:
        async_add_entities([HomeLeaveModeSwitch(device)])
    else:
        _LOGGER.info(
            "Not setting up Home Leave switch: %s, %s (ModelNr %s not confirmed to support it)",
            device.device_name, device.airco_id, device.airco.ModelNr,
        )


class HomeLeaveModeSwitch(SwitchEntity):
    """Switch to enter/leave the unit's own Home Leave (vacant property) mode.

    Enabling this lowers the heat target temperature below the unit's own
    Home Leave threshold (~16-18°C, observed as 10°C once active), which the
    unit then reports as "Vacant" - this is a frost-protection/low-power
    standby mode intended for when nobody's home, distinct from just turning
    the unit off. Disabling it raises the temperature back to a normal value.
    """

    _attr_translation_key = "home_leave_mode"
    _attr_has_entity_name: bool = True
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, device: Device) -> None:
        super().__init__()
        self._device = device
        self._attr_device_info = device.device_info
        self._attr_unique_id = f"{DOMAIN}-{self._device.airco_id}-home-leave-mode"
        self._update_state()

    def _update_state(self) -> None:
        self._attr_is_on = self._device.airco.Vacant
        self._attr_available = self._device.available

    async def async_turn_on(self, **kwargs) -> None:
        """Enter Home Leave mode."""
        await self._device.set_airco(
            {
                AirconCommands.Operation: True,
                AirconCommands.OperationMode: HVAC_TRANSLATION[HVACMode.HEAT],
                AirconCommands.PresetTemp: HOME_LEAVE_TEMP,
            }
        )
        self._attr_is_on = True

    async def async_turn_off(self, **kwargs) -> None:
        """Leave Home Leave mode by restoring a normal target temperature."""
        await self._device.set_airco(
            {
                AirconCommands.PresetTemp: NORMAL_TEMP,
            }
        )
        self._attr_is_on = False

    async def async_update(self):
        """Retrieve latest state."""
        self._update_state()
