from ai_runtime.dependencies import *

def build_citation_event(citations: list, search_queries: list = None,
                         google_widget_html: str = None) -> str:
    """
    Build a unified citation SSE event from any provider's native web search results.

    Each provider (Claude, Gemini, OpenAI, xAI) normalizes its citations into
    the standard format before calling this function.

    Args:
        citations: List of dicts, each with:
            - url (str, required): Source URL
            - title (str, required): Source page title
            - cited_text (str, optional): The quoted/referenced text
            - start_index (int, optional): Character position in response text
            - end_index (int, optional): Character position in response text
        search_queries: List of search queries the model executed (optional)
        google_widget_html: Gemini searchEntryPoint HTML (mandatory per Google ToS when present)

    Returns:
        SSE-formatted string: "data: {json}\n\n"
    """
    event = {
        "type": "web_search_citations",
        "citations": citations,
    }
    if search_queries:
        event["search_queries"] = search_queries
    if google_widget_html:
        event["google_search_widget_html"] = google_widget_html
    return f"data: {orjson.dumps(event).decode()}\n\n"
