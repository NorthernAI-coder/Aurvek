---
id: chat_export_mp3
title: Exporting a conversation to audio (MP3)
category: chat
keywords:
  - mp3
  - audio
  - export
  - download
  - descargar
  - exportar audio
  - text to speech
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

You can export an entire conversation as an MP3 audio file. Every message (both user and bot) is converted to speech using text-to-speech, then combined into a single downloadable MP3.

## Steps

1. In the chat sidebar, click the three-dot menu on the conversation you want to export.
2. Select **Download MP3**.
3. Confirm the action in the dialog that appears.
4. The system queues the MP3 generation in the background. A message confirms it has started.
5. Once generation is complete, open the **Media Gallery** to find and download the MP3 file.

## Notes

- MP3 generation runs as a background task and may take some time depending on the length of the conversation.
- Bot messages use the voice assigned to the prompt. User messages use a default voice.
- The generated file is named using the prompt name and a timestamp (e.g., `My_Prompt_2026_03_22_14_30_00.mp3`).
- If a generation is already in progress, you will need to wait a few minutes before requesting another one for the same conversation.
- MP3 export uses your account balance for TTS costs, the same way as listening to individual messages.

## Related

- chat_export_pdf
- tts_listen_messages
