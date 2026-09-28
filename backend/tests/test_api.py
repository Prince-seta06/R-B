import os, sys, tempfile, datetime as dt
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("APP_ENV", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from fastapi.testclient import TestClient
from main import app
from scoring import compute_cci, band_for, priority_score, WEIGHTS, today_ist
from security import track_limiter

B_GOOD = {k: 4 for k in WEIGHTS["building"]}
R_GOOD = {k: 4 for k in WEIGHTS["road"]}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DAYS = lambda n: (today_ist() - dt.timedelta(days=n)).isoformat()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:      # runs startup: creates tables and seeds demo data
        yield c


_sessions = {}


def as_user(client, username):
    """A separate cookie jar per user, so several users can act in one test."""
    if username not in _sessions:
        c = TestClient(app)
        r = c.post("/api/login", json={"username": username, "password": "demo123"})
        assert r.status_code == 200, r.text
        _sessions[username] = c
    return _sessions[username]


def first(c, kind, **match):
    return next(a for a in c.get(f"/api/assets/{kind}").json() if all(a[k] == v for k, v in match.items()))


# ---------------- scoring ----------------
def test_scoring_basics():
    assert compute_cci("building", B_GOOD) == 100.0 and compute_cci("road", R_GOOD) == 100.0
    assert compute_cci("building", {k: 1 for k in B_GOOD}) == 0.0
    assert band_for("building", 100.0, B_GOOD) == "Good"


@pytest.mark.parametrize("kind,good,comp", [("building", B_GOOD, "structure"), ("building", B_GOOD, "electrical"),
                                            ("building", B_GOOD, "fire_safety"), ("road", R_GOOD, "potholes"),
                                            ("road", R_GOOD, "shoulders")])
def test_serious_safety_damage_forces_critical(kind, good, comp):
    r = dict(good, **{comp: 1})
    assert band_for(kind, compute_cci(kind, r), r) == "Critical"


@pytest.mark.parametrize("kind,good,comp", [("building", B_GOOD, "structure"), ("road", R_GOOD, "potholes")])
def test_major_safety_damage_caps_at_poor(kind, good, comp):
    r = dict(good, **{comp: 2})
    assert compute_cci(kind, r) >= 75          # the average alone would say Good
    assert band_for(kind, compute_cci(kind, r), r) == "Poor"


def test_non_safety_component_does_not_override():
    r = dict(B_GOOD, finishes=1)
    assert band_for("building", compute_cci("building", r), r) == "Good"


def test_override_outranks_a_merely_mediocre_asset():
    unsafe = dict(B_GOOD, structure=1)
    meh = {k: 3 for k in B_GOOD}
    p_unsafe = priority_score(compute_cci("building", unsafe), 2, band_for("building", 70.0, unsafe))
    p_meh = priority_score(compute_cci("building", meh), 2, "Fair")
    assert p_unsafe > p_meh


# ---------------- auth ----------------
def test_requires_auth(client):
    for path in ("/api/assets/building", "/api/assets/road", "/api/dashboard", "/api/priorities", "/api/audit", "/api/meta", "/api/grievances"):
        assert client.get(path).status_code == 401


def test_wrong_password_and_lockout(client):
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/login", json={"username": "ghost", "password": "x"}).status_code == 401
    r = c.post("/api/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_cookie_is_httponly_and_logout_ends_session(client):
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "je_surat", "password": "demo123"})
    assert "httponly" in r.headers["set-cookie"].lower() and "token" not in r.json()
    assert c.get("/api/meta").status_code == 200
    c.post("/api/logout")
    assert c.get("/api/meta").status_code == 401


def test_security_headers(client):
    h = client.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


