def assert_billable_claude_system_key(
    *,
    machine: str | None,
    model: str | None,
    llm_id: int | None,
    is_byok: bool,
    input_token_cost: float,
    output_token_cost: float,
) -> str | None:
    """Return an error when a system-key Claude row would bill as free."""
    try:
        input_cost = float(input_token_cost or 0.0)
        output_cost = float(output_token_cost or 0.0)
    except (TypeError, ValueError):
        input_cost = 0.0
        output_cost = 0.0

    if machine != "Claude":
        return None
    if is_byok:
        return None
    if input_cost == 0 and output_cost == 0:
        return (
            "LLM configuration error: Claude system-key model has zero pricing "
            f"(llm_id={llm_id} model={model}). Refusing to bill as free."
        )
    return None
