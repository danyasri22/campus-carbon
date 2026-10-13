"""
models.py - SQLAlchemy schemas, emission-factor catalogue and the GHG engine.

DATABASE_URL defaults to SQLite. For PostgreSQL set e.g.
    DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/carbon
(and `pip install psycopg2-binary`).

Emission factors are indicative values for an Indian campus (grid factor from
the CEA CO2 Baseline Database, ~0.82 kg/kWh; fuels from IPCC/DEFRA-style
factors). Verify them against the latest official releases before publishing
any figure externally.
"""
import json
import os
import random
from datetime import date, datetime, timedelta, time as dtime

from sqlalchemy import (Column, Date, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./campus_carbon.db")
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

SOLAR_METRIC = "solar_generation"

# key, scope, display name, unit, kg CO2e per unit, description
CATALOG = [
    ("diesel_generator", 1, "Diesel generator", "L", 2.68, "Diesel burned in backup generators"),
    ("vehicle_fuel", 1, "Campus vehicle fuel", "L", 2.31, "Petrol/diesel in campus-owned vehicles"),
    ("grid_electricity", 2, "Grid electricity", "kWh", 0.82, "Purchased grid power (CEA factor)"),
    ("commute_two_wheeler", 3, "Commute: two-wheeler", "km", 0.072, "Passenger-km by motorbike or scooter"),
    ("commute_car", 3, "Commute: car", "km", 0.171, "Passenger-km by private car"),
    ("commute_auto", 3, "Commute: auto-rickshaw", "km", 0.100, "Passenger-km by auto-rickshaw"),
    ("commute_bus", 3, "Commute: bus", "km", 0.089, "Passenger-km by bus"),
    ("commute_metro", 3, "Commute: metro/train", "km", 0.041, "Passenger-km by metro or suburban rail"),
    ("food_waste", 3, "Cafeteria food waste", "kg", 2.50, "Food waste sent to disposal"),
]


class CampusBuilding(Base):
    __tablename__ = "campus_buildings"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    building_type = Column(String(40), nullable=False)
    floor_area_sqm = Column(Float, default=0.0)
    solar_capacity_kw = Column(Float, default=0.0)
    readings = relationship("SensorData", back_populates="building")


class EmissionCategory(Base):
    __tablename__ = "emission_categories"
    id = Column(Integer, primary_key=True)
    key = Column(String(40), unique=True, nullable=False, index=True)
    name = Column(String(80), nullable=False)
    scope = Column(Integer, nullable=False)  # 1, 2 or 3
    unit = Column(String(10), nullable=False)
    emission_factor = Column(Float, nullable=False)  # kg CO2e per unit
    description = Column(Text, default="")
    readings = relationship("SensorData", back_populates="category")


class SensorData(Base):
    __tablename__ = "sensor_data"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, index=True, default=datetime.now)
    building_id = Column(Integer, ForeignKey("campus_buildings.id"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("emission_categories.id"), nullable=False)
    scope = Column(Integer, nullable=False, index=True)  # denormalised for fast aggregation
    value = Column(Float, nullable=False)
    co2e_kg = Column(Float, nullable=False)
    source = Column(String(10), nullable=False, default="iot")  # "iot" or "manual"
    meta = Column(Text, nullable=True)  # JSON string (tank level, device id, notes ...)
    building = relationship("CampusBuilding", back_populates="readings")
    category = relationship("EmissionCategory", back_populates="readings")


class CarbonOffsets(Base):
    """Solar rows carry solar_kwh; campus tree-count rows carry tree_count (building_id NULL)."""
    __tablename__ = "carbon_offsets"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True, default=date.today)
    building_id = Column(Integer, ForeignKey("campus_buildings.id"), nullable=True)
    solar_kwh = Column(Float, default=0.0)
    tree_count = Column(Integer, default=0)
    solar_offset_kg = Column(Float, default=0.0)