# ---------------- scoping ----------------
@pytest.mark.parametrize("kind", ["building", "road"])
def test_jurisdiction_scoping(client, kind):
    ce, se, je = as_user(client, "ce_state"), as_user(client, "se_ahmedabad"), as_user(client, "je_surat")
    all_ = ce.get(f"/api/assets/{kind}").json()
    assert {a["district"] for a in je.get(f"/api/assets/{kind}").json()} == {"Surat"}
    assert {a["district"] for a in se.get(f"/api/assets/{kind}").json()} == {"Ahmedabad", "Gandhinagar"}
    other = next(a for a in all_ if a["district"] != "Surat")
    assert je.get(f"/api/assets/{kind}/{other['id']}").status_code == 404


@pytest.mark.parametrize("kind,good", [("building", B_GOOD), ("road", R_GOOD)])
def test_cannot_write_outside_jurisdiction(client, kind, good):
    ce, je = as_user(client, "ce_state"), as_user(client, "je_surat")
    other = next(a for a in ce.get(f"/api/assets/{kind}").json() if a["district"] != "Surat")
    assert je.post(f"/api/assets/{kind}/{other['id']}/inspections", json={"ratings": good}).status_code == 404


def test_data_entry_permissions(client):
    je, se, ce = as_user(client, "je_surat"), as_user(client, "se_surat"), as_user(client, "ce_state")
    a = first(ce, "building")
    # recording inspections and adding photos: Junior Engineers only
    for who in (se, ce):
        assert who.post(f"/api/assets/building/{a['id']}/inspections", json={"ratings": B_GOOD}).status_code == 403
        assert who.get("/api/meta").json()["can_write"] is False
    # registering buildings and roads: Superintending and Chief Engineers only
    assert je.post("/api/assets/building", json=BLD).status_code == 403
    assert je.post("/api/assets/road", json=ROAD).status_code == 403
    assert je.get("/api/meta").json()["can_register"] is False
    assert all(w.get("/api/meta").json()["can_register"] is True for w in (se, ce))


def test_meta_reports_void_permission(client):
    can = {u: as_user(client, u).get("/api/meta").json()["can_void"] for u in ("je_surat", "se_surat", "ce_state")}
    assert can == {"je_surat": False, "se_surat": True, "ce_state": True}


# ---------------- inspections ----------------
@pytest.mark.parametrize("kind,good", [("building", B_GOOD), ("road", R_GOOD)])
def test_inspection_updates_asset_and_audit(client, kind, good):
    je = as_user(client, "je_surat")
    a = first(je, kind)
    r = je.post(f"/api/assets/{kind}/{a['id']}/inspections", json={"ratings": good})
    assert r.status_code == 201 and r.json()["band"] == "Good"
    d = je.get(f"/api/assets/{kind}/{a['id']}").json()
    assert d["cci"] == 100.0 and d["last_inspected"] == today_ist().isoformat() and not d["overdue"]
    assert any(e["action"] == "inspect" and e["asset"] == a["asset_id"] for e in je.get("/api/audit").json())


def test_backdated_inspection_does_not_overwrite_newer(client):
    je = as_user(client, "je_surat")
    a = first(je, "road")
    je.post(f"/api/assets/road/{a['id']}/inspections", json={"ratings": R_GOOD})
    bad = {k: 1 for k in R_GOOD}
    assert je.post(f"/api/assets/road/{a['id']}/inspections", json={"ratings": bad, "inspected_on": DAYS(400)}).status_code == 201
    assert je.get(f"/api/assets/road/{a['id']}").json()["cci"] == 100.0


def test_void_restores_previous_state(client):
    je, se = as_user(client, "je_navsari"), as_user(client, "se_surat")   # Surat circle covers Navsari
    a = first(je, "building")
    je.post(f"/api/assets/building/{a['id']}/inspections", json={"ratings": B_GOOD, "inspected_on": DAYS(10)})
    bad = je.post(f"/api/assets/building/{a['id']}/inspections", json={"ratings": dict(B_GOOD, structure=1)}).json()
    assert je.get(f"/api/assets/building/{a['id']}").json()["band"] == "Critical"
    assert se.post(f"/api/inspections/building/{bad['id']}/void", json={"reason": "x"}).status_code == 422
    assert se.post(f"/api/inspections/building/{bad['id']}/void", json={"reason": "Entered on wrong building"}).status_code == 200
    d = je.get(f"/api/assets/building/{a['id']}").json()
    assert d["band"] == "Good" and d["last_inspected"] == DAYS(10)
    assert next(i for i in d["inspections"] if i["id"] == bad["id"])["voided"] is True
    assert se.post(f"/api/inspections/building/{bad['id']}/void", json={"reason": "again again"}).status_code == 409


