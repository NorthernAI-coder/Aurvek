---
id: chat_export_pdf
title: Exporting a conversation to PDF
category: chat
keywords:
  - pdf
  - export
  - download
  - descargar
  - exportar
  - conversation pdf
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

You can export any conversation as a PDF document. The PDF includes all messages (user and bot), formatted text with markdown rendering, images, code blocks, tables, and timestamps.

## Steps

1. In the chat sidebar, click the three-dot menu on the conversation you want to export.
2. Select **Download PDF**.
3. Confirm the action in the dialog that appears.
4. The system queues the PDF generation in the background. A message confirms it has started.
5. Once generation is complete, open the **Media Gallery** to find and download the PDF file.

## Notes

- PDF generation runs as a background task, so you can continue using the chat while it processes.
- The generated PDF is named using the prompt name and a timestamp (e.g., `My_Prompt_2026_03_22_14_30_00.pdf`).
- Images in messages are embedded in the PDF. If an image is no longer available, a placeholder is shown instead.
- Multi-AI compare responses are included in the PDF with each model's response labeled separately.
- If a generation is already in progress, you will need to wait a few minutes before requesting another one for the same conversation.
- Emoji characters are rendered using a dedicated emoji font (Noto Emoji).

## Related

- chat_export_mp3
- chat_folders
