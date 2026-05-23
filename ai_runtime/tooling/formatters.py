from ai_runtime.dependencies import *

def tools_for_openai(tools: list) -> list:
    """
    Format tools for OpenAI, xAI, and OpenRouter APIs.

    These APIs use the same format as tools_in_app (OpenAI format),
    so we just filter out 'sendToAI' which is only used by semantic router.

    Returns:
        List of tools in OpenAI format, excluding sendToAI
    """
    return [t for t in tools if t['function']['name'] != 'sendToAI']


def tools_for_openai_responses(tools: list, web_search_mode: str = None) -> list:
    """
    Convert tools from OpenAI Chat Completions format to Responses API format.

    Chat Completions: {type: "function", function: {name, description, parameters}, strict: true}
    Responses API:    {type: "function", name, description, parameters, strict: true}

    Also prepends the web_search tool when native search is active.
    web_search_mode is already filtered upstream (set to None when search is disabled).
    """
    result = []

    # Add native web search tool if enabled (upstream already set mode to None when disabled)
    if web_search_mode == 'native':
        result.append({
            "type": "web_search",
            "search_context_size": "medium",
        })

    # Flatten function tools from Chat Completions format to Responses API format
    for t in tools:
        fn = t.get('function', {})
        if fn.get('name') == 'sendToAI':
            continue
        flat = {
            "type": "function",
            "name": fn.get('name'),
            "description": fn.get('description', ''),
            "parameters": fn.get('parameters', {}),
        }
        # Don't propagate strict: true — Responses API enforces that ALL properties
        # must be in 'required' when strict is enabled, which many of our tool schemas
        # don't comply with. Not needed for our use case (no structured outputs).
        result.append(flat)

    return result


def tools_for_xai_responses(tools: list, web_search_mode: str = None) -> list:
    """
    Convert tools from OpenAI Chat Completions format to xAI Responses API format.

    Same flat format as OpenAI Responses API, plus xAI-specific search tools
    (web_search and x_search) when native search mode is active.
    """
    result = []

    if web_search_mode == 'native':
        result.append({"type": "web_search"})
        result.append({"type": "x_search"})

    for t in tools:
        fn = t.get('function', {})
        if fn.get('name') == 'sendToAI':
            continue
        flat = {
            "type": "function",
            "name": fn.get('name'),
            "description": fn.get('description', ''),
            "parameters": fn.get('parameters', {}),
        }
        result.append(flat)

    return result


def tools_for_claude(tools: list) -> list:
    """
    Convert tools from OpenAI format to Anthropic Claude format.

    OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            },
            "strict": True
        }

    Claude format:
        {
            "name": "...",
            "description": "...",
            "input_schema": {...}
        }

    Returns:
        List of tools in Anthropic format, excluding sendToAI
    """
    result = []
    for tool in tools:
        func = tool.get('function', {})
        name = func.get('name', '')

        # Skip sendToAI - it's only for semantic router
        if name == 'sendToAI':
            continue

        result.append({
            "name": name,
            "description": func.get('description', ''),
            "input_schema": func.get('parameters', {"type": "object", "properties": {}})
        })

    return result


def _sanitize_schema_for_gemini(schema: dict) -> dict:
    """Recursively sanitize a JSON Schema dict for Gemini compatibility.

    Gemini's SDK (Pydantic) only accepts single-string type values
    (e.g. 'STRING', 'ARRAY'), not union arrays like ['array', 'null'].
    Also strips unsupported keys like 'additionalProperties'.
    """
    schema = schema.copy()

    # Fix union types: ["array", "null"] -> "array"
    if isinstance(schema.get("type"), list):
        non_null = [t for t in schema["type"] if t != "null"]
        schema["type"] = non_null[0] if non_null else "string"

    # Remove unsupported keys
    schema.pop("additionalProperties", None)

    # Recurse into properties
    if "properties" in schema and isinstance(schema["properties"], dict):
        schema["properties"] = {
            k: _sanitize_schema_for_gemini(v)
            for k, v in schema["properties"].items()
        }

    # Recurse into items (for array types)
    if "items" in schema and isinstance(schema["items"], dict):
        schema["items"] = _sanitize_schema_for_gemini(schema["items"])

    return schema


def tools_for_gemini(tools: list) -> list:
    """Convert tools from OpenAI format to Gemini FunctionDeclaration dicts.

    Returns a flat list of declaration dicts (not wrapped). The caller
    wraps them via genai_types.Tool(function_declarations=declarations).
    """
    declarations = []
    for tool in tools:
        func = tool.get('function', {})
        name = func.get('name', '')

        # Skip sendToAI - it's only for semantic router
        if name == 'sendToAI':
            continue

        params = func.get('parameters', {"type": "object", "properties": {}})
        params = _sanitize_schema_for_gemini(params)

        declarations.append({
            "name": name,
            "description": func.get('description', ''),
            "parameters": params
        })

    return declarations