def test_void_is_for_se_and_ce_only(client):
    je, ce = as_user(client, "je_surat"), as_user(client, "ce_state")
    se_in, se_out = as_user(client, "se_surat"), as_user(client, "se_ahmedabad")
    a = first(je, "road")

    def new_inspection():
        return je.post(f"/api/assets/road/{a['id']}/inspections", json={"ratings": R_GOOD}).json()["id"]

    void = lambda who, iid: who.post(f"/api/inspections/road/{iid}/void", json={"reason": "Recorded in error"})
    iid = new_inspection()
    assert void(je, iid).status_code == 403          # the author cannot void their own inspection
    assert void(se_out, iid).status_code == 404      # an SE of another circle cannot even see it
    assert void(se_in, iid).status_code == 200       # the circle's SE can
    assert void(ce, new_inspection()).status_code == 200   # the state-wide CE can
    assert any(e["action"] == "void" and e["user"] == "se_surat" for e in ce.get("/api/audit").json())


def test_rejects_bad_ratings(client):
    je = as_user(client, "je_surat")
    b, r = first(je, "building"), first(je, "road")
    url = f"/api/assets/building/{b['id']}/inspections"
    assert je.post(url, json={"ratings": dict(B_GOOD, roof=9)}).status_code == 422
    assert je.post(url, json={"ratings": {"roof": 3}}).status_code == 422
    assert je.post(url, json={"ratings": R_GOOD}).status_code == 422
    assert je.post(url, json={"ratings": B_GOOD, "inspected_on": (today_ist() + dt.timedelta(days=1)).isoformat()}).status_code == 422
    assert je.post(f"/api/assets/road/{r['id']}/inspections", json={"ratings": B_GOOD}).status_code == 422


def test_score_endpoint_matches_saved_result(client):
    je = as_user(client, "je_surat")
    ratings = dict(R_GOOD, potholes=2)
    s = je.post("/api/score/road", json={"ratings": ratings}).json()
    r = first(je, "road")
    saved = je.post(f"/api/assets/road/{r['id']}/inspections", json={"ratings": ratings}).json()
    assert (s["cci"], s["band"]) == (saved["cci"], saved["band"]) and s["band"] == "Poor" and s["reason"]


# ---------------- registering assets & geographic restrictions ----------------
BLD = dict(name="Test Office", type="Government Office", district="Surat", lat=21.17, lng=72.83)
ROAD = dict(name="Test Road", road_class="Major District Road", surface="Bituminous", lanes=2, district="Surat",
            path=[[21.17, 72.83], [21.20, 72.86]])


def test_register_building_validation(client):
    se = as_user(client, "se_surat")
    post = lambda **kw: se.post("/api/assets/building", json={**BLD, **kw}).status_code
    assert post(floors=-3) == 422 and post(floors=0) == 422 and post(area_sqm=-5) == 422
    assert post(year_built=2999) == 422 and post(year_built=1500) == 422
    assert post(name="   ") == 422 and post(type="Palace") == 422 and post(lat=95) == 422
    assert post(district="Atlantis") == 422
    assert post(district="Rajkot") == 403
    r = se.post("/api/assets/building", json=BLD)
    assert r.status_code == 201 and r.json()["asset_id"].startswith("GJ-RB-SUR-OFF-") and r.json()["band"] == "Unrated"
    r2 = se.post("/api/assets/building", json=BLD)
    assert r2.json()["asset_id"] != r.json()["asset_id"]


