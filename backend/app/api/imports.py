"""CSV employee import (FR-5): template, validate-and-preview, confirm.

Uploads are multipart (`file`). Validation reports every problem with its row, column
and reason; confirm re-validates against the current database and saves all rows or
none.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession
from app.domain.csv_import import ImportResult
from app.schemas.catalog import NonEmpty
from app.schemas.imports import (
    ImportConfirmed,
    ImportRowOut,
    RowErrorOut,
    ValidationReport,
)
from app.services import imports as import_service

router = APIRouter(prefix="/import", tags=["import"])

PREVIEW_LIMIT = 100
TEMPLATE_FILENAME = "employee_import_template.csv"

Upload = Annotated[UploadFile, File(description="CSV in the template's format")]


def read_upload(file: UploadFile) -> bytes:
    # Read one byte past the limit so an oversized file is detected without
    # holding more than that in memory.
    return file.file.read(import_service.MAX_UPLOAD_BYTES + 1)


def report(result: ImportResult) -> ValidationReport:
    return ValidationReport(
        ok=result.ok,
        row_count=len(result.rows),
        errors=[RowErrorOut(**vars(e)) for e in result.errors],
        preview=[ImportRowOut(**vars(r)) for r in result.rows[:PREVIEW_LIMIT]],
    )


@router.get("/template")
def download_template() -> Response:
    """The CSV header HR fills in, one row per employee (base pay only; other
    compensation types are added afterwards as compensation changes)."""
    return Response(
        content=import_service.template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


@router.post("/validate", response_model=ValidationReport)
def validate_import(file: Upload, db: DbSession) -> ValidationReport:
    """Validate the whole file and preview it. Nothing is saved."""
    return report(import_service.validate_file(db, read_upload(file)))


@router.post(
    "/confirm",
    response_model=ImportConfirmed,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ValidationReport}},
)
def confirm_import(
    file: Upload, changed_by: Annotated[NonEmpty, Form()], db: DbSession
) -> ImportConfirmed | JSONResponse:
    """Re-validate and save every row, or nothing. Each employee gets a "new hire"
    base-pay record effective on their hire date. A file with any error is a 422
    with the same report as /validate."""
    with translate_db_errors(db):
        result, employee_ids = import_service.confirm_file(
            db, read_upload(file), changed_by
        )
        if not result.ok:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=report(result).model_dump(mode="json"),
            )
        db.commit()
    return ImportConfirmed(created=len(employee_ids), employee_ids=employee_ids)
