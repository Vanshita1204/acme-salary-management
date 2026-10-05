from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import Country, Currency
from app.schemas.catalog import CountryOut, CurrencyOut

router = APIRouter(tags=["reference"])


@router.get("/currencies", response_model=list[CurrencyOut])
def list_currencies(db: DbSession) -> list[Currency]:
    return list(db.scalars(select(Currency).order_by(Currency.code)))


@router.get("/countries", response_model=list[CountryOut])
def list_countries(db: DbSession) -> list[Country]:
    return list(db.scalars(select(Country).order_by(Country.name)))