class GHGEngine:
    """All carbon arithmetic lives here."""
    GRID_FACTOR = 0.82            # kg CO2e per kWh displaced by rooftop solar
    TREE_KG_PER_YEAR = 21.77      # kg CO2 absorbed per mature tree per year (EPA-style estimate)

    @staticmethod
    def emission_kg(category: EmissionCategory, value: float) -> float:
        return round(value * category.emission_factor, 4)

    @classmethod
    def solar_offset_kg(cls, kwh: float) -> float:
        return round(kwh * cls.GRID_FACTOR, 4)

    @classmethod
    def tree_offset_kg_per_day(cls, trees: int) -> float:
        return trees * cls.TREE_KG_PER_YEAR / 365.0

    @staticmethod
    def campus_tree_count(db) -> int:
        row = (db.query(CarbonOffsets).filter(CarbonOffsets.tree_count > 0)
               .order_by(CarbonOffsets.id.desc()).first())
        return row.tree_count if row else 0

    @classmethod
    def record_solar(cls, db, building_id, kwh: float) -> float:
        """Add solar generation to today's row for the building; returns kg CO2e offset added."""
        row = (db.query(CarbonOffsets)
               .filter(CarbonOffsets.date == date.today(),
                       CarbonOffsets.building_id == building_id,
                       CarbonOffsets.tree_count == 0).first())
        if row is None:
            row = CarbonOffsets(date=date.today(), building_id=building_id,
                                solar_kwh=0.0, tree_count=0, solar_offset_kg=0.0)
            db.add(row)
        added = cls.solar_offset_kg(kwh)
        row.solar_kwh = (row.solar_kwh or 0.0) + kwh
        row.solar_offset_kg = (row.solar_offset_kg or 0.0) + added
        return added

    @classmethod
    def record_trees(cls, db, tree_count: int) -> None:
        db.add(CarbonOffsets(date=date.today(), building_id=None, tree_count=tree_count,
                             solar_kwh=0.0, solar_offset_kg=0.0))

    @classmethod
    def summary(cls, db, days: int = 7) -> dict:
        now = datetime.now()
        today = now.date()
        first = today - timedelta(days=days - 1)
        labels = [first + timedelta(days=i) for i in range(days)]
        start_dt = datetime.combine(first, dtime.min)

        daily = {d: 0.0 for d in labels}
        totals = {1: 0.0, 2: 0.0, 3: 0.0}
        rows = (db.query(SensorData.timestamp, SensorData.scope, SensorData.co2e_kg)
                .filter(SensorData.timestamp >= start_dt).all())
        for ts, scope, kg in rows:
            d = ts.date()
            if d in daily:
                daily[d] += kg
                totals[scope] += kg

        solar_kg = {d: 0.0 for d in labels}
        solar_kwh_total = 0.0
        for d, kwh, kg in (db.query(CarbonOffsets.date, CarbonOffsets.solar_kwh,
                                    CarbonOffsets.solar_offset_kg)
                           .filter(CarbonOffsets.date >= first).all()):
            if d in solar_kg:
                solar_kg[d] += kg or 0.0
                solar_kwh_total += kwh or 0.0

        trees = cls.campus_tree_count(db)
        per_day = cls.tree_offset_kg_per_day(trees)
        day_fraction = (now - datetime.combine(today, dtime.min)).total_seconds() / 86400.0
        tree_kg = {d: per_day * (day_fraction if d == today else 1.0) for d in labels}

        total_kg = sum(totals.values())
        solar_total = sum(solar_kg.values())
        tree_total = sum(tree_kg.values())
        offset_total = solar_total + tree_total
        scope_names = {1: "Scope 1 direct fuel", 2: "Scope 2 grid electricity", 3: "Scope 3 commute and waste"}

        recent_q = (db.query(SensorData, EmissionCategory, CampusBuilding)
                    .join(EmissionCategory, SensorData.category_id == EmissionCategory.id)
                    .join(CampusBuilding, SensorData.building_id == CampusBuilding.id)
                    .order_by(SensorData.timestamp.desc()).limit(8).all())

        return {
            "window_days": days,
            "total_tco2e": total_kg / 1000.0,
            "offset_tco2e": offset_total / 1000.0,
            "solar_offset_tco2e": solar_total / 1000.0,
            "tree_offset_tco2e": tree_total / 1000.0,
            "net_tco2e": (total_kg - offset_total) / 1000.0,
            "offset_coverage_pct": (offset_total / total_kg * 100.0) if total_kg else 0.0,
            "solar_kwh": solar_kwh_total,
            "tree_count": trees,
            "scopes": [{"scope": s, "label": scope_names[s], "tco2e": totals[s] / 1000.0,
                        "pct": (totals[s] / total_kg * 100.0) if total_kg else 0.0} for s in (1, 2, 3)],
            "trend": {
                "labels": [d.isoformat() for d in labels],
                "emissions": [round(daily[d] / 1000.0, 4) for d in labels],
                "offsets": [round((solar_kg[d] + tree_kg[d]) / 1000.0, 4) for d in labels],
            },
            "recent": [{"id": r.id, "time": r.timestamp.isoformat(timespec="seconds"),
                        "building": b.name, "metric": c.name, "value": r.value, "unit": c.unit,
                        "scope": r.scope, "co2e_kg": r.co2e_kg, "source": r.source}
                       for r, c, b in recent_q],
        }


