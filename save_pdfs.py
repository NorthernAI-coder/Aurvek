# save_pdfs.py

import os

import fitz  # PyMuPDF

from common import MAX_PDF_PAGES


def validate_pdf(pdf_data: bytes, enforce_page_limit: bool = True) -> int:
    """Validate PDF and return page count. Raises ValueError if invalid or above the hard cap."""
    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception:
        raise ValueError("Invalid or corrupted PDF file")
    try:
        page_count = len(doc)
    finally:
        doc.close()
    if enforce_page_limit and page_count > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds {MAX_PDF_PAGES} page limit ({page_count} pages)")
    return page_count


def extract_pdf_page_range(pdf_data: bytes, start_page: int, end_page: int) -> tuple[bytes, int, int]:
    """Return a new PDF containing the 1-based inclusive page range."""
    try:
        start = int(start_page)
        end = int(end_page)
    except (TypeError, ValueError):
        raise ValueError("Invalid PDF page range")

    if start < 1 or end < start:
        raise ValueError("Invalid PDF page range")

    try:
        doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception:
        raise ValueError("Invalid or corrupted PDF file")

    out = None
    try:
        total_pages = len(doc)
        if end > total_pages:
            raise ValueError(f"PDF page range exceeds document length ({total_pages} pages)")
        selected_pages = end - start + 1
        if selected_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF page range exceeds {MAX_PDF_PAGES} page limit ({selected_pages} pages)")

        out = fitz.open()
        out.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
        ranged_data = out.write(garbage=4, deflate=True)
        return ranged_data, selected_pages, total_pages
    finally:
        if out is not None:
            out.close()
        doc.close()


def extract_pdf_text_local(pdf_data: bytes) -> str:
    """Extract text from PDF locally via PyMuPDF. Used only for O1 (text-only legacy provider)."""
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    try:
        pages_text = []
        for page in doc:
            text = page.get_text("text")
            if text.strip():
                pages_text.append(text)
        return "\n\n---\n\n".join(pages_text)
    finally:
        doc.close()


def get_or_extract_pdf_text(pdf_file_path: str) -> str:
    """Get extracted text for a PDF, using disk cache if available. O1 only."""
    cache_path = pdf_file_path.rsplit('.pdf', 1)[0] + '.extracted.txt'

    # Check cache first
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8') as f:
            return f.read()

    # Extract locally via PyMuPDF
    with open(pdf_file_path, 'rb') as f:
        pdf_data = f.read()

    extracted_text = extract_pdf_text_local(pdf_data)

    # Cache to disk
    with open(cache_path, 'w', encoding='utf-8') as f:
        f.write(extracted_text)

    return extracted_text
