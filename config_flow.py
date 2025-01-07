"""Lutron Homeworks Series 4 and 8 config flow."""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import async_get_hass, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
    SchemaOptionsFlowHandler,
)
from homeassistant.helpers.selector import TextSelector
from homeassistant.helpers.typing import VolDictType
from homeassistant.util import slugify

from . import DEFAULT_FADE_RATE, calculate_unique_id
from .const import (
    CONF_ADDR,
    CONF_BUTTONS,
    CONF_CONTROLLER_ID,
    CONF_DIMMERS,
    CONF_INDEX,
    CONF_KEYPADS,
    CONF_LED,
    CONF_NUMBER,
    CONF_RATE,
    CONF_RELEASE_DELAY,
    CONF_SWITCHES,
    CONF_CCO,
    CONF_CCI,
    DEFAULT_BUTTON_NAME,
    DEFAULT_KEYPAD_NAME,
    DEFAULT_LIGHT_NAME,
    DEFAULT_SWITCH_NAME,
    DOMAIN,
)
from .pyhomeworks import exceptions as hw_exceptions
from .pyhomeworks.pyhomeworks import Homeworks
from .pyhomeworks.discovery import HomeworksDiscovery, DiscoveredDevice

_LOGGER = logging.getLogger(__name__)

CONTROLLER_EDIT = {
    vol.Required(CONF_HOST): selector.TextSelector(),
    vol.Required(CONF_PORT): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1,
            max=65535,
            mode=selector.NumberSelectorMode.BOX,
        )
    ),
    vol.Optional(CONF_USERNAME): selector.TextSelector(),
    vol.Optional(CONF_PASSWORD): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    ),
}

LIGHT_EDIT: VolDictType = {
    vol.Optional(CONF_RATE, default=DEFAULT_FADE_RATE): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=20,
            mode=selector.NumberSelectorMode.SLIDER,
            step=0.1,
        )
    ),
}

BUTTON_EDIT: VolDictType = {
    vol.Optional(CONF_LED, default=False): selector.BooleanSelector(),
    vol.Optional(CONF_RELEASE_DELAY, default=0): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=5,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        ),
    ),
}

DATA_SCHEMA_ADD_SWITCH = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_SWITCH_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
        vol.Optional("switch_type", default="cco"): vol.In(["cco", "cci"]),
    }
)

validate_addr = cv.matches_regex(r"\[(?:\d\d:){0,2}\d\d:\d\d:\d\d\]")


async def validate_add_controller(
    handler: ConfigFlow | SchemaOptionsFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate controller setup."""
    # TODO: Check not only password
    user_input[CONF_CONTROLLER_ID] = slugify(user_input[CONF_NAME])
    user_input[CONF_PORT] = int(user_input[CONF_PORT])
    try:
        handler._async_abort_entries_match(  # noqa: SLF001
            {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_host_port") from err

    try:
        handler._async_abort_entries_match(  # noqa: SLF001
            {CONF_CONTROLLER_ID: user_input[CONF_CONTROLLER_ID]}
        )
    except AbortFlow as err:
        raise SchemaFlowError("duplicated_controller_id") from err

    await _try_connection(user_input)

    return user_input


async def _try_connection(user_input: dict[str, Any]) -> None:
    """Try connecting to the controller."""

    def _try_connect(host: str, port: int) -> None:
        """Try connecting to the controller.

        Raises ConnectionError if the connection fails.
        """
        login_secret: str | None
        if CONF_PASSWORD in user_input:
            login_secret = f"{user_input[CONF_USERNAME]}, {user_input[CONF_PASSWORD]}"
        elif CONF_USERNAME in user_input:
            login_secret = user_input[CONF_USERNAME]
        else:
            login_secret = None
        _LOGGER.debug(
            "Trying to connect to %s:%s", user_input[CONF_HOST], user_input[CONF_PORT]
        )
        controller = Homeworks(host, port, lambda msg_types, values: None, login_secret)
        controller.connect()
        controller.close()

    hass = async_get_hass()
    try:
        await hass.async_add_executor_job(
            _try_connect, user_input[CONF_HOST], user_input[CONF_PORT]
        )
    except hw_exceptions.HomeworksConnectionFailed as err:
        _LOGGER.debug("Caught HomeworksConnectionFailed")
        raise SchemaFlowError("connection_error") from err
    except hw_exceptions.HomeworksInvalidCredentialsProvided as err:
        _LOGGER.debug("Caught HomeworksInvalidCredentialsProvided")
        raise SchemaFlowError("invalid_credentials") from err
    except hw_exceptions.HomeworksNoCredentialsProvided as err:
        _LOGGER.debug("Caught HomeworksNoCredentialsProvided")
        raise SchemaFlowError("credentials_needed") from err
    except Exception as err:
        _LOGGER.exception("Caught unexpected exception %s")
        raise SchemaFlowError("unknown_error") from err


def _validate_address(handler: SchemaCommonFlowHandler, addr: str) -> None:
    """Validate address."""
    try:
        validate_addr(addr)
    except vol.Invalid as err:
        raise SchemaFlowError("invalid_addr") from err

    for _key in (CONF_DIMMERS, CONF_KEYPADS):
        items: list[dict[str, Any]] = handler.options[_key]

        for item in items:
            if item[CONF_ADDR] == addr:
                raise SchemaFlowError("duplicated_addr")


def _validate_button_number(handler: SchemaCommonFlowHandler, number: int) -> None:
    """Validate button number."""
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]

    for button in buttons:
        if button[CONF_NUMBER] == number:
            raise SchemaFlowError("duplicated_number")


async def validate_add_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate button input."""
    user_input[CONF_NUMBER] = int(user_input[CONF_NUMBER])
    _validate_button_number(handler, user_input[CONF_NUMBER])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]
    buttons.append(user_input)
    return {}


