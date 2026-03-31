---
id: tts_listen_messages
title: Listening to AI messages with text-to-speech
category: chat
keywords:
  - tts
  - text to speech
  - listen
  - audio
  - escuchar
  - voz
  - play message
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

You can listen to any message in a conversation by clicking the speaker icon. The text is converted to speech and streamed to your browser in real time. If the audio has been played before, it loads instantly from cache.

## Steps

1. Hover over any message (or look at the action icons on a bot message) and click the **speaker icon** (volume icon).
2. The icon changes to an hourglass while audio is loading.
3. Once playback starts, the icon changes to a **stop** icon. Click it to stop playback at any time.
4. To listen to a different message, click its speaker icon. Any currently playing audio will stop automatically.

## Notes

- The system first checks if the audio is already cached. If it is, playback starts immediately without regenerating.
- If not cached, audio is generated and streamed via WebSocket in real time, so playback begins before the full message is processed.
- TTS uses your account balance. Each generation is billed; cached playback is free.
- If you have insufficient balance, a notification will appear instead of playing audio.
- Both user and bot messages can be played. The voice used depends on the message author and the prompt's voice configuration.

## Related

- audio_input_stt
- chat_export_mp3
