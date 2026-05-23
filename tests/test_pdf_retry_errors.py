from __future__ import annotations


class DummyUser:
    id = 42


def test_claude_pdf_page_limit_error_becomes_retry_payload() -> None:
    from ai_runtime.errors import _provider_error_payload

    payload = _provider_error_payload(
        "Claude",
        "invalid_request_error: A maximum of 100 PDF pages may be provided.",
        pdf_metadata={
            "filename": "long.pdf",
            "pages": 120,
            "pdf_count": 1,
            "current_pdf_count": 1,
            "context_pdf_count": 0,
            "range_retry_available": True,
            "retry_filename": "long.pdf",
            "retry_pages": 120,
            "retry_file_hash": "a" * 40,
            "pdfs": [{"filename": "long.pdf", "pages": 120, "file_hash": "a" * 40}],
        },
        current_user=DummyUser(),
        conversation_id=77,
    )

    assert payload["error_code"] == "pdf_too_large"
    assert payload["pdf_too_large"] is True
    assert payload["provider"] == "Claude"
    assert payload["pages"] == 120
    assert payload["retry_token"]
    assert payload["retry_reason"] == "pdf_limit"


def test_claude_prompt_too_long_with_current_pdf_becomes_smaller_range_retry() -> None:
    from ai_runtime.errors import _provider_error_payload

    payload = _provider_error_payload(
        "Claude",
        "invalid_request_error: prompt is too long: 200651 tokens > 200000 maximum",
        pdf_metadata={
            "filename": "long_pages_1-100.pdf",
            "pages": 100,
            "pdf_count": 1,
            "current_pdf_count": 1,
            "context_pdf_count": 0,
            "range_retry_available": True,
            "retry_filename": "long_pages_1-100.pdf",
            "retry_pages": 100,
            "retry_file_hash": "b" * 40,
            "retry_source_hash": "a" * 40,
            "retry_source_pages": 129,
            "pdfs": [{
                "filename": "long_pages_1-100.pdf",
                "pages": 100,
                "file_hash": "b" * 40,
                "retry_source_hash": "a" * 40,
                "retry_source_pages": 129,
            }],
        },
        current_user=DummyUser(),
        conversation_id=77,
    )

    assert payload["error_code"] == "pdf_too_large"
    assert payload["retry_reason"] == "token_limit"
    assert payload["suggested_page_end"] == 80
    assert "fewer pages" in payload["retry_hint"]
    assert payload["retry_token"]


def test_prompt_too_long_without_current_pdf_stays_plain_error() -> None:
    from ai_runtime.errors import _provider_error_payload

    payload = _provider_error_payload(
        "Claude",
        "invalid_request_error: prompt is too long: 200651 tokens > 200000 maximum",
        pdf_metadata={
            "filename": "old-context.pdf",
            "pages": 120,
            "pdf_count": 1,
            "current_pdf_count": 0,
            "context_pdf_count": 1,
            "range_retry_available": False,
            "pdfs": [{"filename": "old-context.pdf", "pages": 120, "file_hash": "c" * 40}],
        },
        current_user=DummyUser(),
        conversation_id=77,
    )

    assert payload == {"error": "invalid_request_error: prompt is too long: 200651 tokens > 200000 maximum"}


def test_ranged_pdf_warning_tells_model_only_selected_pages_are_attached() -> None:
    from ai_runtime.attachments.pdf import _ranged_pdf_warning_text

    warning = _ranged_pdf_warning_text(
        "book_pages_1-80.pdf",
        page_start=1,
        page_end=80,
        source_page_count=129,
    )

    assert warning.startswith("[WARNING]")
    assert "contains only pages 1-80 of the original 129-page PDF" in warning
    assert "Pages outside that range are not attached" in warning
    assert "treat those as references to missing pages" in warning
