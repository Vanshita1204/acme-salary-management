from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.models import CompensationType
from app.schemas.catalog import CompensationTypeIn, CompensationTypeOut

router = APIRouter(prefix="/compensation-types", tags=["compensation-types"])


@router.get("", response_model=list[CompensationTypeOut])
def list_compensation_types(db: DbSession) -> list[CompensationType]:
    return list(
        db.scalars(
            select(CompensationType).order_by(
                CompensationType.is_base_pay.desc(),
                CompensationType.category,
                CompensationType.name,
            )
        )
    )


@router.get("/{type_id}", response_model=CompensationTypeOut)
def get_compensation_type(type_id: int, db: DbSession) -> CompensationType:
    return get_or_404(db, CompensationType, type_id, "compensation type")


@router.post(
    "", response_model=CompensationTypeOut, status_code=status.HTTP_201_CREATED
)
def create_compensation_type(
    body: CompensationTypeIn, db: DbSession
) -> CompensationType:
    """Add a type to the universal catalog. Never a base-pay type (that one is seeded)."""
    comp_type = CompensationType(**body.model_dump(), is_base_pay=False)
    with translate_db_errors(db):
        db.add(comp_type)
        db.commit()
    return comp_type
