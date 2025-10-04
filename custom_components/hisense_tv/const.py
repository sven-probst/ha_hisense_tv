"""Constants for the Hisense TV integration."""

# Attributes
ATTR_CODE = "auth_code"
CONF_MQTT_IN = "mqtt_in"
CONF_ENABLE_POLLING = "enable_polling"
CONF_MQTT_OUT = "mqtt_out"
DATA_KEY = "media_player.hisense_tv"
DEFAULT_CLIENT_ID = "HomeAssistant"
DEFAULT_MQTT_PREFIX = "hisense"
DEFAULT_NAME = "Hisense TV"
DOMAIN = "hisense_tv"

# Discovery
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"

# Services
SERVICE_SEND_KEY = "send_key"
SERVICE_SEND_CHANNEL = "send_channel"

# Service attributes
ATTR_KEY = "key"
ATTR_ENTITY_ID = "entity_id"
ATTR_CHANNEL = "channel"