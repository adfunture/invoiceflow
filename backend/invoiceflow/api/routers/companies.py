from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_admin, require_viewer
from invoiceflow.api.schemas import CompanyOut, CompanyUpdateIn
from invoiceflow.db.models import Company
from invoiceflow.services.audit import audit

router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
def list_companies(auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> list[Company]:
    stmt = select(Company).order_by(Company.id)
    if auth.company_ids is not None:
        stmt = stmt.where(Company.id.in_(auth.company_ids))
    return list(db.scalars(stmt))


def _load(db: Session, auth: AuthContext, company_id: int) -> Company:
    ensure_company(auth, company_id)
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Nicht gefunden")
    return company


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> Company:
    return _load(db, auth, company_id)


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: int, body: CompanyUpdateIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)
) -> Company:
    company = _load(db, auth, company_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(company, key, value)
    audit(db, auth.actor, "company.updated", company_id=company.id, entity_type="company", entity_id=company.id, details=changes)
    db.commit()
    return company