def test_register_road(client):
    se = as_user(client, "se_surat")
    post = lambda **kw: se.post("/api/assets/road", json={**ROAD, **kw})
    assert post(path=[[21.17, 72.83]]).status_code == 422
    assert post(path=[[21.17, 72.83], [21.17, 72.83]]).status_code == 422
    assert post(path=[[21.17, 72.83], [200, 72.9]]).status_code == 422
    assert post(surface="Glass").status_code == 422 and post(lanes=3).status_code == 422
    assert post(road_class="Motorway").status_code == 422 and post(traffic_aadt=-1).status_code == 422
    assert post(district="Rajkot").status_code == 403
    r = post()
    assert r.status_code == 201
    d = r.json()
    assert d["asset_id"].startswith("GJ-RB-SUR-MDR-") and 4 < d["length_km"] < 6 and d["criticality"] == 2
    assert d["kind"] == "road" and d["band"] == "Unrated" and d["overdue"] is True


def test_geographic_boundary_and_statewide_ce(client):
    se, ce = as_user(client, "se_surat"), as_user(client, "ce_state")

    # Outside Gujarat coordinates rejected
    outside_bld = dict(BLD, lat=28.61, lng=77.20)
    assert se.post("/api/assets/building", json=outside_bld).status_code == 422

    # Wrong district coordinates rejected
    wrong_dist_bld = dict(BLD, lat=23.02, lng=72.57)  # Ahmedabad coordinates with district=Surat
    assert se.post("/api/assets/building", json=wrong_dist_bld).status_code == 422

    # the CE is state-wide and can register in any district
    ce_bld = dict(name="CE State Office", type="Government Office", district="Ahmedabad", lat=23.02, lng=72.57)
    r = ce.post("/api/assets/building", json=ce_bld)
    assert r.status_code == 201 and r.json()["district"] == "Ahmedabad"

    # an SE only inside their own circle (Surat circle: Surat and Navsari), and roads follow the same rule
    assert se.post("/api/assets/building", json=ce_bld).status_code == 403
    assert se.post("/api/assets/road", json=dict(ROAD, district="Ahmedabad")).status_code == 403
    assert se.post("/api/assets/building", json=dict(BLD, name="Navsari Office", district="Navsari", lat=20.9467, lng=72.9520)).status_code == 201


# ---------------- citizen & officer grievances ----------------
def test_public_meta_returns_districts_and_categories(client):
    r = client.get("/api/public/meta")
    assert r.status_code == 200
    d = r.json()
    assert "Surat" in d["districts"]
    assert "Potholes / Road Surface Damage" in d["categories"]["road"]
    assert "Structural Damage" in d["categories"]["building"]
    assert len(d["roads"]) > 0 and len(d["buildings"]) > 0


def test_citizen_submit_and_track_road_and_building_grievances(client):
    meta = client.get("/api/public/meta").json()
    road = next(r for r in meta["roads"] if r["district"] == "Surat")
    bld = next(b for b in meta["buildings"] if b["district"] == "Surat")

    # 1. Submit road grievance
    r_road = client.post("/api/public/grievances", json={
        "kind": "road",
        "road_id": road["id"],
        "category": "Potholes / Road Surface Damage",
        "urgency": "Emergency",
        "specific_location": "Near KM 4.5 bridge",
        "description": "Large deep crater causing vehicles to swerve into oncoming traffic",
        "citizen_name": "Patel Ramesh",
        "citizen_phone": "9876543210",
        "citizen_email": "ramesh@example.com"
    })
    assert r_road.status_code == 201
    road_grv = r_road.json()
    assert road_grv["ticket_id"].startswith("GRV-GJ-SUR-ROD-")
    assert road_grv["status"] == "Pending"
    assert road_grv["urgency"] == "Emergency"

    # 2. Submit building grievance
    r_bld = client.post("/api/public/grievances", json={
        "kind": "building",
        "building_id": bld["id"],
        "category": "Roof Leakage / Seepage",
        "urgency": "Urgent",
        "specific_location": "Room 102",
        "description": "Severe water leakage from the ceiling during rain",
        "citizen_name": "Mehta Sneha",
        "citizen_phone": "9123456780"
    })
    assert r_bld.status_code == 201
    bld_grv = r_bld.json()
    assert bld_grv["ticket_id"].startswith("GRV-GJ-SUR-BLD-")

    # 3. Track with Ticket ID + the mobile it was filed with (any common formatting of the number)
    for phone in ("9876543210", "+91 98765-43210", "098765 43210"):
        r = client.post("/api/public/grievances/track", json={"ticket_id": road_grv["ticket_id"].lower(), "phone": phone})
        assert r.status_code == 200 and r.json()["ticket_id"] == road_grv["ticket_id"] and r.json()["kind"] == "road"
    assert client.post("/api/public/grievances/track",
                       json={"ticket_id": bld_grv["ticket_id"], "phone": "9123456780"}).json()["kind"] == "building"


