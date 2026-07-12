"""Shared constants for external device data contracts."""

DEVICE_TOKEN_PREFIX = "avd_"

DEVICE_BINDING_TARGETS = {"device", "group"}
DEVICE_EVENT_DIRECTIONS = {"in", "out", "system"}

BASIC_CAPABILITIES = ("listen", "speak", "snapshot", "ptz")
DEVICE_RESPONSE_MODES = {"text"}

MAX_DEVICE_MESSAGE_CHARS = 20000
