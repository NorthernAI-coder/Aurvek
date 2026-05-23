ALLOWED_PLATFORMS = {"whatsapp", "telegram"}

PLATFORM_LABELS = {
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
}


def validate_platform(platform: str) -> bool:
    return platform in ALLOWED_PLATFORMS


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)