def test_officer_grievance_management(client):
    je = as_user(client, "je_surat")
    ce = as_user(client, "ce_state")

    # Stats endpoint returns road and building counts
    stats = je.get("/api/grievances/stats").json()
    assert "total" in stats and "road_count" in stats and "building_count" in stats
    assert stats["pending"] >= 1

    # Filter grievances by road only
    roads_only = je.get("/api/grievances?kind=road").json()
    assert all(g["kind"] == "road" for g in roads_only)

    # Filter grievances by building only
    bld_only = je.get("/api/grievances?kind=building").json()
    assert all(g["kind"] == "building" for g in bld_only)

    # Update grievance status to Under Review, then Work Order Issued, then Resolved
    g = roads_only[0]
    r_up = je.patch(f"/api/grievances/{g['id']}", json={
        "status": "Work Order Issued",
        "resolution_notes": "Assigned to road contractor for hot-mix asphalt patching"
    })
    assert r_up.status_code == 200
    assert r_up.json()["status"] == "Work Order Issued"
    assert "contractor" in r_up.json()["resolution_notes"]

    # Resolve grievance
    r_res = je.patch(f"/api/grievances/{g['id']}", json={
        "status": "Resolved",
        "resolution_notes": "Patching completed and inspected on site"
    })
    assert r_res.status_code == 200
    assert r_res.json()["status"] == "Resolved"
    assert r_res.json()["resolved_at"] is not None


# ---------------- grievance resolution is recorded and visible to the citizen ----------------
def _file_grievance(client, phone, district="Surat", kind="road", urgency="Normal"):
    """File as a citizen. Returns the public response plus the internal `id` (looked up as the CE),
    because the public response deliberately does not expose row ids."""
    meta = client.get("/api/public/meta").json()
    asset = next(a for a in meta["roads" if kind == "road" else "buildings"] if a["district"] == district)
    r = client.post("/api/public/grievances", json={
        "kind": kind, ("road_id" if kind == "road" else "building_id"): asset["id"],
        "category": meta["categories"][kind][0], "urgency": urgency, "description": "Deep crater on the carriageway",
        "citizen_name": "Test Citizen", "citizen_phone": phone, "citizen_email": "t@example.com"})
    assert r.status_code == 201, r.text
    pub = r.json()
    rows = as_user(client, "ce_state").get("/api/grievances", params={"search": pub["ticket_id"]}).json()
    return {**pub, "id": next(x["id"] for x in rows if x["ticket_id"] == pub["ticket_id"])}


def _track(client, ticket, phone):
    return client.post("/api/public/grievances/track", json={"ticket_id": ticket, "phone": phone})


