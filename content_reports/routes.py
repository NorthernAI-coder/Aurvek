"""Public API for reporting objectionable or unsafe content."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from auth import get_current_user, unauthenticated_response
from content_reports.service import (
    VALID_REPORT_REASONS,
    VALID_REPORT_TARGET_TYPES,
    create_content_report,
    ensure_content_reports_schema,
    resolve_report_target,
)
from database import get_db_connection
from models import User


router = APIRouter()


class ContentReportRequest(BaseModel):
    target_type: str = Field(..., min_length=1, max_length=32)
    target_id: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=64)
    details: str | None = Field(default=None, max_length=2000)

    @field_validator("target_type")
    @classmethod
    def normalize_target_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_REPORT_TARGET_TYPES:
            raise ValueError("Unsupported report target_type")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_REPORT_REASONS:
            raise ValueError("Unsupported report reason")
        return normalized

    @field_validator("details")
    @classmethod
    def normalize_details(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


@router.post("/api/reports/content")
async def report_content(
    payload: ContentReportRequest,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection() as conn:
        await ensure_content_reports_schema(conn)
        target = await resolve_report_target(
            conn,
            target_type=payload.target_type,
            target_id=payload.target_id,
            reporter_user_id=current_user.id,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Report target not found")

        report_id = await create_content_report(
            conn,
            reporter_user_id=current_user.id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            target_owner_user_id=target["target_owner_user_id"],
            reason=payload.reason,
            details=payload.details,
            metadata=target["metadata"],
        )
        await conn.commit()

    return JSONResponse(
        {
            "success": True,
            "report_id": report_id,
            "status": "open",
            "message": "Report submitted",
        },
        status_code=201,
    )
