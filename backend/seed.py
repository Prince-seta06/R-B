"""Synthetic demo data. Names, coordinates near district HQs, and ratings are invented for the demo."""
import os, math, random, datetime as dt
from sqlalchemy import select
from db import SessionLocal, Base, engine
from models import Jurisdiction, User, Building, Road, Inspection, RoadInspection, Grievance
from scoring import compute_cci, band_for, WEIGHTS, today_ist
from security import hash_password, IS_PROD
from geo import path_length_km

CIRCLES = {
    "Ahmedabad Circle": ["Ahmedabad", "Gandhinagar"],
    "Surat Circle": ["Surat", "Navsari"],
    "Vadodara Circle": ["Vadodara", "Kheda"],
    "Rajkot Circle": ["Rajkot", "Jamnagar"],
}
COORDS = {
    "Ahmedabad": (23.0225, 72.5714), "Gandhinagar": (23.2156, 72.6369),
    "Surat": (21.1702, 72.8311), "Navsari": (20.9467, 72.9520),
    "Vadodara": (22.3072, 73.1812), "Kheda": (22.6916, 72.8634),
    "Rajkot": (22.3039, 70.8022), "Jamnagar": (22.4707, 70.0577),
}
BUILDING_TYPES = {
    "Government Office": ("OFF", ["District Panchayat Office", "Mamlatdar Office", "Sub-Registrar Office", "Taluka Seva Sadan", "Treasury Office"]),
    "Staff Quarters": ("QTR", ["Class III Quarters Block A", "Class III Quarters Block B", "Class IV Quarters", "Officers' Quarters"]),
    "Rest House": ("RST", ["R&B Rest House", "Circuit House", "Inspection Bungalow"]),
    "Court Building": ("CRT", ["Civil Court Building", "Taluka Court Complex"]),
    "Health Centre": ("HLT", ["Community Health Centre", "Primary Health Centre Building"]),
}
BUILDING_CRIT = {"Government Office": 2, "Staff Quarters": 1, "Rest House": 1, "Court Building": 3, "Health Centre": 3}
ROAD_CLASSES = {  # class: (code, criticality, typical length km range, typical AADT range, lanes choices)
    "State Highway": ("SH", 3, (12, 30), (6000, 20000), [2, 4]),
    "Major District Road": ("MDR", 2, (6, 18), (2000, 8000), [2]),
    "Other District Road": ("ODR", 1, (3, 10), (500, 3000), [1, 2]),
    "Village Road": ("VR", 1, (1, 5), (100, 800), [1]),
}
ROAD_NAMES = ["{a} - {b} Road", "{a} Bypass", "{a} - {b} Link Road", "{a} Ring Road (Section)"]
PLACES = ["Kamrej", "Bardoli", "Sanand", "Dholka", "Padra", "Karjan", "Kalol", "Mehmedabad", "Lodhika", "Kalavad",
          "Dhrol", "Jalalpore", "Vyara", "Olpad", "Mandvi", "Halol"]
LOCALITIES = ["Sector 4", "Station Road", "Civil Lines", "Old City", "Ring Road", "Highway Road", "Market Yard"]
BUILDING_NOTES = {
    "structure": "Cracks observed in load-bearing walls and columns; needs structural engineer review.",
    "roof": "Roof leakage and damaged waterproofing; seepage stains on top-floor ceiling.",
    "electrical": "Ageing wiring and overloaded distribution boards; several unsafe connections.",
    "plumbing": "Leaking pipes and blocked drainage; dampness at ground-floor walls.",
    "fire_safety": "Fire extinguishers expired and exit signage missing.",
    "finishes": "Peeling plaster and paint; broken floor tiles in common areas.",
}
ROAD_NOTES = {
    "pavement": "Extensive alligator cracking and ravelling along the wheel paths.",
    "potholes": "Multiple potholes, some deeper than 50 mm; temporary patching has failed.",
    "riding_quality": "Rutting and corrugation; uncomfortable and unsafe riding quality at speed.",
    "drainage": "Side drains silted up and a cross-drainage culvert partly blocked; water stands on the carriageway.",
    "shoulders": "Shoulder drop-off at the pavement edge and eroded embankment slope in places.",
    "safety_furniture": "Faded lane markings, missing kilometre stones and damaged guard rail at curves.",
}


