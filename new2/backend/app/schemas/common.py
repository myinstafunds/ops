"""Shared response envelopes and list query models (PRD 36, 37)."""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Documented failure shape. Registered in ``app.main`` for OpenAPI."""

    error: ErrorDetail
    trace_id: str | None = None


class PageMeta(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_next: bool
    has_previous: bool


class PageResponse(BaseModel, Generic[T]):
    """Envelope for every list endpoint."""

    data: list[T]
    meta: PageMeta
    trace_id: str | None = None


class MessageResponse(BaseModel):
    message: str
    trace_id: str | None = None


class OkResponse(BaseModel):
    ok: bool = True
    trace_id: str | None = None


def page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return PageMeta(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


class PageParams(BaseModel):
    """Validated pagination / sorting parameters.

    Declared as a dependency so ``page_size`` above the server maximum is a
    422 rather than a silently ignored value (PRD 37).
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="1-based page number")
    page_size: int = Field(
        default=settings.ops_default_page_size,
        ge=1,
        le=settings.ops_max_page_size,
        description=f"Items per page (max {settings.ops_max_page_size})",
    )
    sort: str | None = Field(default=None, max_length=64)
    order: Literal["asc", "desc"] = "desc"

    @field_validator("page_size")
    @classmethod
    def _cap_page_size(cls, value: int) -> int:
        return min(value, settings.ops_max_page_size)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def descending(self) -> bool:
        return self.order == "desc"


SortParam = Annotated[str | None, Field(default=None, max_length=64)]
