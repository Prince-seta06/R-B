import os, re, uuid, hmac, logging, datetime as dt
from contextlib import asynccontextmanager
from typing import Optional, Literal

import httpx, jwt
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

import seed
from db import get_db, Base, engine
from models import Jurisdiction, User, Building, Road, Inspection, RoadInspection, AuditLog, Grievance
from scoring import (WEIGHTS, COMPONENT_LABELS, SAFETY_CRITICAL, RATING_LABELS, validate_ratings, compute_cci,
                     band_for, override_reason, priority_score, is_overdue, today_ist, IST)
from security import (verify_password, burn_time, make_token, read_token, limiter, track_limiter, COOKIE_NAME,
                      COOKIE_SECURE, SESSION_HOURS, IS_PROD)
from geo import path_length_km, haversine_km

log = logging.getLogger("rnb")
BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
STATIC = os.path.join(BASE, "static")
BANDS = ["Good", "Fair", "Poor", "Critical", "Unrated"]
BUILDING_TYPES = list(seed.BUILDING_TYPES)
ROAD_CLASSES = list(seed.ROAD_CLASSES)
SURFACES = ["Bituminous", "Concrete", "WBM / Gravel", "Earthen"]
LANES = [1, 2, 4, 6]
WRITE_ROLES = {"JE"}              # only field engineers author data (register assets, record inspections, add photos)
VOID_ROLES = {"SE", "CE"}         # reviewers void wrong inspections; a JE cannot erase their own record
REGISTER_ROLES = {"SE", "CE"}     # deciding what goes on the asset register is a review-level job (SE: own circle, CE: state)
ROLE_TITLES = {"JE": "Junior Engineer", "SE": "Superintending Engineer", "CE": "Chief Engineer"}
CLOSED_STATUSES = ("Resolved", "Rejected")   # the citizen is shown an outcome (who, when, remarks) for these
MAX_PHOTO = 5 * 1024 * 1024
MAX_PHOTOS_PER_INSPECTION = 10