def should_seed() -> bool:
    flag = os.getenv("SEED_DEMO")
    return flag == "1" if flag is not None else not IS_PROD


def make_path(rng, lat0, lng0, length_km):
    """Wiggly centre-line of roughly `length_km` starting near the district HQ."""
    lat, lng = lat0 + rng.uniform(-0.06, 0.06), lng0 + rng.uniform(-0.06, 0.06)
    heading = rng.uniform(0, 2 * math.pi)
    pts, n = [[round(lat, 5), round(lng, 5)]], rng.randint(4, 7)
    step = length_km / (n - 1)
    for _ in range(n - 1):
        heading += rng.uniform(-0.6, 0.6)
        lat += step * math.cos(heading) / 111.0
        lng += step * math.sin(heading) / (111.0 * math.cos(math.radians(lat)))
        pts.append([round(lat, 5), round(lng, 5)])
    return pts


def _add_inspections(db, rng, kind, asset, insp_cls, fk, inspector, today, year):
    weights = WEIGHTS[kind]
    notes = BUILDING_NOTES if kind == "building" else ROAD_NOTES
    age_factor = min(1.0, (year - (asset.year_built or 2000)) / 71)
    n_insp = 0 if rng.random() < 0.08 else rng.randint(1, 3)      # a few never-inspected assets
    last_gap = rng.choice([30, 90, 200, 300, 420, 600])            # some overdue
    dates = sorted(today - dt.timedelta(days=last_gap + 365 * k + rng.randint(0, 40)) for k in range(n_insp))
    for when in dates:
        ratings = {k: max(1, min(4, round(4 - 2.3 * age_factor + rng.gauss(0, 0.75)))) for k in weights}
        cci = compute_cci(kind, ratings)
        band = band_for(kind, cci, ratings)
        worst = min(ratings, key=ratings.get)
        db.add(insp_cls(**{fk: asset.id}, inspector_id=inspector.id, inspected_on=when, ratings=ratings,
                        notes=notes[worst] if ratings[worst] <= 2 else "Routine inspection; no major issues.",
                        cci=cci, band=band, photos=[]))
        asset.cci, asset.band, asset.last_inspected = cci, band, when


