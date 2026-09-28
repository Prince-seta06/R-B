import os
import sys
import shutil

# 1. Add 'backend' to sys.path so imports (db, models, seed, scoring, etc.) succeed
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
BACKEND_DIR = os.path.join(ROOT_DIR, "backend")

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# 2. Redirect SQLite database and uploads to /tmp (the only writable directory in Vercel serverless)
DB_PATH = "/tmp/rnb_assets.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

bundled_db = os.path.join(BACKEND_DIR, "rnb_assets.db")
if os.path.exists(bundled_db) and not os.path.exists(DB_PATH):
    try:
        shutil.copy2(bundled_db, DB_PATH)
    except Exception:
        pass

UPLOADS_DIR = "/tmp/uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

if not os.getenv("JWT_SECRET"):
    os.environ["JWT_SECRET"] = "e62c1404d5bf731551aa7434fa50ab52"

# 3. Import and configure FastAPI app
import main
main.UPLOADS = UPLOADS_DIR

app = main.app