Kind = Literal["building", "road"]
CFG = {
    "building": dict(Asset=Building, Insp=Inspection, fk="building_id"),
    "road": dict(Asset=Road, Insp=RoadInspection, fk="road_id"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(UPLOADS, exist_ok=True)
    Base.metadata.create_all(engine)
    seed.run()  # no-op if data exists or seeding is disabled (see README)
    yield


app = FastAPI(title="R&B Asset Register", lifespan=lifespan,
              docs_url=None if IS_PROD else "/docs", redoc_url=None, openapi_url=None if IS_PROD else "/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data: https://*.tile.openstreetmap.org; "
                                   "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "strict-origin-when-cross-origin"})
    return resp


# ---------- auth & scoping ----------
def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not signed in")
    try:
        user = db.get(User, read_token(token))
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Session expired, sign in again")
    if not user:
        raise HTTPException(401, "Unknown user")
    return user


def writer(user: User = Depends(current_user)) -> User:
    if user.role not in WRITE_ROLES:
        raise HTTPException(403, "Only Junior Engineers can record inspections and add photos")
    return user


def registrar(user: User = Depends(current_user)) -> User:
    if user.role not in REGISTER_ROLES:
        raise HTTPException(403, "Only a Superintending Engineer or Chief Engineer can register buildings and roads")
    return user


def voider(user: User = Depends(current_user)) -> User:
    if user.role not in VOID_ROLES:
        raise HTTPException(403, "Only a Superintending Engineer or Chief Engineer can void an inspection")
    return user


def allowed_division_ids(db: Session, user: User):
    if user.role == "CE":
        return None  # state-wide
    if user.role == "SE":
        return [d.id for d in db.scalars(select(Jurisdiction).where(Jurisdiction.parent_id == user.jurisdiction_id))]
    return [user.jurisdiction_id]


def scoped_query(db: Session, user: User, kind: str):
    Asset = CFG[kind]["Asset"]
    q = select(Asset)
    ids = allowed_division_ids(db, user)
    if ids is not None:
        q = q.where(Asset.division_id.in_(ids))
    return q


def get_asset_or_404(db: Session, user: User, kind: str, asset_pk: int):
    a = db.scalars(scoped_query(db, user, kind).where(CFG[kind]["Asset"].id == asset_pk)).first()
    if not a:
        raise HTTPException(404, "Not found in your jurisdiction")
    return a


def get_inspection_or_404(db: Session, user: User, kind: str, insp_id: int):
    insp = db.get(CFG[kind]["Insp"], insp_id)
    if not insp:
        raise HTTPException(404, "Inspection not found")
    asset = get_asset_or_404(db, user, kind, getattr(insp, CFG[kind]["fk"]))
    return insp, asset


def jmap(db):
    return {j.id: j for j in db.scalars(select(Jurisdiction))}


def audit(db: Session, user: User, action: str, kind: str, asset, detail: str = ""):
    db.add(AuditLog(user_id=user.id, username=user.username, action=action, kind=kind,
                    asset_code=asset.asset_id, division_id=asset.division_id, detail=detail[:500]))


def latest_valid(a):
    return next((i for i in a.inspections if not i.voided), None)


def recompute(db: Session, kind: str, a):
    """Set the asset's derived state from its newest non-voided inspection (or clear it)."""
    Insp, fk = CFG[kind]["Insp"], CFG[kind]["fk"]
    db.flush()
    latest = db.scalars(select(Insp).where(getattr(Insp, fk) == a.id, Insp.voided.is_(False))
                        .order_by(Insp.inspected_on.desc(), Insp.id.desc())).first()
    if latest:
        a.cci, a.band, a.last_inspected = latest.cci, latest.band, latest.inspected_on
    else:
        a.cci, a.band, a.last_inspected = None, "Unrated", None



DISTRICT_CONFIG = {
    "Ahmedabad":   {"center": (23.0225, 72.5714), "max_km": 42.0},
    "Gandhinagar": {"center": (23.2156, 72.6369), "max_km": 25.0},
    "Surat":       {"center": (21.1702, 72.8311), "max_km": 35.0},
    "Navsari":     {"center": (20.9467, 72.9520), "max_km": 28.0},
    "Vadodara":    {"center": (22.3072, 73.1812), "max_km": 35.0},
    "Kheda":       {"center": (22.6916, 72.8634), "max_km": 35.0},
    "Rajkot":      {"center": (22.3039, 70.8022), "max_km": 48.0},
    "Jamnagar":    {"center": (22.4707, 70.0577), "max_km": 45.0},
}
GUJARAT_BOUNDS = {"min_lat": 20.1, "max_lat": 24.7, "min_lng": 68.1, "max_lng": 74.5}

GRIEVANCE_BUILDING_CATEGORIES = [
    "Structural Damage",
    "Roof Leakage / Seepage",
    "Electrical Hazard",
    "Plumbing / Water Supply",
    "Fire Safety Defect",
    "Wall Finishes / Plaster",
    "Other Building Defect",
]
GRIEVANCE_ROAD_CATEGORIES = [
    "Potholes / Road Surface Damage",
    "Pavement Cracking / Rutting",
    "Waterlogging / Drainage Clog",
    "Damaged Culvert / Bridge Approach",
    "Missing / Broken Signs & Road Markings",
    "Shoulder Erosion / Hazard",
    "Other Road Hazard",
]
GRIEVANCE_URGENCIES = ["Normal", "Urgent", "Emergency"]
GRIEVANCE_STATUSES = ["Pending", "Under Review", "Work Order Issued", "Resolved", "Rejected"]


def validate_district_coords(district: str, lat: float, lng: float) -> tuple[bool, str]:
    if not (GUJARAT_BOUNDS["min_lat"] <= lat <= GUJARAT_BOUNDS["max_lat"] and
            GUJARAT_BOUNDS["min_lng"] <= lng <= GUJARAT_BOUNDS["max_lng"]):
        return False, "Location is outside Gujarat State (Lat: 20.1°N - 24.7°N, Lng: 68.1°E - 74.5°E)."
    if district not in DISTRICT_CONFIG:
        return False, f"Unknown or unsupported district '{district}'."
    
    cfg = DISTRICT_CONFIG[district]
    nearest = min(DISTRICT_CONFIG.keys(), key=lambda d: haversine_km((lat, lng), DISTRICT_CONFIG[d]["center"]))
    d_km = haversine_km((lat, lng), cfg["center"])
    if nearest != district:
        return False, f"Coordinates ({lat:.4f}, {lng:.4f}) fall in {nearest} district, not in {district}."
    if d_km > cfg["max_km"]:
        return False, f"Coordinates are {d_km:.1f} km from {district} center (max allowed: {cfg['max_km']} km)."
    return True, ""


def grievance_dict(g: Grievance):
    asset_name = ""
    asset_code = ""
    district = ""
    sub_type = ""
    if g.kind == "building" and g.building:
        asset_name = g.building.name
        asset_code = g.building.asset_id
        district = g.building.district
        sub_type = g.building.type
    elif g.kind == "road" and g.road:
        asset_name = g.road.name
        asset_code = g.road.asset_id
        district = g.road.district
        sub_type = g.road.road_class + (f" ({g.road.road_ref})" if g.road.road_ref else "")
    return {
        "id": g.id,
        "ticket_id": g.ticket_id,
        "kind": g.kind,
        "building_id": g.building_id,
        "road_id": g.road_id,
        "asset_name": asset_name,
        "asset_code": asset_code,
        "district": district,
        "sub_type": sub_type,
        "citizen_name": g.citizen_name,
        "citizen_phone": g.citizen_phone,
        "citizen_email": g.citizen_email,
        "category": g.category,
        "urgency": g.urgency,
        "description": g.description,
        "specific_location": g.specific_location,
        "photo_url": g.photo_url,
        "status": g.status,
        "resolution_notes": g.resolution_notes,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "resolved_at": g.resolved_at.isoformat() if g.resolved_at else None,
        "resolved_at_display": _ist_display(g.resolved_at),
        "resolved_by": g.resolved_by.full_name if g.resolved_by else None,
        "resolved_by_role": ROLE_TITLES.get(g.resolved_by.role) if g.resolved_by else None,
    }


# Fields a citizen may see on the public tracking page. Never the phone number or e-mail.
_PUBLIC_GRIEVANCE_FIELDS = (
    "ticket_id", "kind", "asset_name", "district", "citizen_name", "category", "urgency", "description",
    "specific_location", "photo_url", "status", "resolution_notes", "created_at", "resolved_at",
    "resolved_at_display", "resolved_by", "resolved_by_role")


def public_grievance_dict(g: Grievance):
    full = grievance_dict(g)
    return {k: full[k] for k in _PUBLIC_GRIEVANCE_FIELDS}


def _ist_display(ts):
    """Timestamps are stored in UTC (SQLite hands them back naive); show them to citizens in IST."""
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")

# ---------- serialisers ----------
def asset_dict(kind: str, a, J):
    div = J[a.division_id]
    d = {"kind": kind, "id": a.id, "asset_id": a.asset_id, "name": a.name, "district": a.district,
         "criticality": a.criticality, "year_built": a.year_built, "division": div.name,
         "circle": J[div.parent_id].name, "wms_work_id": a.wms_work_id, "cci": a.cci, "band": a.band,
         "last_inspected": a.last_inspected.isoformat() if a.last_inspected else None,
         "overdue": is_overdue(kind, a.last_inspected), "priority": priority_score(a.cci, a.criticality, a.band),
         "grievances_count": len(a.grievances) if getattr(a, "grievances", None) else 0,
         "unresolved_grievances_count": sum(1 for g in a.grievances if g.status not in ("Resolved", "Rejected")) if getattr(a, "grievances", None) else 0,
         "grievances": [grievance_dict(g) for g in a.grievances] if getattr(a, "grievances", None) else []}
    if kind == "building":
        d.update(type=a.type, type_label=a.type, lat=a.lat, lng=a.lng, floors=a.floors, area_sqm=a.area_sqm,
                 gujrams_road_id=a.gujrams_road_id)
    else:
        d.update(road_class=a.road_class, type_label=a.road_class, road_ref=a.road_ref, surface=a.surface,
                 lanes=a.lanes, path=a.path, length_km=a.length_km, traffic_aadt=a.traffic_aadt,
                 gujrams_id=a.gujrams_id, lat=a.path[0][0], lng=a.path[0][1])
    return d


def inspection_dict(kind: str, i):
    return {"id": i.id, "inspected_on": i.inspected_on.isoformat(), "ratings": i.ratings, "notes": i.notes,
            "cci": i.cci, "band": i.band, "summary": i.summary, "voided": i.voided, "void_reason": i.void_reason,
            "photos": [f"/api/photos/{kind}/{i.id}/{n}" for n in (i.photos or [])],
            "reason": override_reason(kind, i.cci, i.ratings, i.band),
            "inspector": i.inspector.full_name if i.inspector else None}


# ---------- schemas ----------
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


class BuildingIn(BaseModel):
    name: str = Field(max_length=120)
    type: str
    district: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    year_built: Optional[int] = Field(None, ge=1800)
    floors: int = Field(1, ge=1, le=60)
    area_sqm: Optional[int] = Field(None, ge=1, le=1_000_000)
    criticality: int = Field(2, ge=1, le=3)


class RoadIn(BaseModel):
    name: str = Field(max_length=120)
    road_class: str
    road_ref: Optional[str] = Field(None, max_length=20)
    surface: str
    lanes: int = 2
    district: str
    path: list[tuple[float, float]] = Field(min_length=2, max_length=300)
    year_built: Optional[int] = Field(None, ge=1800)
    traffic_aadt: Optional[int] = Field(None, ge=0, le=1_000_000)
    criticality: Optional[int] = Field(None, ge=1, le=3)   # defaults from the road class


class InspectionIn(BaseModel):
    ratings: dict[str, int]
    notes: str = Field("", max_length=2000)
    inspected_on: Optional[dt.date] = None


class VoidIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ScoreIn(BaseModel):
    ratings: dict[str, int]



class GrievanceIn(BaseModel):
    kind: str = "building"  # building | road
    building_id: Optional[int] = None
    road_id: Optional[int] = None
    citizen_name: str = Field(max_length=120)
    citizen_phone: str = Field(min_length=8, max_length=20)
    citizen_email: Optional[str] = Field(None, max_length=120)
    category: str
    urgency: str = "Normal"
    description: str = Field(min_length=5, max_length=3000)
    specific_location: str = Field("", max_length=200)
    photo_url: Optional[str] = None


class GrievanceUpdateIn(BaseModel):
    status: str
    resolution_notes: Optional[str] = Field("", max_length=2000)


class TrackIn(BaseModel):
    ticket_id: str = Field(min_length=5, max_length=40)
    phone: str = Field(min_length=8, max_length=20)

# ---------- auth routes ----------
@app.get("/api/public")
def public_info():
    """Unauthenticated: lets the login page decide whether to show the demo-account hints."""
    return {"demo_accounts": seed.should_seed()}


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    uname = body.username.strip().lower()
    key = f"{request.client.host if request.client else '?'}|{uname}"
    wait = limiter.blocked(key)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = db.scalars(select(User).where(User.username == uname)).first()
    ok = verify_password(body.password, user.password_hash) if user else (burn_time(body.password) or False)
    if not ok:
        limiter.record_fail(key)
        raise HTTPException(401, "Wrong username or password")
    limiter.reset(key)
    response.set_cookie(COOKIE_NAME, make_token(user.id), httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, max_age=SESSION_HOURS * 3600, path="/")
    return {"user": {"name": user.full_name, "role": user.role}}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=COOKIE_SECURE)
    return {"ok": True}


@app.get("/api/meta")
def meta(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = allowed_division_ids(db, user)
    divs = [j for j in jmap(db).values() if j.level == "division" and (ids is None or j.id in ids)]

    def comps(kind):
        return [{"key": k, "label": COMPONENT_LABELS[kind][k], "weight": w, "safety": k in SAFETY_CRITICAL[kind]}
                for k, w in WEIGHTS[kind].items()]
    return {
        "user": {"name": user.full_name, "role": user.role}, "can_write": user.role in WRITE_ROLES, "can_void": user.role in VOID_ROLES,
        "can_register": user.role in REGISTER_ROLES,
        "kinds": {
            "building": {"label": "Building", "components": comps("building"), "types": BUILDING_TYPES},
            "road": {"label": "Road", "components": comps("road"), "surfaces": SURFACES, "lanes": LANES,
                     "classes": [{"name": c, "criticality": seed.ROAD_CLASSES[c][1]} for c in ROAD_CLASSES]},
        },
        "rating_labels": RATING_LABELS, "bands": BANDS,
        "districts": sorted(list(DISTRICT_CONFIG.keys())) if user.role == "CE" else sorted({d.district for d in divs if d.district}),
        "divisions": [{"id": d.id, "name": d.name} for d in divs],
        "gujarat_bounds": GUJARAT_BOUNDS,
        "district_config": {d: {"center": list(cfg["center"]), "max_km": cfg["max_km"]} for d, cfg in DISTRICT_CONFIG.items()},
        "grievance_building_categories": GRIEVANCE_BUILDING_CATEGORIES,
        "grievance_road_categories": GRIEVANCE_ROAD_CATEGORIES,
        "grievance_statuses": GRIEVANCE_STATUSES,
        "grievance_urgencies": GRIEVANCE_URGENCIES}


@app.post("/api/score/{kind}")
def score(kind: Kind, body: ScoreIn, user: User = Depends(current_user)):
    """The single source of truth for scoring: the inspection form's live preview calls this."""
    try:
        r = validate_ratings(kind, body.ratings)
    except ValueError as e:
        raise HTTPException(422, str(e))
    cci = compute_cci(kind, r)
    band = band_for(kind, cci, r)
    return {"cci": cci, "band": band, "reason": override_reason(kind, cci, r, band)}


# ---------- assets ----------
@app.get("/api/assets/{kind}")
def list_assets(kind: Kind, user: User = Depends(current_user), db: Session = Depends(get_db)):
    J = jmap(db)
    Asset = CFG[kind]["Asset"]
    return [asset_dict(kind, a, J) for a in db.scalars(scoped_query(db, user, kind).order_by(Asset.asset_id))]


def _division_for(db: Session, user: User, district: str):
    divs = [j for j in jmap(db).values() if j.level == "division" and j.district == district]
    if not divs:
        raise HTTPException(422, "Unknown district")
    ids = allowed_division_ids(db, user)
    mine = [d for d in divs if ids is None or d.id in ids]
    if not mine:
        raise HTTPException(403, f"{district} is outside your jurisdiction")
    return mine[0]


def _insert_with_unique_id(db: Session, kind: str, district: str, code: str, make):
    """asset_id embeds a sequence number; if two registrations race, retry with the next number."""
    Asset = CFG[kind]["Asset"]
    n = (db.scalar(select(func.max(Asset.id))) or 0) + 1
    for attempt in range(6):
        a = make(f"GJ-RB-{district[:3].upper()}-{code}-{n + attempt:04d}")
        db.add(a)
        try:
            db.commit()
            db.refresh(a)
            return a
        except IntegrityError:
            db.rollback()
    raise HTTPException(409, "Could not allocate a unique asset ID, please retry")


def _check_year(year):
    if year is not None and year > today_ist().year:
        raise HTTPException(422, "Year built cannot be in the future")


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise HTTPException(422, "Name is required")
    return name


@app.post("/api/assets/building", status_code=201)
def create_building(body: BuildingIn, user: User = Depends(registrar), db: Session = Depends(get_db)):
    if body.type not in BUILDING_TYPES:
        raise HTTPException(422, "Unknown building type")
    _check_year(body.year_built)
    name, div = _clean_name(body.name), _division_for(db, user, body.district)
    valid_coords, coord_err = validate_district_coords(body.district, body.lat, body.lng)
    if not valid_coords:
        raise HTTPException(422, coord_err)
    code = seed.BUILDING_TYPES[body.type][0]
    b = _insert_with_unique_id(db, "building", body.district, code, lambda aid: Building(
        asset_id=aid, name=name, type=body.type, district=body.district, lat=body.lat, lng=body.lng,
        year_built=body.year_built, floors=body.floors, area_sqm=body.area_sqm, criticality=body.criticality,
        division_id=div.id, band="Unrated"))
    audit(db, user, "register", "building", b, f"{b.type}: {b.name}"); db.commit()
    return asset_dict("building", b, jmap(db))


@app.post("/api/assets/road", status_code=201)
def create_road(body: RoadIn, user: User = Depends(registrar), db: Session = Depends(get_db)):
    if body.road_class not in ROAD_CLASSES:
        raise HTTPException(422, "Unknown road class")
    if body.surface not in SURFACES:
        raise HTTPException(422, "Unknown surface type")
    if body.lanes not in LANES:
        raise HTTPException(422, "Lanes must be one of " + ", ".join(map(str, LANES)))
    if any(not (-90 <= la <= 90 and -180 <= lo <= 180) for la, lo in body.path):
        raise HTTPException(422, "Invalid coordinates in path")
    length = path_length_km([list(p) for p in body.path])
    if length <= 0:
        raise HTTPException(422, "The road's start and end must be different places")
    if length > 500:
        raise HTTPException(422, "A single road record longer than 500 km looks wrong; check the path")
    _check_year(body.year_built)
    name, div = _clean_name(body.name), _division_for(db, user, body.district)
    for p_lat, p_lng in body.path:
        valid_coords, coord_err = validate_district_coords(body.district, p_lat, p_lng)
        if not valid_coords:
            raise HTTPException(422, f"Road route point: {coord_err}")
    code, default_crit = seed.ROAD_CLASSES[body.road_class][:2]
    ref = body.road_ref.strip() if body.road_ref and body.road_ref.strip() else None
    r = _insert_with_unique_id(db, "road", body.district, code, lambda aid: Road(
        asset_id=aid, name=name, road_class=body.road_class, road_ref=ref, surface=body.surface, lanes=body.lanes,
        district=body.district, path=[list(p) for p in body.path], length_km=length, year_built=body.year_built,
        traffic_aadt=body.traffic_aadt, criticality=body.criticality or default_crit, division_id=div.id,
        band="Unrated"))
    audit(db, user, "register", "road", r, f"{r.road_class}: {r.name} ({r.length_km} km)"); db.commit()
    return asset_dict("road", r, jmap(db))


@app.get("/api/assets/{kind}/{asset_pk}")
def asset_detail(kind: Kind, asset_pk: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_asset_or_404(db, user, kind, asset_pk)
    d = asset_dict(kind, a, jmap(db))
    d["inspections"] = [inspection_dict(kind, i) for i in a.inspections]
    return d


@app.post("/api/assets/{kind}/{asset_pk}/inspections", status_code=201)
def add_inspection(kind: Kind, asset_pk: int, body: InspectionIn, user: User = Depends(writer),
                   db: Session = Depends(get_db)):
    a = get_asset_or_404(db, user, kind, asset_pk)
    try:
        ratings = validate_ratings(kind, body.ratings)
    except ValueError as e:
        raise HTTPException(422, str(e))
    when = body.inspected_on or today_ist()
    if when > today_ist():
        raise HTTPException(422, "Inspection date cannot be in the future")
    if when < dt.date(2000, 1, 1):
        raise HTTPException(422, "Inspection date is too far in the past")
    cci = compute_cci(kind, ratings)
    band = band_for(kind, cci, ratings)
    Insp, fk = CFG[kind]["Insp"], CFG[kind]["fk"]
    insp = Insp(**{fk: a.id}, inspector_id=user.id, inspected_on=when, ratings=ratings,
                notes=body.notes.strip(), cci=cci, band=band, photos=[])
    db.add(insp)
    recompute(db, kind, a)   # newest non-voided inspection wins, so a back-dated entry cannot overwrite newer state
    audit(db, user, "inspect", kind, a, f"{when.isoformat()}: {band} ({cci})")
    db.commit(); db.refresh(insp)
    return inspection_dict(kind, insp)


@app.post("/api/inspections/{kind}/{insp_id}/void")
def void_inspection(kind: Kind, insp_id: int, body: VoidIn, user: User = Depends(voider),
                    db: Session = Depends(get_db)):
    insp, a = get_inspection_or_404(db, user, kind, insp_id)
    if insp.voided:
        raise HTTPException(409, "Already voided")
    insp.voided, insp.void_reason = True, body.reason.strip()
    recompute(db, kind, a)
    audit(db, user, "void", kind, a, f"inspection {insp.inspected_on.isoformat()}: {insp.void_reason}")
    db.commit()
    return {"ok": True}


# ---------- photos (authenticated; files are never served statically) ----------
def _sniff_image(head: bytes):
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


@app.post("/api/inspections/{kind}/{insp_id}/photo")
def add_photo(kind: Kind, insp_id: int, file: UploadFile = File(...), user: User = Depends(writer),
              db: Session = Depends(get_db)):
    insp, a = get_inspection_or_404(db, user, kind, insp_id)
    if insp.voided:
        raise HTTPException(409, "Cannot add photos to a voided inspection")
    if len(insp.photos or []) >= MAX_PHOTOS_PER_INSPECTION:
        raise HTTPException(409, f"At most {MAX_PHOTOS_PER_INSPECTION} photos per inspection")
    data = file.file.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        raise HTTPException(413, "Image is larger than 5 MB")
    sniffed = _sniff_image(data[:12])
    if not sniffed:   # judged by the file's own bytes, not the client-supplied content type
        raise HTTPException(415, "Upload a JPEG, PNG or WebP image")
    name = f"{uuid.uuid4().hex}{sniffed[0]}"
    with open(os.path.join(UPLOADS, name), "wb") as f:
        f.write(data)
    insp.photos = list(insp.photos or []) + [name]
    audit(db, user, "photo", kind, a, f"inspection {insp.inspected_on.isoformat()}")
    db.commit()
    return {"photos": [f"/api/photos/{kind}/{insp.id}/{n}" for n in insp.photos]}


@app.get("/api/photos/{kind}/{insp_id}/{name}")
def get_photo(kind: Kind, insp_id: int, name: str, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    insp, _ = get_inspection_or_404(db, user, kind, insp_id)
    if name not in (insp.photos or []):        # also blocks path traversal: only stored names pass
        raise HTTPException(404, "Photo not found")
    mt = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[os.path.splitext(name)[1]]
    return FileResponse(os.path.join(UPLOADS, name), media_type=mt, headers={"Cache-Control": "private, max-age=3600"})


# ---------- summaries ----------
ACTION = {
    "building": {
        "Critical": "Recommend an engineer's review before continued full occupancy, and an urgent repair work order.",
        "Poor": "Recommend including in the next repair programme and re-inspecting within six months.",
        "Fair": "Recommend routine maintenance and a re-inspection at the normal annual interval.",
        "Good": "No action beyond routine maintenance."},
    "road": {
        "Critical": "Recommend an urgent site check, interim safety measures (patching, signage or a speed restriction) and an urgent repair work order.",
        "Poor": "Recommend including in the next resurfacing / repair programme and re-inspecting within six months.",
        "Fair": "Recommend routine maintenance (crack sealing, drain cleaning) and re-inspection at the normal annual interval.",
        "Good": "No action beyond routine maintenance."},
}


def template_summary(kind: str, a, insp) -> str:
    labels = COMPONENT_LABELS[kind]
    weak_items = [(k, v) for k, v in sorted(insp.ratings.items(), key=lambda kv: kv[1]) if v <= 3][:2]
    weak = (" and ".join(f"{labels[k].lower()} ({RATING_LABELS[v].lower()})" for k, v in weak_items)
            if weak_items else "none; all components are in minor or no damage")
    why = override_reason(kind, insp.cci, insp.ratings, insp.band)
    override = f" The band is {insp.band} because of {why}, regardless of the overall index." if why else ""
    extent = f", {a.length_km} km" if kind == "road" else ""
    return (f"{a.name} ({a.asset_id}{extent}) has a condition index of {insp.cci}/100 (band: {insp.band}).{override} "
            f"Weakest areas: {weak}. {ACTION[kind][insp.band]}")


@app.post("/api/inspections/{kind}/{insp_id}/summary")
def make_summary(kind: Kind, insp_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    insp, a = get_inspection_or_404(db, user, kind, insp_id)
    if insp.voided:
        raise HTTPException(409, "This inspection was voided")
    if insp.summary:
        return {"summary": insp.summary, "source": "saved"}
    text, source = template_summary(kind, a, insp), "template"
    key = os.getenv("GROQ_API_KEY")
    if key:
        try:
            what = (f"{a.type}, built {a.year_built}" if kind == "building"
                    else f"{a.road_class}, {a.surface}, {a.length_km} km, built {a.year_built}")
            prompt = (f"Asset: {a.name} ({what}). Condition index {insp.cci}/100, band {insp.band}. "
                      f"Component ratings (1=serious damage, 4=minor/none): {insp.ratings}. "
                      f"Inspector notes: {insp.notes or 'none'}. "
                      "Write a 3-sentence maintenance summary for an executive engineer. "
                      "Use only the data given; do not invent facts or costs.")
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"}, timeout=15,
                           json={"model": "llama-3.3-70b-versatile", "temperature": 0.2,
                                 "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text, source = r.json()["choices"][0]["message"]["content"].strip(), "groq"
        except Exception as e:
            log.warning("AI summary failed, using template: %s", type(e).__name__)
    insp.summary = text
    audit(db, user, "summary", kind, a, f"source={source}")
    db.commit()
    return {"summary": text, "source": source}


# ---------- dashboard, priorities, audit ----------
KindFilter = Literal["all", "building", "road"]


def _kinds(f: str):
    return ("building", "road") if f == "all" else (f,)


@app.get("/api/dashboard")
def dashboard(kind: KindFilter = "all", user: User = Depends(current_user), db: Session = Depends(get_db)):
    J = jmap(db)
    rows = [(k, a) for k in _kinds(kind) for a in db.scalars(scoped_query(db, user, k))]
    by_circle = user.role == "CE"
    groups = {}
    for k, a in rows:
        div = J[a.division_id]
        name = J[div.parent_id].name if by_circle else div.name
        g = groups.setdefault(name, {"name": name, "bands": {b: 0 for b in BANDS}, "cci_sum": 0.0, "rated": 0, "overdue": 0})
        g["bands"][a.band or "Unrated"] += 1
        g["overdue"] += is_overdue(k, a.last_inspected)
        if a.cci is not None:
            g["cci_sum"] += a.cci; g["rated"] += 1
    out = []
    for g in sorted(groups.values(), key=lambda x: x["name"]):
        g["avg_cci"] = round(g.pop("cci_sum") / g["rated"], 1) if g["rated"] else None
        g.pop("rated"); out.append(g)
    rated = [a.cci for _, a in rows if a.cci is not None]
    roads = [a for k, a in rows if k == "road"]
    return {"total": len(rows), "by_kind": {k: sum(1 for x, _ in rows if x == k) for k in _kinds(kind)},
            "needs_attention": sum(a.band in ("Poor", "Critical") for _, a in rows),
            "critical": sum(a.band == "Critical" for _, a in rows),
            "overdue": sum(is_overdue(k, a.last_inspected) for k, a in rows),
            "avg_cci": round(sum(rated) / len(rated), 1) if rated else None,
            "road_km": round(sum(r.length_km for r in roads), 1) if roads else None,
            "road_km_needs_attention": round(sum(r.length_km for r in roads if r.band in ("Poor", "Critical")), 1) if roads else None,
            "groups": out, "grouped_by": "circle" if by_circle else "division"}


@app.get("/api/priorities")
def priorities(kind: KindFilter = "all", limit: int = 10, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    J = jmap(db)
    items, unrated = [], []
    for k in _kinds(kind):
        Asset = CFG[k]["Asset"]
        q = scoped_query(db, user, k).options(selectinload(Asset.inspections))   # avoids one query per asset
        for a in db.scalars(q):
            d = asset_dict(k, a, J)
            if a.cci is None:
                unrated.append(d)
                continue
            latest = latest_valid(a)
            d["weakest"] = ([COMPONENT_LABELS[k][c] for c, v in sorted(latest.ratings.items(), key=lambda kv: kv[1]) if v <= 2][:3]
                            if latest else [])
            d["reason"] = override_reason(k, latest.cci, latest.ratings, latest.band) if latest else None
            items.append(d)
    items.sort(key=lambda r: (-r["priority"], r["asset_id"]))
    unrated.sort(key=lambda r: (-r["criticality"], r["asset_id"]))
    return {"items": items[:max(1, min(limit, 50))], "uninspected": unrated[:50], "uninspected_total": len(unrated)}


@app.get("/api/audit")
def audit_log(limit: int = 100, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500)))
    ids = allowed_division_ids(db, user)
    if ids is not None:
        q = q.where(AuditLog.division_id.in_(ids))
    return [{"at": r.at.isoformat(timespec="seconds"), "user": r.username, "action": r.action, "kind": r.kind,
             "asset": r.asset_code, "detail": r.detail} for r in db.scalars(q)]



# ---------- public citizen grievance routes ----------
PHOTO_URL_RE = re.compile(r"/api/public/grievance-photo/grv_[0-9a-f]{32}\.(jpg|png|webp)")


def _phone_key(p: str) -> str:
    """Last 10 digits, so '+91 98765 43210', '098765 43210' and '9876543210' all match.
    Unicode digits (e.g. Gujarati) are converted to 0-9, so the result is always plain ASCII."""
    return "".join(str(int(ch)) for ch in p if ch.isdecimal())[-10:]


@app.get("/api/public/meta")
def public_meta(db: Session = Depends(get_db)):
    buildings = db.scalars(select(Building).order_by(Building.district, Building.name)).all()
    roads = db.scalars(select(Road).order_by(Road.district, Road.name)).all()
    districts = sorted(list(DISTRICT_CONFIG.keys()))
    return {
        "districts": districts,
        "district_config": {
            d: {"center": list(cfg["center"]), "max_km": cfg["max_km"]}
            for d, cfg in DISTRICT_CONFIG.items()
        },
        "categories": {
            "building": GRIEVANCE_BUILDING_CATEGORIES,
            "road": GRIEVANCE_ROAD_CATEGORIES
        },
        "urgencies": GRIEVANCE_URGENCIES,
        "buildings": [{"id": b.id, "name": b.name, "asset_id": b.asset_id, "district": b.district, "type": b.type, "lat": b.lat, "lng": b.lng} for b in buildings],
        "roads": [{"id": r.id, "name": r.name, "asset_id": r.asset_id, "district": r.district, "road_class": r.road_class, "road_ref": r.road_ref, "length_km": r.length_km} for r in roads]
    }


@app.post("/api/public/grievances", status_code=201)
def submit_grievance(body: GrievanceIn, db: Session = Depends(get_db)):
    kind = "road" if body.kind == "road" else "building"
    dist = ""
    if kind == "building":
        if not body.building_id:
            raise HTTPException(422, "Please select a government building")
        b = db.get(Building, body.building_id)
        if not b:
            raise HTTPException(404, "Selected building not found")
        dist = b.district
        valid_cats = GRIEVANCE_BUILDING_CATEGORIES
    else:
        if not body.road_id:
            raise HTTPException(422, "Please select a road")
        r = db.get(Road, body.road_id)
        if not r:
            raise HTTPException(404, "Selected road not found")
        dist = r.district
        valid_cats = GRIEVANCE_ROAD_CATEGORIES

    if body.category not in valid_cats:
        cats_str = ", ".join(valid_cats)
        raise HTTPException(422, f"Invalid category for {kind}. Must be one of: {cats_str}")
    if body.urgency not in GRIEVANCE_URGENCIES:
        urg_str = ", ".join(GRIEVANCE_URGENCIES)
        raise HTTPException(422, f"Invalid urgency level. Must be one of: {urg_str}")
    if not body.citizen_name.strip():
        raise HTTPException(422, "Citizen name is required")
    if not body.citizen_phone.strip() or len(body.citizen_phone.strip()) < 8:
        raise HTTPException(422, "Valid contact phone number is required")
    if not body.description.strip():
        raise HTTPException(422, "Please describe the grievance defect")
    if body.photo_url and not PHOTO_URL_RE.fullmatch(body.photo_url):
        raise HTTPException(422, "Invalid photo reference; upload the photo again")

    seq = (db.scalar(select(func.max(Grievance.id))) or 0) + 1
    prefix = "BLD" if kind == "building" else "ROD"
    ticket_id = f"GRV-GJ-{dist[:3].upper()}-{prefix}-{seq:04d}"

    g = Grievance(
        ticket_id=ticket_id,
        kind=kind,
        building_id=body.building_id if kind == "building" else None,
        road_id=body.road_id if kind == "road" else None,
        citizen_name=body.citizen_name.strip(),
        citizen_phone=body.citizen_phone.strip(),
        citizen_email=body.citizen_email.strip() if body.citizen_email else None,
        category=body.category,
        urgency=body.urgency,
        description=body.description.strip(),
        specific_location=body.specific_location.strip(),
        photo_url=body.photo_url,
        status="Pending",
        resolution_notes=""
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return public_grievance_dict(g)


@app.post("/api/public/grievances/photo")
def upload_grievance_photo(file: UploadFile = File(...)):
    data = file.file.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        raise HTTPException(413, "Image is larger than 5 MB")
    sniffed = _sniff_image(data[:12])
    if not sniffed:
        raise HTTPException(415, "Upload a JPEG, PNG or WebP image")
    name = f"grv_{uuid.uuid4().hex}{sniffed[0]}"
    with open(os.path.join(UPLOADS, name), "wb") as f:
        f.write(data)
    return {"photo_url": f"/api/public/grievance-photo/{name}"}


@app.get("/api/public/grievance-photo/{name}")
def get_grievance_photo(name: str):
    if not name.startswith("grv_") or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(404, "Photo not found")
    path = os.path.join(UPLOADS, name)
    if not os.path.exists(path):
        raise HTTPException(404, "Photo not found")
    mt = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[os.path.splitext(name)[1]]
    return FileResponse(path, media_type=mt, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/public/grievances/track")
def track_grievance(body: TrackIn, request: Request, db: Session = Depends(get_db)):
    """A citizen must give BOTH the Tracking ID and the mobile number the grievance was filed with.
    (The old lookup matched partial names/phones and returned everyone's contact details.)"""
    ip = request.client.host if request.client else "?"
    wait = track_limiter.blocked(ip)
    if wait:
        raise HTTPException(429, f"Too many unmatched lookups. Try again in {wait // 60 + 1} minute(s).",
                            headers={"Retry-After": str(wait)})
    g = db.scalars(
        select(Grievance)
        .options(selectinload(Grievance.building), selectinload(Grievance.road), selectinload(Grievance.resolved_by))
        .where(func.upper(Grievance.ticket_id) == body.ticket_id.strip().upper())
    ).first()
    phone = _phone_key(body.phone)
    if not g or len(phone) < 8 or not hmac.compare_digest(_phone_key(g.citizen_phone), phone):
        track_limiter.record_fail(ip)
        raise HTTPException(404, "No grievance matches that Tracking ID and mobile number. Check both and try again.")
    return public_grievance_dict(g)


# ---------- departmental officer grievance management routes ----------
@app.get("/api/grievances")
def list_officer_grievances(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    search: Optional[str] = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db)
):
    ids = allowed_division_ids(db, user)
    q = select(Grievance).options(selectinload(Grievance.building), selectinload(Grievance.road), selectinload(Grievance.resolved_by))
    
    if kind in ("building", "road"):
        q = q.where(Grievance.kind == kind)
    if status:
        q = q.where(Grievance.status == status)
    if urgency:
        q = q.where(Grievance.urgency == urgency)
    
    search = (search or "").strip()
    if ids is not None or search:
        q = q.outerjoin(Building, Grievance.building_id == Building.id).outerjoin(Road, Grievance.road_id == Road.id)
    # Scoping: if not statewide CE, filter by officer's divisions
    if ids is not None:
        q = q.where(or_(Building.division_id.in_(ids), Road.division_id.in_(ids)))

    if search:
        # % and _ typed by the officer are literal characters, not wildcards
        s = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.where(or_(
            Grievance.ticket_id.ilike(s, escape="\\"),
            Grievance.citizen_name.ilike(s, escape="\\"),
            Grievance.citizen_phone.ilike(s, escape="\\"),
            Grievance.category.ilike(s, escape="\\"),
            Building.name.ilike(s, escape="\\"),
            Road.name.ilike(s, escape="\\"),
            Building.district.ilike(s, escape="\\"),
            Road.district.ilike(s, escape="\\"),
        ))

    q = q.order_by(Grievance.created_at.desc())
    return [grievance_dict(g) for g in db.scalars(q).unique()]


@app.get("/api/grievances/stats")
def officer_grievance_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = allowed_division_ids(db, user)
    q = select(Grievance).options(selectinload(Grievance.building), selectinload(Grievance.road))
    if ids is not None:
        q = q.outerjoin(Building, Grievance.building_id == Building.id).outerjoin(Road, Grievance.road_id == Road.id)
        q = q.where(or_(Building.division_id.in_(ids), Road.division_id.in_(ids)))
    
    all_g = list(db.scalars(q).unique())
    return {
        "total": len(all_g),
        "pending": sum(1 for g in all_g if g.status == "Pending"),
        "under_review": sum(1 for g in all_g if g.status == "Under Review"),
        "work_order_issued": sum(1 for g in all_g if g.status == "Work Order Issued"),
        "resolved": sum(1 for g in all_g if g.status == "Resolved"),
        "rejected": sum(1 for g in all_g if g.status == "Rejected"),
        "emergency": sum(1 for g in all_g if g.urgency == "Emergency" and g.status not in ("Resolved", "Rejected")),
        "building_count": sum(1 for g in all_g if g.kind == "building"),
        "road_count": sum(1 for g in all_g if g.kind == "road"),
    }


@app.patch("/api/grievances/{id}")
def update_grievance_status(id: int, body: GrievanceUpdateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = db.get(Grievance, id)
    if not g:
        raise HTTPException(404, "Grievance not found")
    
    # Check jurisdiction (a grievance with no linked asset is not scoped to anyone below state level)
    ids = allowed_division_ids(db, user)
    asset = g.building if g.kind == "building" else g.road
    if ids is not None and (asset is None or asset.division_id not in ids):
        raise HTTPException(403, "This grievance is outside your jurisdiction")

    if body.status not in GRIEVANCE_STATUSES:
        st_str = ", ".join(GRIEVANCE_STATUSES)
        raise HTTPException(422, f"Invalid status. Must be one of: {st_str}")

    notes = (body.resolution_notes if body.resolution_notes is not None else (g.resolution_notes or "")).strip()
    closing = body.status in CLOSED_STATUSES
    if closing and len(notes) < 3:
        raise HTTPException(422, f"Remarks are required to mark a grievance {body.status}: the citizen sees them on the tracking page")

    previous = g.status
    g.status, g.resolution_notes = body.status, notes
    if closing:
        # Record the closing officer. Stamp again when the outcome changes or a different officer takes it over
        # (e.g. the CE finalises what a JE resolved); a same-officer edit of the remarks keeps the original time.
        if previous != body.status or g.resolved_by_id != user.id or not g.resolved_at:
            g.resolved_at = dt.datetime.now(dt.timezone.utc)
            g.resolved_by_id = user.id
    else:
        g.resolved_at = None       # re-opened: the earlier outcome stays in the audit log
        g.resolved_by_id = None

    if asset:
        audit(db, user, "grievance_action", g.kind, asset,
              f"{g.ticket_id}: {previous} -> {body.status}" + (f" | {notes}" if notes else ""))
    db.commit()
    db.refresh(g)
    return grievance_dict(g)

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
