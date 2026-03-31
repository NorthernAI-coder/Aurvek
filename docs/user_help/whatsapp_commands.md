---
id: whatsapp_commands
title: Available WhatsApp commands
category: whatsapp
keywords:
  - whatsapp commands
  - whatsapp help
  - comandos whatsapp
  - ayuda whatsapp
  - text mode
  - voice mode
  - change prompt
  - new conversation
  - prompt list
  - list conversations
  - switch conversation
  - chats
  - set conversation
prerequisites:
  - An active WhatsApp conversation assigned to your account
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-31
---

## Short answer

WhatsApp supports several commands that let you control your conversation without leaving the chat. All commands start with `!` and are case-insensitive.

## Steps

1. **!help** -- Shows the list of available commands directly in WhatsApp.
2. **!text** -- Switches responses to text mode. The AI will reply with written messages. You can also type `text mode` or `text_mode`.
3. **!voice** -- Switches responses to voice mode. The AI will reply with audio messages. You can also type `voice mode` or `voice_mode`.
4. **!prompt list** -- Lists up to 20 prompts you have access to, showing their ID and name.
5. **!prompt \<name or id\>** -- Switches the active conversation to a different prompt. You can use the prompt's numeric ID or its name (partial matches work). Example: `!prompt 42` or `!prompt email campaigns`.
6. **!new** -- Starts a new conversation. The previous conversation is saved and remains accessible from the web interface.
7. **!chats** -- Lists your recent conversations with their ID, title, message count, last activity date, and platform badges. Your active WhatsApp conversation is marked with `->`.
8. **!set \<id\> [platform]** -- Switches your active conversation. Use `!set 1234` to assign conversation #1234 to WhatsApp, or `!set 1234 telegram` to move it to Telegram. Short aliases `wa` and `tg` are supported, and the ID can be written with or without `#`.

## Notes

- Commands are processed before the AI sees your message, so the AI will never receive a command as input.
- When switching prompts with `!prompt`, the system first tries an exact ID match, then an exact name match (case-insensitive), then a partial name match.
- If the current conversation is locked, regular messages are blocked. Send `!new` to start a fresh conversation.
- Voice mode responses use text-to-speech to generate audio. This may consume additional balance depending on your plan.

## Related

- whatsapp_continue_conversation
- whatsapp_setup_phone
- external_manage_conversations
