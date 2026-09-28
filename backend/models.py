import datetime as dt
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text, JSON, Boolean
from sqlalchemy.orm import relationship
from db import Base


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Jurisdiction(Base):
    """Circle -> Division hierarchy (mirrors the R&B org structure)."""
    __tablename__ = "jurisdictions"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    level = Column(String, nullable=False)  # "circle" | "division"
    parent_id = Column(Integer, ForeignKey("jurisdictions.id"), nullable=True)
    district = Column(String)  # set on divisions: the district they serve


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # JE (division) | SE (circle) | CE (state-wide)
    jurisdiction_id = Column(Integer, ForeignKey("jurisdictions.id"), nullable=True)


class Building(Base):
    __tablename__ = "buildings"
    id = Column(Integer, primary_key=True)
    asset_id = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    district = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    year_built = Column(Integer)
    floors = Column(Integer, default=1)
    area_sqm = Column(Integer)
    criticality = Column(Integer, default=2)  # 1 low, 2 medium, 3 high
    division_id = Column(Integer, ForeignKey("jurisdictions.id"), nullable=False, index=True)
    # Integration stubs (read-only links to other R&B systems)
    wms_work_id = Column(String)
    gujrams_road_id = Column(String)
    # Derived from the latest non-voided inspection
    cci = Column(Float)
    band = Column(String, default="Unrated")
    last_inspected = Column(Date)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    inspections = relationship("Inspection", back_populates="asset", cascade="all, delete-orphan",
                               order_by="(Inspection.inspected_on.desc(), Inspection.id.desc())")
    grievances = relationship("Grievance", back_populates="building", cascade="all, delete-orphan",
                              order_by="(Grievance.created_at.desc(), Grievance.id.desc())")


class Road(Base):
    __tablename__ = "roads"
    id = Column(Integer, primary_key=True)
    asset_id = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    road_class = Column(String, nullable=False)   # State Highway | Major District Road | ...
    road_ref = Column(String)                     # e.g. "SH-6"
    surface = Column(String, nullable=False)      # Bituminous | Concrete | WBM / Gravel | Earthen
    lanes = Column(Integer, default=2)
    district = Column(String, nullable=False)
    path = Column(JSON, nullable=False)           # [[lat, lng], ...] centre-line, at least 2 points
    length_km = Column(Float, nullable=False)     # computed from `path`
    year_built = Column(Integer)
    traffic_aadt = Column(Integer)                # vehicles/day, optional
    criticality = Column(Integer, default=2)
    division_id = Column(Integer, ForeignKey("jurisdictions.id"), nullable=False, index=True)
    wms_work_id = Column(String)                  # stub link to works monitoring
    gujrams_id = Column(String)                   # stub link to the GujRAMS record
    cci = Column(Float)
    band = Column(String, default="Unrated")
    last_inspected = Column(Date)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    inspections = relationship("RoadInspection", back_populates="asset", cascade="all, delete-orphan",
                               order_by="(RoadInspection.inspected_on.desc(), RoadInspection.id.desc())")
    grievances = relationship("Grievance", back_populates="road", cascade="all, delete-orphan",
                              order_by="(Grievance.created_at.desc(), Grievance.id.desc())")


class _InspectionColumns:
    """Shared columns (declared per class below because the foreign key differs)."""
    id = Column(Integer, primary_key=True)
    inspected_on = Column(Date, nullable=False)
    ratings = Column(JSON, nullable=False)   # {"structure": 1..4, ...}
    notes = Column(Text, default="")
    cci = Column(Float, nullable=False)
    band = Column(String, nullable=False)
    photos = Column(JSON, default=list)      # bare file names, served only via the authenticated route
    summary = Column(Text)
    voided = Column(Boolean, default=False, nullable=False)
    void_reason = Column(Text)


class Inspection(_InspectionColumns, Base):
    __tablename__ = "inspections"
    building_id = Column(Integer, ForeignKey("buildings.id"), nullable=False, index=True)
    inspector_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    asset = relationship("Building", back_populates="inspections")
    inspector = relationship("User")


class RoadInspection(_InspectionColumns, Base):
    __tablename__ = "road_inspections"
    road_id = Column(Integer, ForeignKey("roads.id"), nullable=False, index=True)
    inspector_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    asset = relationship("Road", back_populates="inspections")
    inspector = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    username = Column(String)
    action = Column(String, nullable=False)   # register | inspect | void | photo | summary | grievance_action
    kind = Column(String)
    asset_code = Column(String)
    division_id = Column(Integer, ForeignKey("jurisdictions.id"), index=True)
    detail = Column(Text, default="")


class Grievance(Base):
    __tablename__ = "grievances"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(String, unique=True, nullable=False, index=True)
    kind = Column(String, nullable=False, default="building")  # "building" | "road"
    building_id = Column(Integer, ForeignKey("buildings.id"), nullable=True, index=True)
    road_id = Column(Integer, ForeignKey("roads.id"), nullable=True, index=True)
    citizen_name = Column(String, nullable=False)
    citizen_phone = Column(String, nullable=False)
    citizen_email = Column(String, nullable=True)
    category = Column(String, nullable=False)
    urgency = Column(String, default="Normal")  # "Normal", "Urgent", "Emergency"
    description = Column(Text, nullable=False)
    specific_location = Column(String, default="")  # e.g. "Near KM 14.2" or "Room 203"
    photo_url = Column(String, nullable=True)
    status = Column(String, default="Pending")  # "Pending", "Under Review", "Work Order Issued", "Resolved", "Rejected"
    resolution_notes = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    building = relationship("Building", back_populates="grievances")
    road = relationship("Road", back_populates="grievances")
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