def test_ce_resolution_is_recorded_and_citizen_sees_it(client):
    ce = as_user(client, "ce_state")
    g = _file_grievance(client, "9000011111")
    url = f"/api/grievances/{g['id']}"
    assert ce.patch(url, json={"status": "Resolved", "resolution_notes": ""}).status_code == 422     # remarks required
    assert ce.patch(url, json={"status": "Rejected", "resolution_notes": "  "}).status_code == 422
    r = ce.patch(url, json={"status": "Resolved", "resolution_notes": "Crater filled and compacted; inspected by CE office"})
    assert r.status_code == 200
    d = r.json()
    assert d["resolved_by"]                                                   # the officer's name is recorded
    assert d["resolved_by_role"] == "Chief Engineer" and d["resolved_at"] and d["resolved_at_display"].endswith("IST")

    seen = _track(client, g["ticket_id"], "9000011111").json()                # what the citizen sees
    assert seen["status"] == "Resolved" and seen["resolution_notes"].startswith("Crater filled")
    assert seen["resolved_by_role"] == "Chief Engineer" and seen["resolved_by"] and seen["resolved_at_display"]
    assert any(e["action"] == "grievance_action" and g["ticket_id"] in e["detail"] and "Crater filled" in e["detail"]
               for e in ce.get("/api/audit").json())


def test_reopening_clears_outcome_and_new_closer_is_recorded(client):
    je, ce = as_user(client, "je_surat"), as_user(client, "ce_state")
    g = _file_grievance(client, "9000022222")
    url = f"/api/grievances/{g['id']}"
    first_close = je.patch(url, json={"status": "Resolved", "resolution_notes": "Patched by division"}).json()
    assert first_close["resolved_by_role"] == "Junior Engineer"
    second = ce.patch(url, json={"status": "Resolved", "resolution_notes": "Verified on site by CE"}).json()
    assert second["resolved_by_role"] == "Chief Engineer"                       # latest closing officer wins
    reopened = ce.patch(url, json={"status": "Under Review", "resolution_notes": "Reported to have recurred"}).json()
    assert reopened["resolved_at"] is None and reopened["resolved_by"] is None
    assert _track(client, g["ticket_id"], "9000022222").json()["resolved_by"] is None


def test_rejected_grievance_shows_reason_and_officer(client):
    se = as_user(client, "se_surat")
    g = _file_grievance(client, "9000033333")
    r = se.patch(f"/api/grievances/{g['id']}", json={"status": "Rejected", "resolution_notes": "Road belongs to the municipality"})
    assert r.status_code == 200
    seen = _track(client, g["ticket_id"], "9000033333").json()
    assert seen["status"] == "Rejected" and seen["resolved_by_role"] == "Superintending Engineer"
    assert "municipality" in seen["resolution_notes"]


def test_officers_cannot_close_grievances_outside_their_area(client):
    g = _file_grievance(client, "9000044444", district="Surat")
    assert as_user(client, "je_ahmedabad").patch(
        f"/api/grievances/{g['id']}", json={"status": "Resolved", "resolution_notes": "not mine"}).status_code == 403


def test_officer_grievance_search_works_for_scoped_officers(client):
    je = as_user(client, "je_surat")
    assert je.get("/api/grievances?search=Test%20Citizen").status_code == 200
    assert all(g["district"] == "Surat" for g in je.get("/api/grievances?search=crater").json())


# ---------------- public tracking does not leak other citizens' data ----------------
def test_tracking_needs_matching_ticket_and_mobile(client):
    g = _file_grievance(client, "9000055555")
    ok = _track(client, g["ticket_id"], "9000055555")
    assert ok.status_code == 200
    public = ok.json()
    assert "citizen_phone" not in public and "citizen_email" not in public and "id" not in public
    for bad in ("9000055556", "1234567890", "5555"):
        assert _track(client, g["ticket_id"], bad).status_code in (404, 422)
    assert _track(client, "GRV-GJ-SUR-ROD-9999", "9000055555").status_code == 404
    # the old open lookup (by partial name, phone or ticket) is gone
    assert client.get("/api/public/grievances/track/Test").status_code in (404, 405)
    assert client.get("/api/public/grievances/track/9000055555").status_code in (404, 405)
    track_limiter.reset("testclient")


def test_tracking_is_rate_limited_against_guessing(client):
    g = _file_grievance(client, "9000066666")
    track_limiter.reset("testclient")
    for n in range(8):
        assert _track(client, g["ticket_id"], f"90000000{n:02d}").status_code == 404
    r = _track(client, g["ticket_id"], "9000066666")          # even the right pair is refused while locked out
    assert r.status_code == 429 and "Retry-After" in r.headers
    track_limiter.reset("testclient")


