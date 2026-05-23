from ai_runtime.dependencies import *

def _resolve_legacy_attachment_path(
    raw_url: str,
    current_user,
    *,
    conversation_id: int | None = None,
    expected_kind: str | None = None,
) -> tuple[str, str] | None:
    if not raw_url or current_user is None:
        return None

    raw = str(raw_url).split("?", 1)[0]
    if CLOUDFLARE_BASE_URL and raw.startswith(CLOUDFLARE_BASE_URL):
        raw = raw[len(CLOUDFLARE_BASE_URL):]

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        raw = parsed.path
    elif parsed.scheme:
        return None

    raw = urllib.parse.unquote(raw).lstrip("/")
    if not raw:
        return None

    candidate = Path(raw) if raw.startswith("data/") else Path("data") / raw
    h1, h2, user_hash = generate_user_hash(current_user.username)
    user_root = (Path(users_directory) / h1 / h2 / user_hash).resolve()

    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(user_root):
            return None
        scope_root = user_root
        if conversation_id is not None:
            conv = f"{int(conversation_id):07d}"
            scope_root = user_root / "files" / conv[:3] / conv[3:]
            if not resolved.is_relative_to(scope_root):
                return None
            rel_parts = resolved.relative_to(scope_root).parts
            if expected_kind == "image" and (len(rel_parts) < 2 or rel_parts[0] != "img"):
                return None
            if expected_kind == "pdf" and (len(rel_parts) < 2 or rel_parts[0] != "pdf" or rel_parts[1] != "uploads"):
                return None
            if expected_kind == "text" and (len(rel_parts) < 2 or rel_parts[0] != "txt"):
                return None
    except (OSError, RuntimeError, ValueError):
        return None

    data_root = Path("data").resolve()
    try:
        relative_to_data = resolved.relative_to(data_root).as_posix()
    except ValueError:
        return None
    return relative_to_data, str(resolved)