def seed_grievances(db):
    if db.scalar(select(Grievance.id).limit(1)):
        return
    buildings = db.scalars(select(Building).order_by(Building.id)).all()
    roads = db.scalars(select(Road).order_by(Road.id)).all()
    if not buildings or not roads:
        return

    ce = db.scalars(select(User).where(User.role == "CE")).first()

    def je_of(division_id):
        return db.scalars(select(User).where(User.role == "JE", User.jurisdiction_id == division_id)).first()

    # Sample building grievances
    b_samples = [
        ("Kavita Patel", "9825123456", "kavita.p@gmail.com", "Roof Leakage / Seepage", "Urgent",
         "Severe water leakage from the roof during rains; ceiling plaster is deteriorating in the public records section.",
         "2nd Floor, Room 204 (Record Section)", "Pending", ""),
        ("Rajesh Shah", "9879012345", "rajesh.shah@yahoo.com", "Electrical Hazard", "Emergency",
         "Exposed high-voltage wiring and occasional sparking near the main visitor staircase.",
         "Main Entrance & Staircase Landing", "Under Review", "Site inspection assigned to Junior Engineer for immediate isolation."),
        ("Manish Mehta", "9904567890", None, "Structural Damage", "Urgent",
         "Visible horizontal crack on exterior column extending into the ground floor beam.",
         "North Wing Column B-4", "Work Order Issued", "Structural audit completed. Work order issued to contractor for epoxy grouting."),
        ("Dharmesh Joshi", "9426789012", "d.joshi@gujarat.gov.in", "Plumbing / Water Supply", "Normal",
         "Public restroom faucets leaking continuously and main drain line partially clogged.",
         "Ground Floor Public Facilities", "Resolved", "Plumber replaced valve gaskets and cleared blockage."),
    ]
    for idx, (name, phone, email, cat, urg, desc, loc, st, notes) in enumerate(b_samples):
        b = buildings[idx % len(buildings)]
        ticket_id = f"GRV-GJ-{b.district[:3].upper()}-BLD-{idx+1:04d}"
        g = Grievance(
            ticket_id=ticket_id, kind="building", building_id=b.id, road_id=None,
            citizen_name=name, citizen_phone=phone, citizen_email=email,
            category=cat, urgency=urg, description=desc, specific_location=loc,
            status=st, resolution_notes=notes,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=(idx + 1) * 3)
        )
        if st == "Resolved":
            # a resolved ticket always records who closed it: here the division's JE
            g.resolved_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            j = je_of(b.division_id)
            g.resolved_by_id = j.id if j else None
        db.add(g)

    # Sample road grievances
    r_samples = [
        ("Bhavesh Prajapati", "9824156789", "bhavesh.p@gmail.com", "Potholes / Road Surface Damage", "Emergency",
         "Large cluster of deep potholes exceeding 70mm depth near river bridge approach; high accident risk for two-wheelers.",
         "Near KM 12.4, Bridge Approach", "Pending", ""),
        ("Amit Solanki", "9723045612", "amit.solanki@rediffmail.com", "Waterlogging / Drainage Clog", "Urgent",
         "Cross-drainage culvert completely blocked with silt, causing 1-foot standing water across the entire carriageway after rain.",
         "Opposite Market Yard Entrance (KM 4.2)", "Under Review", "JE inspected; JCB deployed to excavate silt and unblock culvert barrel."),
        ("Sanjay Makwana", "9909123456", "sanjay.m@yahoo.com", "Pavement Cracking / Rutting", "Normal",
         "Severe rutting and uneven surface causing heavy vehicles to sway dangerously.",
         "Between KM 18.0 and 19.5", "Work Order Issued", "WMS sanction issued for 2km asphalt milling and resurfacing."),
        ("Hitesh Parmar", "9428012345", None, "Shoulder Erosion / Hazard", "Urgent",
         "Shoulder edge dropped by over 15cm following soil washout; risk of vehicle rollover on blind curve.",
         "Sharp Curve near Village Junction", "Resolved", "Shoulder backfilled with granular sub-base and compacted on 18th."),
    ]
    for idx, (name, phone, email, cat, urg, desc, loc, st, notes) in enumerate(r_samples):
        r = roads[idx % len(roads)]
        ticket_id = f"GRV-GJ-{r.district[:3].upper()}-ROD-{idx+1:04d}"
        g = Grievance(
            ticket_id=ticket_id, kind="road", building_id=None, road_id=r.id,
            citizen_name=name, citizen_phone=phone, citizen_email=email,
            category=cat, urgency=urg, description=desc, specific_location=loc,
            status=st, resolution_notes=notes,
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=(idx + 1) * 2)
        )
        if st == "Resolved":
            # demo: this one was closed by the Chief Engineer, so the CE-resolved flow is visible out of the box
            g.resolved_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            g.resolved_by_id = ce.id if ce else None
        db.add(g)

    db.commit()


