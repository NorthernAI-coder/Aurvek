---
id: plus_menu_overview
title: Overview of the plus menu options
category: chat
keywords:
  - plus menu
  - tools menu
  - attachments
  - features
  - menu
  - herramientas
  - menu mas
  - opciones
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

The plus (+) menu is a toolbar located at the bottom-left of the chat input area. It gives you quick access to file attachments, audio recording, web search, thinking tokens, AI voice calls, and multi-AI comparison. A small indicator dot appears on the button when any feature is active.

## Steps

1. Click the **+** button next to the message input to open the menu.
2. The menu contains the following options:
   - **Attach files** -- Opens a file picker to attach images or PDFs to your message. Only visible if the current model supports vision.
   - **Record audio** -- Starts a voice recording using your microphone. The recording is transcribed to text and inserted into the message input.
   - **Thinking tokens** -- Adjusts the thinking token budget for deeper reasoning. Only visible for models that support extended thinking (Claude 3.7/4+). Use the slider to set a budget from 0 to 20,000 tokens.
   - **Web search** -- Toggles real-time web search on or off. When active, the AI can search the internet for current information. Some prompts may force this on or hide it entirely.
   - **AI Voice** -- Opens the voice call overlay to start a real-time voice conversation with the AI using ElevenLabs.
   - **Multi-AI Compare** -- Sends your message to multiple AI models simultaneously and shows their responses side by side. Only visible when the feature is available for the current conversation.
3. Click outside the menu or select an option to close it.

## Notes

- Not all options are visible at all times. Availability depends on the current model, prompt configuration, and your account permissions.
- The plus button shows a small dot indicator when thinking tokens are set above zero, web search is enabled, or multi-AI mode is active.
- The Thinking tokens and Web search options only appear when a conversation is selected and the model supports those features.

## Related

- file_uploads
- web_search_usage
- voice_calls_usage
- image_generation
- video_generation
