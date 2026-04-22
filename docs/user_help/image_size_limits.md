---
id: image_size_limits
title: Why images sent to the AI are resized to 1568 pixels
category: limitations
keywords:
  - image size
  - image quality
  - image resolution
  - image resized
  - image blurry
  - screenshot quality
  - tamano imagen
  - resolucion imagen
  - imagen borrosa
  - captura de pantalla
  - 1568
  - image limits
  - provider image limits
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-04-14
---

## Short answer

When you attach an image to a chat in Aurvek, the image is resized so that the
longest side is at most 1568 pixels before it is sent to the AI. Aspect ratio is
preserved and no pixels are cropped. This cap applies only to images inside chat
messages; profile pictures, prompt avatars, and theme wallpapers are unaffected.

## Notes

- For screenshots with small text, crop the specific region you want to show.
  Several focused captures usually work better than one very large image.
- Aurvek stores and displays the resized version (1568 px or less). The original
  full-resolution bytes are not kept on the server once the resized version has
  been saved.
- This cap applies only to images you upload. It does not apply to images the
  AI generates for you (DALL-E, Ideogram, Gemini image generation, etc.).

## Related

- file_uploads
- image_generation
- limitations_file_types
