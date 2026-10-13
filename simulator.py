"""
simulator.py - IoT edge-node simulator.

Every INTERVAL seconds it posts HTTP telemetry (MQTT-style topic included in
`meta`) for: grid kWh meters, a diesel generator (with tank level), a cafeteria
waste scale and rooftop solar inverters.

Time is compressed: each 5 s tick represents TIME_SCALE * 5 s of campus time
(default 60x = 5 simulated minutes) so the dashboard visibly moves.

Usage:
    python simulator.py
    API_URL=http://192.168.1.20:8000 TIME_SCALE=120 python simulator.py
"""
import os
import random
import math
import sys
import time
from datetime import datetime

import requests

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
INTERVAL = float(os.getenv("INTERVAL", "5"))
TIME_SCALE = float(os.getenv("TIME_SCALE", "60"))
TANK_CAPACITY_L = 500.0
GEN_BURN_L_PER_H = 14.0

# average electrical load in kW per building (grid meters)
LOADS_KW = {"Academic Block A": 95, "Hostel Complex": 75, "Central Cafeteria": 38, "Engineering Workshops": 62}
SOLAR_KW = {"Academic Block A": 120, "Hostel Complex": 80, "Central Cafeteria": 20, "Engineering Workshops": 60}


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def load_buildings(session: requests.Session) -> dict:
    while True:
        try:
            r = session.get(f"{API_URL}/api/buildings", timeout=5)
            r.raise_for_status()
            return {b["name"]: b["id"] for b in r.json()}
        except requests.RequestException as exc:
            print(f"[wait] API not reachable at {API_URL} ({exc.__class__.__name__}); retrying in 3 s")
            time.sleep(3)


def post(session: requests.Session, building_id: int, metric: str, value: float, meta: dict) -> None:
    payload = {"building_id": building_id, "metric": metric, "value": round(value, 3),
               "timestamp": datetime.now().isoformat(timespec="seconds"), "meta": meta}
    try:
        r = session.post(f"{API_URL}/api/telemetry", json=payload, timeout=5)
        if r.ok:
            body = r.json()
            co2 = body.get("co2e_kg", body.get("offset_kg", 0.0))
            tag = "offset" if body.get("type") == "offset" else "emit"
            print(f"[{datetime.now():%H:%M:%S}] {tag:6} {metric:17} {value:9.3f} -> {co2:8.2f} kg CO2e  ({meta['topic']})")
        else:
            print(f"[error] {r.status_code}: {r.text[:160]}")
    except requests.RequestException as exc:
        print(f"[error] post failed: {exc.__class__.__name__}")


def main() -> None:
    session = requests.Session()
    buildings = load_buildings(session)
    sim_hour = datetime.now().hour + datetime.now().minute / 60.0
    tick_hours = INTERVAL * TIME_SCALE / 3600.0
    tank_l = TANK_CAPACITY_L * 0.8
    outage_ticks = 0
    print(f"Simulator online -> {API_URL} | tick = {INTERVAL}s real = {tick_hours * 60:.1f} simulated min | Ctrl+C to stop")

    try:
        while True:
            h = sim_hour % 24
            day_shape = 0.55 + 0.45 * math.sin(math.pi * (h - 6) / 14) if 6 <= h <= 20 else 0.45

            # 1) grid electricity meters (kWh consumed during the tick)
            for name, kw in LOADS_KW.items():
                if name in buildings:
                    kwh = kw * day_shape * tick_hours * random.uniform(0.9, 1.1)
                    post(session, buildings[name], "grid_electricity", kwh,
                         {"device": f"meter-{slug(name)}", "topic": f"campus/{slug(name)}/energy", "kw": round(kw * day_shape, 1)})

            # 2) diesel generator: random grid outages trigger it
            wb = buildings.get("Engineering Workshops")
            if outage_ticks == 0 and random.random() < 0.08:
                outage_ticks = random.randint(2, 6)
            if wb:
                if outage_ticks > 0:
                    litres = min(GEN_BURN_L_PER_H * tick_hours * random.uniform(0.9, 1.15), tank_l)
                    tank_l -= litres
                    outage_ticks -= 1
                    post(session, wb, "diesel_generator", litres,
                         {"device": "dg-01", "topic": "campus/workshops/generator", "running": True,
                          "tank_level_pct": round(tank_l / TANK_CAPACITY_L * 100, 1)})
                else:
                    print(f"[{datetime.now():%H:%M:%S}] idle   diesel_generator  standby, tank {tank_l / TANK_CAPACITY_L * 100:.0f}%")
                if tank_l < TANK_CAPACITY_L * 0.15:
                    tank_l = TANK_CAPACITY_L * 0.95
                    print("[info] generator tank refilled")

            # 3) cafeteria waste scale during meal service windows
            cafe = buildings.get("Central Cafeteria")
            if cafe and (7 <= h < 10 or 12 <= h < 15 or 19 <= h < 22):
                post(session, cafe, "food_waste", 24 * tick_hours * random.uniform(0.6, 1.5),
                     {"device": "scale-01", "topic": "campus/cafeteria/waste"})

            # 4) rooftop solar inverters (offsets)
            if 6 <= h <= 18:
                irradiance = math.sin(math.pi * (h - 6) / 12) * random.uniform(0.75, 1.0)
                for name, kw in SOLAR_KW.items():
                    if name in buildings and irradiance > 0.02:
                        post(session, buildings[name], "solar_generation", kw * irradiance * tick_hours,
                             {"device": f"inverter-{slug(name)}", "topic": f"campus/{slug(name)}/solar"})

            sim_hour += tick_hours
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
