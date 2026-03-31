---
id: telegram_commands
title: Telegram bot commands
category: telegram
keywords:
  - telegram
  - commands
  - bot
  - help
  - text
  - voice
  - prompt
  - new conversation
  - unlink
  - comandos
  - ayuda
  - voz
  - texto
  - nuevo
  - desvincular
  - list conversations
  - switch conversation
  - chats
  - set conversation
  - listar
  - cambiar
prerequisites:
  - A Telegram account linked to Aurvek (see telegram_setup)
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
---

## Short answer

The Aurvek Telegram bot supports commands prefixed with `!`. You can switch between text and voice responses, change your active prompt, list and switch conversations, start a new one, or unlink your account. Type `!help` in Telegram for the full list.

## Steps

1. **!help** -- Shows all available commands.
2. **!text** -- Switches AI responses to text mode.
3. **!voice** -- Switches AI responses to voice mode (text-to-speech audio). Falls back to text if generation fails.
4. **!prompt list** -- Lists prompts (AI personalities) you can access, with ID and name. Up to 20 shown.
5. **!prompt \<name or id\>** -- Switches the active prompt. Use the prompt ID or name (partial matching supported).
6. **!new** -- Starts a new conversation. Your previous one is saved and accessible from the web.
7. **!unlink** -- Unlinks your Telegram account. The bot will no longer recognize you until you link again.
8. **!chats** -- Lists your recent conversations (up to 15) with ID, title, message count, and last activity. Your active conversation is marked with `->`.
9. **!set \<id\> [platform]** -- Switches your active conversation. `!set 1234` assigns to Telegram, `!set 1234 whatsapp` to WhatsApp. Aliases: `wa`, `tg`. Optional `#` prefix.

## Notes

- Commands are case-insensitive (`!Help`, `!HELP`, `!help` all work).
- You can also send regular text, voice messages, or photos. Voice messages are auto-transcribed.
- If your conversation is locked, send `!new` to start a fresh one.
- Long responses are auto-split for Telegram's length limits.
- `!prompt` uses smart matching: exact ID, then exact name, then partial name.

## Related

- telegram_setup
- telegram_unlink
- external_manage_conversations
