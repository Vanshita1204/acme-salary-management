from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.models import Company
from app.schemas.catalog import CompanyIn, CompanyOut

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
def list_companies(db: DbSession) -> list[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)))


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, db: DbSession) -> Company:
    return get_or_404(db, Company, company_id, "company")


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(body: CompanyIn, db: DbSession) -> Company:
    company = Company(**body.model_dump())
    with translate_db_errors(db):
        db.add(company)
        db.commit()
    return company
