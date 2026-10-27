"""MachineCare AI — Phase 1: verified historical downtime record API.

This phase does NOT use RAG or an LLM. Those come after validating data ingestion.
"""
from contextlib import asynccontextmanager
from datetime import date
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Date, ForeignKey, Integer, String, Text, create_engine, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{ROOT / "machinecare.db"}')
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite:') else {})

class Base(DeclarativeBase):
    pass

class Incident(Base):
    __tablename__ = 'incidents'
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    occurred_on: Mapped[date] = mapped_column(Date)
    production_line: Mapped[str] = mapped_column(String(100))
    machine: Mapped[str] = mapped_column(String(100), index=True)
    product: Mapped[str] = mapped_column(String(100))
    downtime_minutes: Mapped[int] = mapped_column(Integer)
    issue_category: Mapped[str] = mapped_column(String(100))
    cause: Mapped[str] = mapped_column(Text)
    action_taken: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    responsible_team: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40))
    preventive_action: Mapped[str] = mapped_column(Text)

class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    occurred_on: date
    production_line: str
    machine: str
    product: str
    downtime_minutes: int
    issue_category: str
    cause: str
    action_taken: str
    root_cause: str
    responsible_team: str
    status: str
    preventive_action: str

def seed_database() -> None:
    """Idempotently seed original historical records. No overwrite on restart."""
    records = json.loads((ROOT / 'data' / 'incidents.json').read_text(encoding='utf-8'))
    with Session(engine) as session:
        existing_ids = set(session.scalars(select(Incident.id)).all())
        for row in records:
            if row['Incident ID'] in existing_ids:
                continue
            session.add(Incident(
                id=row['Incident ID'], occurred_on=date.fromisoformat(row['Date']),
                production_line=row['Production Line'], machine=row['Machine'],
                product=row['Product'], downtime_minutes=int(row['Downtime (min)']),
                issue_category=row['Issue Category'], cause=row['Cause'],
                action_taken=row['Action Taken'], root_cause=row['Root Cause'],
                responsible_team=row['Responsible Team'], status=row['Status'],
                preventive_action=row['Preventive Action'],
            ))
        session.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    seed_database()
    yield

app = FastAPI(title='MachineCare AI API', version='0.1.0', lifespan=lifespan)

@app.get('/health')
def health():
    return {'status': 'ok', 'phase': 'database-foundation'}

@app.get('/incidents', response_model=list[IncidentOut])
def list_incidents(machine: str | None = None, status: str | None = None, limit: int = Query(20, ge=1, le=100)):
    with Session(engine) as session:
        stmt = select(Incident).order_by(Incident.occurred_on, Incident.id).limit(limit)
        if machine:
            stmt = stmt.where(Incident.machine.ilike(f'%{machine}%'))
        if status:
            stmt = stmt.where(Incident.status == status)
        return session.scalars(stmt).all()

@app.get('/incidents/count')
def count_incidents():
    with Session(engine) as session:
        return {'count': session.scalar(select(func.count()).select_from(Incident))}

@app.get('/incidents/{incident_id}', response_model=IncidentOut)
def get_incident(incident_id: str):
    with Session(engine) as session:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail='Incident not found')
        return incident