async def validate_add_switch(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate switch input."""
    _validate_address(handler, user_input[CONF_ADDR])
    items = handler.options[CONF_SWITCHES]
    items.append(user_input)
    return {}


async def validate_add_keypad(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate keypad or light input."""
    _validate_address(handler, user_input[CONF_ADDR])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    items = handler.options[CONF_KEYPADS]
    items.append(user_input | {CONF_BUTTONS: []})
    return {}


async def validate_add_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate light input."""
    _validate_address(handler, user_input[CONF_ADDR])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    items = handler.options[CONF_DIMMERS]
    items.append(user_input)
    return {}


async def get_select_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a button."""
    keypad = handler.flow_state["_idx"]
    buttons: list[dict[str, Any]] = handler.options[CONF_KEYPADS][keypad][CONF_BUTTONS]

    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def get_select_keypad_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a keypad."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[CONF_KEYPADS])
                },
            )
        }
    )


async def get_select_light_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a light."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[CONF_DIMMERS])
                },
            )
        }
    )


async def get_select_switch_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for selecting a switch."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): vol.In(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[CONF_SWITCHES])
                },
            )
        }
    )


async def validate_select_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store button index in flow state."""
    handler.flow_state["_button_idx"] = int(user_input[CONF_INDEX])
    return {}


async def validate_select_keypad_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Store keypad or light index in flow state."""
    handler.flow_state["_idx"] = int(user_input[CONF_INDEX])
    return {}


async def get_edit_button_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for button editing."""
    keypad_idx: int = handler.flow_state["_idx"]
    button_idx: int = handler.flow_state["_button_idx"]
    return dict(handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS][button_idx])


async def get_edit_light_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for light editing."""
    idx: int = handler.flow_state["_idx"]
    return dict(handler.options[CONF_DIMMERS][idx])


async def get_edit_switch_suggested_values(
    handler: SchemaCommonFlowHandler,
) -> dict[str, Any]:
    """Return suggested values for switch editing."""
    idx: int = handler.flow_state["_idx"]
    return dict(handler.options[CONF_SWITCHES][idx])


async def validate_button_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited keypad or light."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    keypad_idx: int = handler.flow_state["_idx"]
    button_idx: int = handler.flow_state["_button_idx"]
    buttons: list[dict] = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]
    buttons[button_idx].update(user_input)
    return {}


async def validate_light_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited keypad or light."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    idx: int = handler.flow_state["_idx"]
    handler.options[CONF_DIMMERS][idx].update(user_input)
    return {}


