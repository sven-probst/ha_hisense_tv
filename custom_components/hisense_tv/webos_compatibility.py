"""WebOS Compatibility Layer for Hisense TV integration."""
import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import ATTR_ENTITY_ID

# Import webostv domain to register compatible services
try:
    from homeassistant.components.webostv.const import DOMAIN as WEBOSTV_DOMAIN
except ImportError:
    WEBOSTV_DOMAIN = "webostv"

from .const import DOMAIN, SERVICE_SEND_TEXT, ATTR_TEXT

_LOGGER = logging.getLogger(__name__)

# Dictionary to store the last text sent via the webOS wrapper for each entity
last_webostv_text = {}


async def async_setup_webos_compatibility(hass: HomeAssistant, get_entity_func):
    """Set up the webOS compatibility services."""
    if not WEBOSTV_DOMAIN:
        _LOGGER.warning("Could not import webostv domain. Compatibility services will not be available.")
        return

    async def async_webostv_button_wrapper(call: ServiceCall):
        """Handles webostv.button and wraps it to the Hisense entity."""
        button_pressed = call.data.get("button")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("webOS button wrapper called for button: %s", button_pressed)

        if not button_pressed or not target_entity_id:
            return

        media_player = get_entity_func(target_entity_id)
        if not media_player:
            return

        key_map = {
            "LANGUAGE": "LANG", "GUIDE": "EPG", "BACK": "RETURNS",
            "ENTER": "OK", "CENTER": "OK", "VOLUME_UP": "VOLUMEUP",
            "VOLUME_DOWN": "VOLUMEDOWN", "CAPTIONS": "SUBTITLE", "CC": "SUBTITLE",
        }
        hisense_key = key_map.get(button_pressed.upper(), button_pressed.upper())
        await media_player.async_send_key(hisense_key)

    async def async_webostv_launch_wrapper(call: ServiceCall):
        """Handles webostv.launch and wraps it to the Hisense entity."""
        app_id = call.data.get("app_id")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("webOS launch wrapper called for app_id: %s", app_id)

        if not app_id or not target_entity_id:
            return

        media_player = get_entity_func(target_entity_id)
        if media_player:
            await media_player.async_launch_app(app_id.capitalize())

    async def async_webostv_command_wrapper(call: ServiceCall):
        """Handles webostv.command (for keyboard, media controls) and wraps it."""
        _LOGGER.debug("webOS command wrapper called with data: %s", call.data)
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        if not target_entity_id:
            return

        media_player = get_entity_func(target_entity_id)
        if not media_player:
            return

        command = call.data.get("command")
        payload = call.data.get("payload", {})
        text_to_send = payload.get("text")

        # Case 1: Handle text input from keyboard
        if text_to_send is not None:
            if target_entity_id not in last_webostv_text:
                initial_text = getattr(media_player, '_input_text', None) or ""
                last_webostv_text[target_entity_id] = initial_text
            old_text = last_webostv_text.get(target_entity_id, "")

            common_prefix_len = 0
            while common_prefix_len < len(old_text) and common_prefix_len < len(text_to_send) and old_text[common_prefix_len] == text_to_send[common_prefix_len]:
                common_prefix_len += 1

            backspaces_needed = len(old_text) - common_prefix_len
            text_to_append = text_to_send[common_prefix_len:]
            final_text_to_send = ("\b" * backspaces_needed) + text_to_append

            await media_player.async_send_text(final_text_to_send)
            last_webostv_text[target_entity_id] = text_to_send
            return

        # Case 2: Handle pre-filling of the text input field
        elif command == "system.launcher/getForegroundAppInfo":
            if hasattr(media_player, '_input_text'):
                hass.bus.async_fire(f"webostv_response_{target_entity_id.replace('.', '_')}", {"payload": {"text": media_player._input_text}})
            return

        # Case 3: Handle generic commands (media controls, etc.)
        elif command:
            command_map = {
                "media.controls/stop": "STOP", "media.controls/play": "PLAY",
                "media.controls/pause": "PAUSE", "media.controls/rewind": "BACK",
                "media.controls/fastForward": "FORWARDS",
                "com.webos.service.ime/deleteCharacters": "BACKSPACE",
                "com.webos.service.ime/sendEnterKey": "LIT_ENTER_SPECIAL",
            }
            hisense_key = command_map.get(command)
            if hisense_key:
                if hisense_key == "LIT_ENTER_SPECIAL":
                    last_webostv_text.pop(target_entity_id, None)
                    await media_player.async_send_text("\n")
                else:
                    await media_player.async_send_key(hisense_key)
                    if hisense_key == "BACKSPACE":
                        old_text = last_webostv_text.get(target_entity_id, "")
                        last_webostv_text[target_entity_id] = old_text[:-1]

    _LOGGER.debug("Registering webOS compatibility services.")
    hass.services.async_register(
        WEBOSTV_DOMAIN, "button", async_webostv_button_wrapper
    )
    hass.services.async_register(
        WEBOSTV_DOMAIN, "launch", async_webostv_launch_wrapper
    )
    hass.services.async_register(
        WEBOSTV_DOMAIN, "command", async_webostv_command_wrapper
    )


async def async_unload_webos_compatibility(hass: HomeAssistant):
    """Unload the webOS compatibility services."""
    if not WEBOSTV_DOMAIN:
        return

    _LOGGER.debug("Unregistering webOS compatibility services.")
    hass.services.async_remove(WEBOSTV_DOMAIN, "button")
    hass.services.async_remove(WEBOSTV_DOMAIN, "launch")
    hass.services.async_remove(WEBOSTV_DOMAIN, "command")
    last_webostv_text.clear()