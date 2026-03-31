---
id: external_platforms_overview
title: Using WhatsApp and Telegram with Aurvek
category: chat
keywords:
  - external platforms
  - whatsapp
  - telegram
  - messaging
  - plataformas externas
  - mensajeria
  - whatsapp y telegram
  - whatsapp and telegram
  - both platforms
  - ambas plataformas
  - assign conversation
  - asignar conversacion
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
---

## Short answer

You can continue your Aurvek conversations on WhatsApp or Telegram. Each conversation can only be assigned to one external platform at a time. Assigning a conversation to WhatsApp automatically removes it from Telegram, and vice versa.

## Steps

1. Open the chat sidebar and find the conversation you want to use externally.
2. Click the three-dot menu on the conversation.
3. Select **Use for WhatsApp** or assign it to Telegram (via the Telegram bot).
4. Messages you send from the external app will go to that conversation, and everything stays synced with the web.
5. To stop using an external platform, open the same menu and remove the assignment.

## Notes

- **One platform per conversation.** A conversation cannot be on both WhatsApp and Telegram simultaneously. Assigning it to one removes it from the other.
- **One conversation per platform.** You can only have one active conversation per external platform. Assigning a new conversation to WhatsApp replaces the previous WhatsApp assignment.
- **Phone number required for WhatsApp.** You need a verified phone number in Settings before using WhatsApp.
- **Telegram linking.** To use Telegram, link your account through the Aurvek Telegram bot first (see telegram_setup).
- Both platforms support text messages, images, and voice messages. Document attachments (Word, spreadsheets, etc.) are not supported on external platforms.
- Commands like `!help`, `!new`, `!text`, `!voice`, `!prompt`, `!chats`, and `!set` work on both WhatsApp and Telegram.
- `!chats` and `!set` let you list and switch conversations directly from the messaging app without opening the web interface.

## Related

- whatsapp_continue_conversation
- whatsapp_commands
- telegram_setup
- telegram_commands
- external_manage_conversations
