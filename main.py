"""
main.py - FastAPI application for the Campus Carbon Footprint & Scope 1-3 dashboard.

Run:  uvicorn main:app --reload
Open: http://127.0.0.1:8000        (dashboard)
      http://127.0.0.1:8000/docs   (interactive API docs)
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from models import (SOLAR_METRIC, Base, CampusBuilding, EmissionCategory, GHGEngine,
                    SensorData, SessionLocal, engine, seed_if_empty)

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_if_empty(db)
    yield


app = FastAPI(title="Campus Carbon Footprint API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ReadingIn(BaseModel):
    building_id: int = Field(..., ge=1)
    metric: str = Field(..., description="Category key, e.g. grid_electricity, diesel_generator, "
                                          "food_waste, commute_bus, or solar_generation (kWh offset)")
    value: float = Field(..., gt=0, le=1_000_000)
    timestamp: Optional[datetime] = None
    meta: Optional[dict] = None


class OffsetIn(BaseModel):
    tree_count: Optional[int] = Field(None, ge=0, le=10_000_000)
    solar_kwh: Optional[float] = Field(None, gt=0, le=1_000_000)
    building_id: Optional[int] = Field(None, ge=1)


def ingest(db, p: ReadingIn, source: Literal["iot", "manual"]) -> dict:
    building = db.get(CampusBuilding, p.building_id)
    if building is None:
        raise HTTPException(404, f"Building {p.building_id} not found")

    if p.metric == SOLAR_METRIC:
        offset_kg = GHGEngine.record_solar(db, p.building_id, p.value)
        db.commit()
        return {"type": "offset", "metric": SOLAR_METRIC, "value": p.value, "unit": "kWh",
                "offset_kg": offset_kg, "offset_tco2e": offset_kg / 1000.0}

    cat = db.query(EmissionCategory).filter_by(key=p.metric).first()
    if cat is None:
        valid = [c.key for c in db.query(EmissionCategory).all()] + [SOLAR_METRIC]
        raise HTTPException(422, f"Unknown metric '{p.metric}'. Valid metrics: {', '.join(valid)}")

    ts = p.timestamp.replace(tzinfo=None) if p.timestamp else datetime.now()
    co2e = GHGEngine.emission_kg(cat, p.value)
    reading = SensorData(timestamp=ts, building_id=building.id, category_id=cat.id, scope=cat.scope,
                         value=p.value, co2e_kg=co2e, source=source,
                         meta=json.dumps(p.meta) if p.meta else None)
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return {"type": "emission", "id": reading.id, "metric": cat.key, "name": cat.name,
            "scope": cat.scope, "value": p.value, "unit": cat.unit,
            "co2e_kg": co2e, "tco2e": co2e / 1000.0, "source": source}


@app.post("/api/telemetry", status_code=201, summary="Receive an IoT telemetry reading")
def post_telemetry(payload: ReadingIn, db=Depends(get_db)):
    return ingest(db, payload, "iot")


@app.post("/api/entries", status_code=201, summary="Submit a manual operational entry")
def post_entry(payload: ReadingIn, db=Depends(get_db)):
    return ingest(db, payload, "manual")


@app.post("/api/offsets", status_code=201, summary="Update campus tree count and/or add solar generation")
def post_offsets(payload: OffsetIn, db=Depends(get_db)):
    if payload.tree_count is None and payload.solar_kwh is None:
        raise HTTPException(422, "Provide tree_count and/or solar_kwh")
    added_kg = 0.0
    if payload.solar_kwh is not None:
        if payload.building_id is not None and db.get(CampusBuilding, payload.building_id) is None:
            raise HTTPException(404, f"Building {payload.building_id} not found")
        added_kg = GHGEngine.record_solar(db, payload.building_id, payload.solar_kwh)
    if payload.tree_count is not None:
        GHGEngine.record_trees(db, payload.tree_count)
    db.commit()
    return {"solar_offset_kg_added": added_kg, "tree_count": GHGEngine.campus_tree_count(db),
            "tree_offset_kg_per_year": payload.tree_count * GHGEngine.TREE_KG_PER_YEAR
            if payload.tree_count is not None else None}


@app.get("/api/summary", summary="Dashboard KPIs, scope split and trend")
def get_summary(days: int = Query(7, ge=1, le=30), db=Depends(get_db)):
    return GHGEngine.summary(db, days)


@app.get("/api/buildings")
def get_buildings(db=Depends(get_db)):
    return [{"id": b.id, "name": b.name, "type": b.building_type, "floor_area_sqm": b.floor_area_sqm,
             "solar_capacity_kw": b.solar_capacity_kw} for b in db.query(CampusBuilding).order_by(CampusBuilding.id)]


@app.get("/api/categories")
def get_categories(db=Depends(get_db)):
    return [{"key": c.key, "name": c.name, "scope": c.scope, "unit": c.unit,
             "emission_factor": c.emission_factor, "description": c.description}
            for c in db.query(EmissionCategory).order_by(EmissionCategory.scope, EmissionCategory.id)]


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(BASE_DIR / "index.html")