def test_grievance_photo_reference_is_validated(client):
    meta = client.get("/api/public/meta").json()
    road = next(r for r in meta["roads"] if r["district"] == "Surat")
    base = {"kind": "road", "road_id": road["id"], "category": meta["categories"]["road"][0], "urgency": "Normal",
            "description": "Broken edge", "citizen_name": "Photo Tester", "citizen_phone": "9000077777"}
    assert client.post("/api/public/grievances", json={**base, "photo_url": "javascript:alert(1)"}).status_code == 422
    assert client.post("/api/public/grievances", json={**base, "photo_url": "https://evil.example/x.png"}).status_code == 422
    good = "/api/public/grievance-photo/grv_" + "a" * 32 + ".png"
    assert client.post("/api/public/grievances", json={**base, "photo_url": good}).status_code == 201



# ---------------- officer grievance filters ----------------
def test_officer_grievance_filters_combine_search_and_scope(client):
    ce, je = as_user(client, "ce_state"), as_user(client, "je_surat")
    se_ahm = as_user(client, "se_ahmedabad")
    road = _file_grievance(client, "9000088881", kind="road", urgency="Emergency")
    bld = _file_grievance(client, "9000088882", kind="building", urgency="Urgent")
    get = lambda who, **p: who.get("/api/grievances", params=p).json()
    tickets = lambda rows: {r["ticket_id"] for r in rows}

    # each filter on its own, then all three together (the combination in the reported screenshot)
    assert road["ticket_id"] in tickets(get(ce, kind="road")) and road["ticket_id"] not in tickets(get(ce, kind="building"))
    assert bld["ticket_id"] in tickets(get(ce, urgency="Urgent")) and road["ticket_id"] not in tickets(get(ce, urgency="Urgent"))
    combo = get(ce, kind="building", status="Pending", urgency="Emergency")
    assert all(r["kind"] == "building" and r["status"] == "Pending" and r["urgency"] == "Emergency" for r in combo)
    assert tickets(get(ce, kind="road", status="Pending", urgency="Emergency")) >= {road["ticket_id"]}
    assert bld["ticket_id"] not in tickets(combo)

    # status filter follows a status change
    assert ce.patch(f"/api/grievances/{road['id']}", json={"status": "Under Review", "resolution_notes": ""}).status_code == 200
    assert road["ticket_id"] not in tickets(get(ce, status="Pending"))
    assert road["ticket_id"] in tickets(get(ce, status="Under Review"))

    # search: ticket id, district (the placeholder promises it), and % / _ are literal, not wildcards
    assert road["ticket_id"] in tickets(get(ce, search=road["ticket_id"][-9:]))
    assert road["ticket_id"] in tickets(get(ce, search="surat"))
    assert get(ce, search="%") == [] and get(ce, search="_") == []

    # scoped officers can search AND filter (this used to join the same tables twice)
    rows = get(je, search="surat", kind="road", status="Under Review")
    assert road["ticket_id"] in tickets(rows) and all(r["district"] == "Surat" for r in rows)
    assert all(r["district"] in ("Ahmedabad", "Gandhinagar") for r in get(se_ahm, search="surat"))
    assert all(r["district"] in ("Ahmedabad", "Gandhinagar") for r in get(se_ahm, kind="building", urgency="Emergency"))


# ---------------- front-end guard: no <form> nested inside the modal's own <form> ----------------
def test_grievance_modal_has_no_nested_form():
    """openModal() wraps its content in <form class="modal">. A nested <form> is dropped by the HTML parser,
    so its onsubmit handler is never bound and Save falls back to a native GET that reloads the page."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "app.js"), encoding="utf-8").read()
    body = src[src.index("function openGrievanceModal("):src.index("/* ---------- audit ---------- */")]
    assert "<form" not in body
    assert "root.querySelector('form').onsubmit" in body
