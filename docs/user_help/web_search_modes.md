---
id: web_search_modes
title: Web search modes explained
category: search
keywords:
  - native search
  - perplexity
  - search mode
  - search engine
  - modo de busqueda
  - motor de busqueda
required_role: public
tool_visible: true
approval_status: approved
last_reviewed: 2026-03-22
---

## Short answer

Aurvek offers two web search modes: **Native** and **Perplexity**. Native mode uses the AI model's built-in search capability and is recommended for most users. Perplexity mode uses Perplexity AI as an external search service. You can switch between them in your account settings.

## Steps

1. Go to your **Settings** page (click your profile icon, then Settings).
2. Find the **Web Search** section.
3. Choose one of the two search engines:
   - **Native** (Recommended) -- Uses the AI model's built-in web search. Faster and more integrated with the conversation context.
   - **Perplexity** -- Routes search queries to Perplexity AI (sonar-pro model) as a separate service. If Perplexity is not configured on the server, this option appears disabled.
4. Save your profile settings. The change takes effect on your next message.

## Notes

- Native search is available for Claude, GPT, and xAI models. If you are using a model that does not support native search (such as Gemini), the system automatically falls back to Perplexity mode.
- In Native mode, the AI model performs the search itself and integrates the results directly into its response, often including inline citations.
- In Perplexity mode, the AI calls Perplexity as a tool, retrieves the search results, and then formulates its own answer using those results. This means Perplexity mode involves a two-step process.
- Your search mode preference applies across all conversations. Individual prompts can override this by forcing search on or off.

## Related

- web_search_usage
