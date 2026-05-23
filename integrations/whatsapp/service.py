import os


phone_user_not_found = os.getenv(
    "WHATSAPP_UNKNOWN_USER_MESSAGE",
    "This phone number is not registered on the platform. Please contact the administrator or sign up to get started.",
)


def get_phone_user_not_found() -> str:
    return phone_user_not_found


def set_phone_user_not_found(message: str) -> None:
    global phone_user_not_found
    if message:
        phone_user_not_found = message
