---
id: telegram_unlink
title: How to unlink Telegram from your Aurvek account
category: telegram
keywords:
  - telegram
  - unlink
  - disconnect
  - remove
  - desvincular
  - desconectar
  - eliminar
prerequisites:
  - A Telegram account currently linked to Aurvek (see telegram_setup)
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

Send `!unlink` to the Aurvek bot in Telegram. This immediately removes the link between your Telegram and Aurvek accounts.

## Steps

1. Open the Aurvek bot conversation in Telegram.
2. Type `!unlink` and send the message.
3. The bot will confirm with: "Your Telegram has been unlinked from your account."
4. The bot will no longer recognize your Telegram account. Any further messages will prompt you to go through the linking process again.

## Notes

- Unlinking does not delete your Aurvek account or any of your conversations. Your chat history remains accessible from the Aurvek web interface.
- Unlinking does not delete the Telegram chat. The messages in your Telegram app stay as they are, but the bot will stop responding to you as a linked user.
- If you want to re-link the same Telegram account afterward, send any message to the bot and follow the phone number sharing process again (see telegram_setup).
- There is currently no way to unlink Telegram from the Aurvek web interface; it can only be done through the bot itself.
- If you are an administrator and need to unlink a user's Telegram account, you can do so from the admin panel by clearing their `telegram_chat_id` field.

## Related

- telegram_setup
- telegram_commands