def run(force=False):
    Base.metadata.create_all(engine)
    if not force and not should_seed():
        return
    db = SessionLocal()
    try:
        if db.scalar(select(User.id).limit(1)) and not force:
            seed_grievances(db)
            return
        rng = random.Random(42)
        today = today_ist()
        year = today.year

        div_by_district, circle_by_name = {}, {}
        for circle, districts in CIRCLES.items():
            c = Jurisdiction(name=circle, level="circle")
            db.add(c); db.flush()
            circle_by_name[circle] = c
            for d in districts:
                dv = Jurisdiction(name=f"{d} R&B Division", level="division", parent_id=c.id, district=d)
                db.add(dv); db.flush()
                div_by_district[d] = dv

        pw = hash_password("demo123")  # one hash for all demo accounts keeps first start quick
        users = [User(username="ce_state", password_hash=pw, full_name="Chief Engineer (State)", role="CE")]
        je_by_div = {}
        for d, dv in div_by_district.items():
            u = User(username=f"je_{d.lower()}", password_hash=pw, full_name=f"Junior Engineer, {d}",
                     role="JE", jurisdiction_id=dv.id)
            users.append(u); je_by_div[dv.id] = u
        for circle, c in circle_by_name.items():
            users.append(User(username=f"se_{circle.split()[0].lower()}", password_hash=pw,
                              full_name=f"Superintending Engineer, {circle}", role="SE", jurisdiction_id=c.id))
        db.add_all(users); db.flush()

        bseq = rseq = 0
        for district, dv in div_by_district.items():
            lat0, lng0 = COORDS[district]
            je = je_by_div[dv.id]
            for _ in range(6):
                bseq += 1
                btype = rng.choice(list(BUILDING_TYPES))
                code, names = BUILDING_TYPES[btype]
                b = Building(
                    asset_id=f"GJ-RB-{district[:3].upper()}-{code}-{bseq:04d}",
                    name=f"{rng.choice(names)}, {rng.choice(LOCALITIES)}", type=btype, district=district,
                    lat=round(lat0 + rng.uniform(-0.06, 0.06), 5), lng=round(lng0 + rng.uniform(-0.06, 0.06), 5),
                    year_built=rng.randint(1955, 2020), floors=rng.randint(1, 5), area_sqm=rng.randint(200, 4000),
                    criticality=BUILDING_CRIT[btype], division_id=dv.id,
                    wms_work_id=f"WMS-{rng.randint(2019, year)}-{rng.randint(1000, 9999)}" if rng.random() < 0.6 else None,
                    gujrams_road_id=f"SH{rng.randint(1, 160)}" if rng.random() < 0.7 else None)
                db.add(b); db.flush()
                _add_inspections(db, rng, "building", b, Inspection, "building_id", je, today, year)

            for _ in range(5):
                rseq += 1
                cls = rng.choice(list(ROAD_CLASSES))
                code, crit, (lmin, lmax), (tmin, tmax), lanes = ROAD_CLASSES[cls]
                path = make_path(rng, lat0, lng0, rng.uniform(lmin, lmax))
                a, b2 = rng.sample(PLACES, 2)
                surface = "Bituminous" if cls != "Village Road" else rng.choice(["Bituminous", "Concrete", "WBM / Gravel"])
                r = Road(
                    asset_id=f"GJ-RB-{district[:3].upper()}-{code}-{rseq:04d}",
                    name=rng.choice(ROAD_NAMES).format(a=a, b=b2), road_class=cls,
                    road_ref=f"{code}-{rng.randint(1, 160)}" if cls in ("State Highway", "Major District Road") else None,
                    surface=surface, lanes=rng.choice(lanes), district=district, path=path,
                    length_km=path_length_km(path), year_built=rng.randint(1975, 2022),
                    traffic_aadt=rng.randint(tmin, tmax), criticality=crit, division_id=dv.id,
                    wms_work_id=f"WMS-{rng.randint(2019, year)}-{rng.randint(1000, 9999)}" if rng.random() < 0.5 else None,
                    gujrams_id=f"GJRAMS-{rng.randint(10000, 99999)}" if rng.random() < 0.7 else None)
                db.add(r); db.flush()
                _add_inspections(db, rng, "road", r, RoadInspection, "road_id", je, today, year)
        
        seed_grievances(db)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    run(force=False)
    print("Seeded.")
