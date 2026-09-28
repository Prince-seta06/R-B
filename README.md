# R&B Asset Register (buildings and roads)

Inventory, inspection and maintenance prioritisation for Gujarat R&B government **buildings and roads**.
Intended to sit alongside GujRAMS: roads here carry a stub link to a GujRAMS record, and buildings a link to the nearest GujRAMS road.
The road model follows common Indian road-survey practice (inventory plus a visual condition survey of surface, potholes,
riding quality, drainage and shoulders). It is **not** GujRAMS's own data model, which was not available when this was written;
check the fields against GujRAMS before claiming compatibility.

## Run (about 2 minutes)
```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open http://localhost:8000. Demo data seeds itself on first start (see "Upgrading" if you have an older `rnb_assets.db`).

| Login | Password | Can do |
|---|---|---|
| `je_surat` (any `je_<district>`) | `demo123` | see one division; **record inspections and add photos**; act on grievances. Cannot register assets or void |
| `se_ahmedabad` (any `se_<circle>`) | `demo123` | see one circle; **register buildings and roads** and **void inspections** in it; act on grievances. Cannot record inspections |
| `ce_state` | `demo123` | see the whole state; **register buildings and roads** in any district and **void inspections**; act on grievances. Cannot record inspections |

All data is synthetic (invented names, ratings, road geometries near district HQs).

## Settings (environment variables)
| Variable | Effect |
|---|---|
| `APP_ENV=production` | Refuses to start without a strong `JWT_SECRET`, marks the session cookie `Secure`, disables `/docs`, and stops seeding demo accounts |
| `JWT_SECRET` | Signing key, at least 32 characters in production. In dev a random key is generated per run (sessions reset on restart) |
| `SEED_DEMO=1` / `0` | Force demo data and accounts on or off. Default: on in dev, off in production |
| `DATABASE_URL` | PostgreSQL instead of SQLite (see docker-compose.yml) |
| `COOKIE_SECURE=1` | Force the `Secure` cookie flag (implied by production; needs HTTPS) |
| `GROQ_API_KEY` | Summaries written by Llama 3.3 70b via Groq. **This sends the asset name, ratings and the inspector's notes to a third party**; check that is allowed for your data before setting it. Without it a rule-based summary is used |

## How it works
- Ratings use a 1-4 scale (1 serious damage, 4 minor or none) over six components per asset type.
  - Buildings: structure, roof, electrical, plumbing, fire safety, finishes.
  - Roads: pavement surface, potholes and patching, riding quality, drains and culverts, shoulders and embankment, markings/signs/barriers.
- Condition index = weighted average scaled to 0-100. Bands: Good 75+, Fair 50+, Poor 25+, Critical below.
- **Safety rules** override the average: a safety-critical component rated 1 forces **Critical**; rated 2 caps the band at **Poor**.
  Safety-critical components are structure, electrical and fire safety for buildings, and potholes and shoulders for roads.
- Priority = (100 - index) x criticality factor (1, 1.25, 1.5). Critical-band assets are ranked as if their index were no better than 24.9,
  so a safety override is never outranked by a merely mediocre asset. Buildings and roads share one ranking; it is an ordering aid, not a cost estimate.
- Assets that have never been inspected are listed separately under priorities.
- Inspections are overdue after 365 days. Road length is calculated from the drawn centre-line.
- Access follows the R&B hierarchy (JE division, SE circle, CE state), enforced on the server for every endpoint, **including photos**.
  Only Junior Engineers can record inspections and add photos (`WRITE_ROLES` in `main.py`).
  Only Superintending and Chief Engineers can register buildings and roads (`REGISTER_ROLES`) and void inspections (`VOID_ROLES`):
  the person who inspects an asset neither decides that it exists nor erases their own record. An SE can only do either inside
  their own circle; the CE can do both anywhere.
- Wrong inspections are **voided** with a reason, not deleted. The asset's current condition falls back to the newest valid inspection.
- Every write is recorded in the audit log (Audit log tab).
- **Citizen grievances**: citizens file a grievance against a road or building and track it publicly. Officers (JE/SE/CE, within their
  jurisdiction) move it through Pending, Under Review, Work Order Issued, Resolved or Rejected.
  - Closing a ticket (Resolved or Rejected) **requires remarks**. The officer's name, designation (e.g. "Chief Engineer") and the time
    are recorded and shown to the citizen with the remarks. If another officer later closes it again, the latest closer is recorded;
    re-opening clears the outcome. Every status change, with its remarks, is in the audit log.
  - To track a ticket the citizen must enter **both** the Tracking ID and the mobile number it was filed with. The public view never
    shows the phone number or e-mail, and wrong pairs are rate-limited per IP (8 per 15 minutes).
- Scoring lives only on the server; the inspection form's live preview asks `/api/score/<kind>`.

**The component lists, weights, band thresholds and safety rules are illustrative defaults.** R&B engineers should calibrate them before the numbers inform real decisions.

## Security notes
- Session is an httpOnly, `SameSite=Strict` cookie (no token in JavaScript). Passwords use PBKDF2-SHA256 at 600k iterations.
- Login is limited to 5 failures per IP + username per 15 minutes. The limiter is in-memory: with several server processes, or behind a
  reverse proxy that hides client IPs, back it with Redis or the proxy's real-IP header.
- Grievance photo references on the public form must be one of the app's own uploaded photos (no arbitrary URLs).
- Photos are validated by their file contents (JPEG/PNG/WebP, 5 MB, 10 per inspection) and served only through an authenticated route.
- A Content-Security-Policy blocks inline and third-party scripts. Leaflet and Chart.js are self-hosted in `static/vendor`.
  Map tiles still load from OpenStreetMap, so the map background needs internet access.
- Run behind HTTPS in production.

## Upgrading
Grievance changes need **no schema change**. The demo database was backfilled so resolved tickets show who closed them; in your own
copy, tickets resolved before this update have no recorded officer and show "by a departmental officer" until they are re-saved.
Public tracking changed from `GET /api/public/grievances/track/{query}` to `POST /api/public/grievances/track` (`ticket_id`, `phone`).

## Upgrading from the earlier version
The schema changed (roads, voiding, audit log). Delete the old `backend/rnb_assets.db` (demo data only) and restart. There is no migration tool yet.

## Tests
```bash
cd backend && pip install -r requirements-dev.txt && pytest
```

## Known limits (be upfront in the demo)
- Jurisdiction, users and asset data are synthetic. WMS and GujRAMS links are stub fields, not live integrations.
- **Production data loading is not built**: with `APP_ENV=production` there are no users or jurisdictions until you add them (there is no admin UI or import yet).
- Roads are single records with one centre-line and one rating per inspection; there is no chainage-level (segment) survey yet.
  Bridges, culverts as separate assets, and cost estimation are not covered.
- No offline sync or CSV import.
- The map loads every asset at once and draws each individually. Fine for hundreds; thousands would need bounding-box queries and marker clustering.
- Ratings are stored as JSON on each inspection (fast to build; a component table would suit heavy reporting).
- No schema migrations (Alembic) yet.
