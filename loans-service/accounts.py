from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import session
from storage import transaction

router = APIRouter(prefix="/api/accounts")


class Manual(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    amount: str = Field(pattern=r"^-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?$")
    currency: Literal["USD", "EUR", "GBP", "CAD"]
    as_of: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("as_of")
    @classmethod
    def recorded_date(cls, v):
        if date.fromisoformat(v) > datetime.now(timezone.utc).date():
            raise ValueError("Use an observed date, not a forecast.")
        return v

    def values(self):
        return self.name, int(Decimal(self.amount) * 100), self.currency, self.as_of


class Create(Manual):
    create_key: UUID


@contextmanager
def authorized(request, write=False):
    cfg = request.app.state.cfg
    with transaction(cfg, write) as db:
        member = session(db, cfg, request)
        yield db, member


def other(request, member):
    return next(iter(request.app.state.cfg.members - {member["email"]}))


VISIBLE = """SELECT id, name, cents, currency, as_of, recipient, owner FROM accounts
    WHERE (owner=:sub OR recipient=:email)"""


def public(row, member):
    return {"id": row["id"], "name": row["name"], "amount": f"{Decimal(row['cents']) / 100:.2f}",
            "currency": row["currency"], "as_of": row["as_of"], "mine": row["owner"] == member["sub"],
            "shared": row["recipient"] is not None}


@router.get("")
def accounts(request: Request):
    with authorized(request) as (db, member):
        rows = db.execute(VISIBLE + " ORDER BY as_of DESC, id", dict(member))
        return {"member": member["email"], "other": other(request, member),
                "accounts": [public(row, member) for row in rows]}


@router.get("/{account_id}")
def detail(account_id: UUID, request: Request):
    with authorized(request) as (db, member):
        row = db.execute(VISIBLE + " AND id=:id", dict(member) | {"id": str(account_id)}).fetchone()
        if not row:
            raise HTTPException(404, "Account not found.")
        return public(row, member)


@router.post("", status_code=201)
def create(body: Create, request: Request):
    with authorized(request, True) as (db, member):
        account_id = str(uuid4())
        result = db.execute("""INSERT INTO accounts(id,owner,name,cents,currency,as_of,create_key)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,create_key) DO NOTHING""",
            (account_id, member["sub"], *body.values(), str(body.create_key)))
        if not result.rowcount:
            raise HTTPException(409, "Submission already recorded. Refresh before submitting again.")
        return {"id": account_id}


def require_changed(cursor):
    if not cursor.rowcount:
        raise HTTPException(404, "Account not found.")


@router.put("/{account_id}", status_code=204)
def update(account_id: UUID, body: Manual, request: Request):
    with authorized(request, True) as (db, member):
        require_changed(db.execute("""UPDATE accounts SET name=?,cents=?,currency=?,as_of=?
            WHERE id=? AND owner=?""", (*body.values(), str(account_id), member["sub"])))


@router.delete("/{account_id}", status_code=204)
def delete(account_id: UUID, request: Request):
    with authorized(request, True) as (db, member):
        require_changed(db.execute("DELETE FROM accounts WHERE id=? AND owner=?", (str(account_id), member["sub"])))


@router.put("/{account_id}/share", status_code=204)
@router.delete("/{account_id}/share", status_code=204)
def sharing(account_id: UUID, request: Request):
    with authorized(request, True) as (db, member):
        recipient = other(request, member) if request.method == "PUT" else None
        require_changed(db.execute("UPDATE accounts SET recipient=? WHERE id=? AND owner=?",
                                   (recipient, str(account_id), member["sub"])))