async def validate_switch_edit(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Update edited switch."""
    # Standard behavior is to merge the result with the options.
    # In this case, we want to add a sub-item so we update the options directly.
    idx: int = handler.flow_state["_idx"]
    handler.options[CONF_SWITCHES][idx].update(user_input)
    return {}


async def get_remove_button_schema(handler: SchemaCommonFlowHandler) -> vol.Schema:
    """Return schema for button removal."""
    keypad_idx: int = handler.flow_state["_idx"]
    buttons: list[dict] = handler.options[CONF_KEYPADS][keypad_idx][CONF_BUTTONS]
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_NUMBER]})"
                    for index, config in enumerate(buttons)
                },
            )
        }
    )


async def get_remove_keypad_light_schema(
    handler: SchemaCommonFlowHandler, *, key: str
) -> vol.Schema:
    """Return schema for keypad or light removal."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(handler.options[key])
                },
            )
        }
    )


async def get_remove_switch_schema(
    handler: SchemaCommonFlowHandler
) -> vol.Schema:
    """Return schema for switch removal."""
    switches: list[dict] = handler.options[CONF_SWITCHES]
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{config[CONF_NAME]} ({config[CONF_ADDR]})"
                    for index, config in enumerate(switches)
                },
            )
        }
    )


async def validate_remove_button(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate remove keypad or light."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    keypad_idx: int = handler.flow_state["_idx"]
    keypad: dict = handler.options[CONF_KEYPADS][keypad_idx]
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(keypad[CONF_BUTTONS]):
        if str(index) not in removed_indexes:
            items.append(item)
        button_number = keypad[CONF_BUTTONS][index][CONF_NUMBER]
        for domain in (BINARY_SENSOR_DOMAIN, BUTTON_DOMAIN):
            if entity_id := entity_registry.async_get_entity_id(
                domain,
                DOMAIN,
                calculate_unique_id(
                    handler.options[CONF_CONTROLLER_ID],
                    keypad[CONF_ADDR],
                    button_number,
                ),
            ):
                entity_registry.async_remove(entity_id)
    keypad[CONF_BUTTONS] = items
    return {}


async def validate_remove_switch(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate remove switch."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(handler.options[CONF_SWITCHES]):
        if str(index) not in removed_indexes:
            items.append(item)
        if entity_id := entity_registry.async_get_entity_id(
            SWITCH_DOMAIN,
            DOMAIN,
            calculate_unique_id(
                handler.options[CONF_CONTROLLER_ID], item[CONF_ADDR], 0
            ),
        ):
            entity_registry.async_remove(entity_id)
    handler.options[CONF_SWITCHES] = items
    return {}


async def validate_remove_keypad_light(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any], *, key: str
) -> dict[str, Any]:
    """Validate remove keypad or light."""
    removed_indexes: set[str] = set(user_input[CONF_INDEX])

    # Standard behavior is to merge the result with the options.
    # In this case, we want to remove sub-items so we update the options directly.
    entity_registry = er.async_get(handler.parent_handler.hass)
    items: list[dict[str, Any]] = []
    item: dict[str, Any]
    for index, item in enumerate(handler.options[key]):
        if str(index) not in removed_indexes:
            items.append(item)
        elif key != CONF_DIMMERS:
            continue
        if entity_id := entity_registry.async_get_entity_id(
            LIGHT_DOMAIN,
            DOMAIN,
            calculate_unique_id(
                handler.options[CONF_CONTROLLER_ID], item[CONF_ADDR], 0
            ),
        ):
            entity_registry.async_remove(entity_id)
    handler.options[key] = items
    return {}


async def async_step_auto_discover(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Handle auto-discovery step."""
    if not user_input:
        return {}
    
    try:
        validate_addr(user_input["start_addr"])
        validate_addr(user_input["end_addr"])
    except vol.Invalid as err:
        raise SchemaFlowError("invalid_addr") from err
        
    return user_input

async def get_select_discovered_schema(
    handler: SchemaCommonFlowHandler
) -> vol.Schema:
    """Get schema for discovered devices selection."""
    return vol.Schema(
        {
            vol.Required(CONF_INDEX): cv.multi_select(
                {
                    str(index): f"{device.name} ({device.addr})"
                    for index, device in enumerate(handler.flow_state.get("discovered_devices", []))
                }
            )
        }
    )

async def validate_select_discovered(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate selected discovered devices."""
    return user_input

DATA_SCHEMA_ADD_CONTROLLER = vol.Schema(
    {
        vol.Required(
            CONF_NAME, description={"suggested_value": "Lutron Homeworks"}
        ): selector.TextSelector(),
        **CONTROLLER_EDIT,
    }
)
DATA_SCHEMA_EDIT_CONTROLLER = vol.Schema(CONTROLLER_EDIT)
DATA_SCHEMA_ADD_LIGHT = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_LIGHT_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
        **LIGHT_EDIT,
    }
)
DATA_SCHEMA_ADD_KEYPAD = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_KEYPAD_NAME): TextSelector(),
        vol.Required(CONF_ADDR): TextSelector(),
    }
)
DATA_SCHEMA_ADD_BUTTON = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_BUTTON_NAME): TextSelector(),
        vol.Required(CONF_NUMBER): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=24,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            ),
        ),
        **BUTTON_EDIT,
    }
)
DATA_SCHEMA_EDIT_BUTTON = vol.Schema(BUTTON_EDIT)
DATA_SCHEMA_EDIT_LIGHT = vol.Schema(LIGHT_EDIT)

