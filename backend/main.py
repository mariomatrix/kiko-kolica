from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Optional, List
import os
from datetime import datetime, timezone

from config import settings
from db import get_db, get_db_conn
from logging_config import logger
import schemas

app = FastAPI(
    title="Kolica Scan API",
    description="API za praćenje i skeniranje dijelova u proizvodnji",
    version="1.1.0"
)

# Sessions & CORS
app.add_middleware(SessionMiddleware, secret_key=settings.KOLICA_SESSION_SECRET, same_site="lax")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth Helpers
def get_current_user_or_none(request: Request):
    auth = request.session.get("auth")
    if not auth:
        return None
    try:
        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT username, role, display_name, active FROM korisnici WHERE username = %s",
                (auth.get("username"),),
            ).fetchone()
            if not row or not row["active"]:
                request.session.pop("auth", None)
                return None
            return dict(row)
    except Exception as e:
        logger.error(f"Error getting current user: {e}")
        return None

def require_auth(request: Request):
    user = get_current_user_or_none(request)
    if not user:
        raise HTTPException(status_code=401, detail="Prijava je obavezna.")
    return user

def require_admin(request: Request):
    user = require_auth(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Ova akcija je dozvoljena samo administratoru.")
    return user

# Row formatters
def format_row_vrijeme(row):
    if row and row.get("vrijeme"):
        val = row["vrijeme"]
        if isinstance(val, datetime):
            row["vrijeme"] = val.strftime("%Y-%m-%d %H:%M:%S")
    return row

def format_rows_vrijeme(rows):
    return [format_row_vrijeme(dict(r)) for r in rows]

# Static & Frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_path), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(os.path.join(frontend_path, "index.html"))

@app.get("/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc)}

# Auth Endpoints
@app.get("/auth/me", response_model=schemas.UserResponse)
def auth_me(request: Request):
    user = get_current_user_or_none(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niste prijavljeni.")
    return user

@app.post("/auth/login", response_model=schemas.UserResponse)
def auth_login(body: schemas.LoginRequest, request: Request, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT username, password, role, display_name, active FROM korisnici WHERE username = %s",
        (body.username.strip(),),
    ).fetchone()

    if not row or row["password"] != body.password or not row["active"]:
        raise HTTPException(status_code=401, detail="Neispravno korisničko ime ili lozinka.")

    request.session["auth"] = {"username": row["username"]}
    return {
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"] or row["username"],
    }

@app.post("/auth/logout")
def auth_logout(request: Request):
    request.session.pop("auth", None)
    return {"ok": True}

# Ture & Dijelovi
@app.get("/ture", response_model=List[str])
def get_ture(request: Request, conn=Depends(get_db)):
    require_auth(request)
    rows = conn.execute("SELECT DISTINCT tura FROM dijelovi ORDER BY tura").fetchall()
    return [r["tura"] for r in rows]

@app.get("/dijelovi", response_model=List[schemas.DioResponse])
def get_dijelovi(request: Request, tura: Optional[str] = None, conn=Depends(get_db)):
    require_auth(request)
    if tura:
        rows = conn.execute("SELECT * FROM dijelovi WHERE tura = %s ORDER BY id", (tura,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM dijelovi ORDER BY tura, id").fetchall()
    return format_rows_vrijeme(rows)

@app.get("/scan/{barcode}", response_model=schemas.ScanResponse)
def scan_barcode(barcode: str, request: Request, conn=Depends(get_db)):
    user = require_auth(request)
    all_rows = conn.execute("SELECT * FROM dijelovi WHERE LOWER(barcode) = LOWER(%s)", (barcode,)).fetchall()
    if not all_rows:
        raise HTTPException(status_code=404, detail=f"Barcode '{barcode}' nije pronađen.")

    target = conn.execute(
        "SELECT * FROM dijelovi WHERE LOWER(barcode) = LOWER(%s) AND status = 'IZREZANO' AND scan_count < kom ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1",
        (barcode,)
    ).fetchone()

    if not target:
        statusi = list({r["status"] for r in all_rows})
        sample_row = format_row_vrijeme(dict(all_rows[0]))
        kom = max(int(sample_row["kom"] or 1), 1)
        scan_count = min(max(int(sample_row["scan_count"] or 0), 0), kom)
        return {
            "found": True,
            "has_izrezano": False,
            "total": len(all_rows),
            "statusi": statusi,
            "row": sample_row,
            "completed_progress": f"{scan_count} od {kom}",
        }

    kom = max(int(target["kom"] or 1), 1)
    new_scan_count = min(max(int(target["scan_count"] or 0), 0) + 1, kom)
    scan_time = datetime.now(timezone.utc)

    conn.execute(
        "UPDATE dijelovi SET status = 'SAVIJENO', scan_count = %s, vrijeme = %s, operater = %s WHERE id = %s",
        (new_scan_count, scan_time, user["display_name"], target["id"])
    )
    # conn.commit() is handled by context manager if not in transaction, but Depends(get_db) uses context manager from get_db_conn
    # psycopg connection in context manager commits on exit if no error

    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (target["id"],)).fetchone()
    return {
        "found": True,
        "has_izrezano": True,
        "remaining_scans": max(kom - new_scan_count, 0),
        "row": format_row_vrijeme(dict(updated))
    }

@app.patch("/dijelovi/{id}/status", response_model=schemas.DioResponse)
def update_status(id: int, body: schemas.StatusUpdate, request: Request, conn=Depends(get_db)):
    require_admin(request)
    row = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Redak nije pronađen.")

    kom = max(int(row["kom"] or 1), 1)
    scan_count = min(max(int(row["scan_count"] or 0), 0), kom)
    kolica = row["kolica"]
    if row["status"] == "SAVIJENO" and body.status != "SAVIJENO":
        kolica = None

    vrijeme = row["vrijeme"]
    if body.status == "IZREZANO":
        scan_count = 0
        vrijeme = None
    elif body.status == "SAVIJENO":
        scan_count = kom
        if not vrijeme:
            vrijeme = datetime.now(timezone.utc)

    conn.execute(
        "UPDATE dijelovi SET status = %s, kolica = %s, scan_count = %s, vrijeme = %s WHERE id = %s",
        (body.status, kolica, scan_count, vrijeme, id)
    )
    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    return format_row_vrijeme(dict(updated))

@app.patch("/dijelovi/{id}/kolica", response_model=schemas.DioResponse)
def update_kolica(id: int, body: schemas.KolicaUpdate, request: Request, conn=Depends(get_db)):
    user = require_auth(request)
    row = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Redak nije pronađen.")
    conn.execute(
        "UPDATE dijelovi SET kolica = %s, operater = %s WHERE id = %s",
        (body.kolica, user["display_name"], id)
    )
    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    return format_row_vrijeme(dict(updated))

@app.patch("/dijelovi/{id}/kolica-admin", response_model=schemas.DioResponse)
def update_kolica_admin(id: int, body: schemas.AdminKolicaUpdate, request: Request, conn=Depends(get_db)):
    user = require_admin(request)
    row = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Redak nije pronađen.")
    conn.execute(
        "UPDATE dijelovi SET kolica = %s, operater = %s WHERE id = %s",
        (body.kolica, user["display_name"], id)
    )
    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    return format_row_vrijeme(dict(updated))

@app.patch("/dijelovi/{id}/kom", response_model=schemas.DioResponse)
def update_kom(id: int, body: schemas.KomUpdate, request: Request, conn=Depends(get_db)):
    require_admin(request)
    if body.kom < 1:
        raise HTTPException(status_code=400, detail="KOM mora biti barem 1.")
    row = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Redak nije pronađen.")

    scan_count = min(max(int(row["scan_count"] or 0), 0), body.kom)
    status = row["status"]
    if scan_count == 0 and status == "SAVIJENO":
        status = "IZREZANO"
    elif scan_count > 0 and status == "IZREZANO":
        status = "SAVIJENO"

    conn.execute(
        "UPDATE dijelovi SET kom = %s, scan_count = %s, status = %s WHERE id = %s",
        (body.kom, scan_count, status, id)
    )
    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    return format_row_vrijeme(dict(updated))

@app.patch("/dijelovi/{id}/tijek", response_model=schemas.DioResponse)
def update_tijek(id: int, body: schemas.TijekUpdate, request: Request, conn=Depends(get_db)):
    require_admin(request)
    row = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Redak nije pronađen.")

    kom = max(int(row["kom"] or 1), 1)
    if body.scan_count < 0 or body.scan_count > kom:
        raise HTTPException(status_code=400, detail=f"TIJEK mora biti između 0 i {kom}.")

    status = "SAVIJENO" if body.scan_count > 0 else "IZREZANO"
    vrijeme = row["vrijeme"]
    if body.scan_count == 0:
        vrijeme = None
    elif not vrijeme:
        vrijeme = datetime.now(timezone.utc)

    conn.execute(
        "UPDATE dijelovi SET scan_count = %s, status = %s, vrijeme = %s WHERE id = %s",
        (body.scan_count, status, vrijeme, id)
    )
    updated = conn.execute("SELECT * FROM dijelovi WHERE id = %s", (id,)).fetchone()
    return format_row_vrijeme(dict(updated))

@app.get("/statistike")
def get_statistike(request: Request, tura: Optional[str] = None, conn=Depends(get_db)):
    require_auth(request)
    where = "WHERE tura = %s" if tura else ""
    params = (tura,) if tura else ()

    def count_sql(condition=""):
        full_where = f"{where} AND {condition}" if where and condition else (f"WHERE {condition}" if condition else where)
        res = conn.execute(f"SELECT COUNT(*) FROM dijelovi {full_where}", params).fetchone()
        return list(res.values())[0] if res else 0

    return {
        "ukupno": count_sql(),
        "savijeno": count_sql("status='SAVIJENO'"),
        "izrezano": count_sql("status='IZREZANO'"),
        "krivo_savijeno": count_sql("status='KRIVO SAVIJENO'"),
        "s_kolicima": count_sql("kolica IS NOT NULL"),
        "bez_kolica": count_sql("kolica IS NULL"),
    }

# ─── DB INIT & Import logic (Omitted some redundant logic for brevity, but kept structure) ────────────────
from import_tura import (
    create_grm_request, poll_grm_request, ingest_grm_request,
    get_grm_request_work_orders, wait_and_ingest_grm_request
)

def init_db():
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS korisnici (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL, role TEXT NOT NULL, display_name TEXT, active BOOLEAN NOT NULL DEFAULT TRUE)")
                cur.execute("CREATE TABLE IF NOT EXISTS dijelovi (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, tura TEXT NOT NULL, barcode TEXT NOT NULL, nalog TEXT, broj_rn TEXT, part TEXT, kom INTEGER NOT NULL DEFAULT 1, scan_count INTEGER NOT NULL DEFAULT 0, materijal TEXT, klasifikacija TEXT, debljina NUMERIC(6, 2), status TEXT NOT NULL DEFAULT 'IZREZANO', kolica INTEGER, tura_br TEXT, operater TEXT, vrijeme TIMESTAMPTZ)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_barcode ON dijelovi(barcode)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_tura ON dijelovi(tura)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_dijelovi_status ON dijelovi(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_dijelovi_lower_barcode ON dijelovi (lower(barcode))")
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")

@app.on_event("startup")
def startup_event():
    init_db()

# GRM Endpoints
@app.post("/grm/request")
def create_request(body: schemas.GrmRequestParams, request: Request):
    require_admin(request)
    return create_grm_request(date_from=body.from_date, date_to=body.to_date, admctr=body.admctr, request_id=body.request_id)

@app.get("/grm/request/{request_id}")
def get_request_status(request_id: str, request: Request):
    require_admin(request)
    tracked = get_tracking(request_id)
    if not tracked: raise HTTPException(status_code=404, detail="Request nije pronađen.")
    return tracked

@app.post("/grm/request/{request_id}/poll")
def poll_request(request_id: str, request: Request):
    require_admin(request)
    return poll_grm_request(request_id)

@app.post("/grm/request/{request_id}/ingest")
def ingest_request(request_id: str, request: Request):
    require_admin(request)
    return ingest_grm_request(request_id)

@app.get("/grm/request/{request_id}/work-orders")
def get_request_work_orders(request_id: str, request: Request):
    require_admin(request)
    return get_grm_request_work_orders(request_id)

@app.post("/grm/request/{request_id}/ingest-selection")
def ingest_request_selection(request_id: str, body: schemas.GrmSelectionIngest, request: Request):
    require_admin(request)
    return ingest_grm_request(request_id, selected_sifradn=body.sifradn)

@app.post("/grm/request/{request_id}/wait")
def wait_request(request_id: str, request: Request, timeout_seconds: int = 90):
    require_admin(request)
    return wait_and_ingest_grm_request(request_id, timeout_seconds=timeout_seconds)