def seed_if_empty(db) -> None:
    """Create reference data plus 7 days of plausible history so the dashboard is never blank."""
    if db.query(EmissionCategory).count() == 0:
        for key, scope, name, unit, factor, desc in CATALOG:
            db.add(EmissionCategory(key=key, scope=scope, name=name, unit=unit,
                                    emission_factor=factor, description=desc))
    if db.query(CampusBuilding).count() == 0:
        db.add_all([
            CampusBuilding(name="Academic Block A", building_type="academic", floor_area_sqm=9800, solar_capacity_kw=120),
            CampusBuilding(name="Hostel Complex", building_type="residential", floor_area_sqm=12500, solar_capacity_kw=80),
            CampusBuilding(name="Central Cafeteria", building_type="cafeteria", floor_area_sqm=1800, solar_capacity_kw=20),
            CampusBuilding(name="Engineering Workshops", building_type="workshop", floor_area_sqm=4200, solar_capacity_kw=60),
            CampusBuilding(name="Transport Hub", building_type="transport", floor_area_sqm=600, solar_capacity_kw=10),
        ])
    db.commit()
    if db.query(SensorData).count() > 0:
        return

    rnd = random.Random(13)
    cats = {c.key: c for c in db.query(EmissionCategory).all()}
    blds = {b.name: b for b in db.query(CampusBuilding).all()}
    now = datetime.now()
    midnight_today = datetime.combine(now.date(), dtime.min)
    elapsed = max((now - midnight_today).total_seconds() / 86400.0, 0.05)

    def add(ts, bname, key, value):
        c = cats[key]
        db.add(SensorData(timestamp=ts, building_id=blds[bname].id, category_id=c.id, scope=c.scope,
                          value=round(value, 2), co2e_kg=GHGEngine.emission_kg(c, value),
                          source="iot" if key in ("grid_electricity", "diesel_generator", "food_waste") else "manual"))

    for ago in range(6, -1, -1):
        day = now.date() - timedelta(days=ago)
        frac = elapsed if ago == 0 else 1.0
        weekend = 0.72 if day.weekday() >= 5 else 1.0

        def ts():
            base = datetime.combine(day, dtime.min)
            if ago == 0:
                return base + timedelta(seconds=rnd.uniform(0, elapsed * 86400))
            return base + timedelta(hours=rnd.uniform(8, 18))

        def j(x, spread=0.1):
            return x * rnd.uniform(1 - spread, 1 + spread) * frac

        for name, kwh in (("Academic Block A", 2200), ("Hostel Complex", 1800),
                          ("Central Cafeteria", 900), ("Engineering Workshops", 1500)):
            add(ts(), name, "grid_electricity", j(kwh * weekend))
        if rnd.random() < 0.7:
            add(ts(), "Engineering Workshops", "diesel_generator", j(rnd.uniform(20, 45)))
        add(ts(), "Transport Hub", "vehicle_fuel", j(55 * weekend))
        for key, km in (("commute_two_wheeler", 4200), ("commute_car", 1500), ("commute_auto", 900),
                        ("commute_bus", 3100), ("commute_metro", 1800)):
            add(ts(), "Transport Hub", key, j(km * weekend))
        add(ts(), "Central Cafeteria", "food_waste", j(85 * weekend, 0.18))

        for b in blds.values():
            if b.solar_capacity_kw:
                kwh = b.solar_capacity_kw * 4.6 * rnd.uniform(0.8, 1.05) * frac
                db.add(CarbonOffsets(date=day, building_id=b.id, solar_kwh=round(kwh, 2), tree_count=0,
                                     solar_offset_kg=GHGEngine.solar_offset_kg(kwh)))
    db.add(CarbonOffsets(date=now.date(), building_id=None, tree_count=1450, solar_kwh=0.0, solar_offset_kg=0.0))
    db.commit()