OPTIONS_FLOW = {
    "init": SchemaFlowMenuStep(
        [
            "add_keypad",
            "select_edit_keypad",
            "remove_keypad",
            "add_light",
            "select_edit_light",
            "remove_light",
            "add_switch",
            "select_edit_switch",
            "remove_switch",
            "auto_discover",
            "select_discovered",
        ]
    ),
    "add_keypad": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_KEYPAD,
        suggested_values=None,
        validate_user_input=validate_add_keypad,
    ),
    "select_edit_keypad": SchemaFlowFormStep(
        get_select_keypad_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_keypad",
    ),
    "edit_keypad": SchemaFlowMenuStep(
        [
            "add_button",
            "select_edit_button",
            "remove_button",
        ]
    ),
    "add_button": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_BUTTON,
        suggested_values=None,
        validate_user_input=validate_add_button,
    ),
    "select_edit_button": SchemaFlowFormStep(
        get_select_button_schema,
        suggested_values=None,
        validate_user_input=validate_select_button,
        next_step="edit_button",
    ),
    "edit_button": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_BUTTON,
        suggested_values=get_edit_button_suggested_values,
        validate_user_input=validate_button_edit,
    ),
    "remove_button": SchemaFlowFormStep(
        get_remove_button_schema,
        suggested_values=None,
        validate_user_input=validate_remove_button,
    ),
    "remove_keypad": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_KEYPADS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_keypad_light, key=CONF_KEYPADS),
    ),
    "add_light": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_LIGHT,
        suggested_values=None,
        validate_user_input=validate_add_light,
    ),
    "select_edit_light": SchemaFlowFormStep(
        get_select_light_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_light",
    ),
    "edit_light": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_LIGHT,
        suggested_values=get_edit_light_suggested_values,
        validate_user_input=validate_light_edit,
    ),
    "remove_light": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_DIMMERS),
        suggested_values=None,
        validate_user_input=partial(validate_remove_keypad_light, key=CONF_DIMMERS),
    ),
    "add_switch": SchemaFlowFormStep(
        DATA_SCHEMA_ADD_SWITCH,
        suggested_values=None,
        validate_user_input=validate_add_switch,
    ),
    "select_edit_switch": SchemaFlowFormStep(
        get_select_switch_schema,
        suggested_values=None,
        validate_user_input=validate_select_keypad_light,
        next_step="edit_switch",
    ),
    "edit_switch": SchemaFlowFormStep(
        DATA_SCHEMA_EDIT_LIGHT,
        suggested_values=get_edit_switch_suggested_values,
        validate_user_input=validate_switch_edit,
    ),
    "remove_switch": SchemaFlowFormStep(
        partial(get_remove_keypad_light_schema, key=CONF_SWITCHES),
        suggested_values=None,
        validate_user_input=partial(validate_remove_keypad_light, key=CONF_SWITCHES),
    ),
    "auto_discover": SchemaFlowFormStep(
        {
            vol.Optional("start_addr", default="[00:00:00:00]"): str,
            vol.Optional("end_addr", default="[99:99:99:99]"): str,
        },
        suggested_values=None,
        validate_user_input=async_step_auto_discover,
    ),
    "select_discovered": SchemaFlowFormStep(
        get_select_discovered_schema,
        suggested_values=None,
        validate_user_input=validate_select_discovered,
    ),
}


class HomeworksConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    """Config flow for Lutron Homeworks."""

    async def _validate_edit_controller(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate controller setup."""
        # TODO: Check not only password
        user_input[CONF_PORT] = int(user_input[CONF_PORT])

        our_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert our_entry
        other_entries = self._async_current_entries()
        for entry in other_entries:
            if entry.entry_id == our_entry.entry_id:
                continue
            if (
                user_input[CONF_HOST] == entry.options[CONF_HOST]
                and user_input[CONF_PORT] == entry.options[CONF_PORT]
            ):
                raise SchemaFlowError("duplicated_host_port")

        await _try_connection(user_input)
        return user_input

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfigure flow."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry

        errors = {}
        suggested_values = {
            CONF_HOST: entry.options[CONF_HOST],
            CONF_PORT: entry.options[CONF_PORT],
        }

        if user_input:
            suggested_values = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
            }
            try:
                await self._validate_edit_controller(user_input)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                new_options = entry.options | {
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                }
                return self.async_update_reload_and_abort(
                    entry,
                    options=new_options,
                    reason="reconfigure_successful",
                    reload_even_if_entry_is_unchanged=False,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA_EDIT_CONTROLLER, suggested_values
            ),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors = {}
        if user_input:
            try:
                await validate_add_controller(self, user_input)
            except SchemaFlowError as err:
                errors["base"] = str(err)
            else:
                self._async_abort_entries_match(
                    {CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
                )
                name = user_input.pop(CONF_NAME)
                user_input |= {CONF_DIMMERS: [], CONF_KEYPADS: [], CONF_SWITCHES: []}
                return self.async_create_entry(title=name, data={}, options=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA_ADD_CONTROLLER,
            errors=errors,
        )

    async def async_step_auto_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle auto-discovery of devices."""
        if user_input is None:
            return self.async_show_form(
                step_id="auto_discover",
                data_schema=vol.Schema({
                    vol.Optional("start_addr", default="[00:00:00:00]"): str,
                    vol.Optional("end_addr", default="[99:99:99:99]"): str,
                }),
            )

        # Start discovery process
        data: HomeworksData = self.hass.data[DOMAIN][self.config_entry.entry_id]
        discovery = HomeworksDiscovery(data.controller)
        
        discovered = await discovery.discover_devices(
            start_addr=user_input["start_addr"],
            end_addr=user_input["end_addr"],
        )

        # Store discovered devices for the next step
        self.discovered_devices = discovered
        return await self.async_step_select_discovered()

    async def async_step_select_discovered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle selection of discovered devices."""
        if user_input is None:
            devices = []
            for addr, device in self.discovered_devices.items():
                devices.append({
                    "addr": addr,
                    "type": device.device_type,
                    "name": device.name,
                    "selected": device.selected
                })
            
            return self.async_show_form(
                step_id="select_discovered",
                data_schema=vol.Schema({
                    vol.Required(f"device_{addr}"): bool
                    for addr in self.discovered_devices
                }),
            )

        # Process selected devices
        for key, value in user_input.items():
            if key.startswith("device_"):
                addr = key[7:]  # Remove "device_" prefix
                device = self.discovered_devices[addr]
                if value:  # If device is selected
                    if device.device_type == "light":
                        self.options[CONF_DIMMERS].append({
                            CONF_ADDR: addr,
                            CONF_NAME: device.name,
                            CONF_RATE: DEFAULT_FADE_RATE
                        })
                    elif device.device_type in ["cco", "cci"]:
                        self.options[CONF_SWITCHES].append({
                            CONF_ADDR: addr,
                            CONF_NAME: device.name
                        })

        return self.async_create_entry(title="", data=self.options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchemaOptionsFlowHandler:
        """Options flow handler for Lutron Homeworks."""
        return SchemaOptionsFlowHandler(config_entry, OPTIONS_FLOW)
