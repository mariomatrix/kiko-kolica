"""
import_tura.py - Uvoz Excel ture ili GRM response paketa u PostgreSQL bazu.

Koristenje:
    python import_tura.py put/do/fajla.xlsx "10. TURA"
    python import_tura.py put/do/fajla.xlsx --all
    python import_tura.py put/do/fajla.xlsx "10. TURA" --replace
    python import_tura.py --grm-response put/do/response_foldera

Zadano ponasanje za Excel import:
- nova tura se doda u bazu
- postojeca tura se preskace i nista se ne mijenja

Za GRM response import:
- request_id postaje tura
- completed i completed_with_warnings su valjani statusi
- duplicate (tura, barcode) redovi se preskacu uz warning
"""

import json
import psycopg
from psycopg.rows import dict_row
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import os

import pandas as pd
from db import get_db

TRACKING_PATH = Path(__file__).with_name("grm_request_state.json")
REQUEST_INBOX = Path(r"Z:\004_Konstrukcija\010_BI_File_Drop\REQUEST")
RESPONSE_ROOT = Path(r"Z:\004_Konstrukcija\010_BI_File_Drop\ALDO_POC\responses")
ERROR_ROOT = Path(r"Z:\004_Konstrukcija\010_BI_File_Drop\ALDO_POC\errors")
MODULE_ID = "shopfloor_parts_lifecycle_v1"
TARGET_DROP = "ALDO_POC"
CONTRACT_VERSION = "poc-v1"

COLUMN_MAP = {
    "Barcode potrebe": "barcode",
    "radni nalog": "nalog",
    "broj rn": "broj_rn",
    "part": "part",
    "kom.": "kom",
    "kom": "kom",
    "scan_count": "scan_count",
    "materijal": "materijal",
    "klasifikacija": "klasifikacija",
    "debljina": "debljina",
    "status": "status",
    "kolica br.": "kolica",
    "tura br": "tura_br",
    "operater": "operater",
    "vrijeme": "vrijeme",
}

VALID_STATUSES = {
    "IZREZANO",
    "SAVIJENO",
    "KRIVO SAVIJENO",
    "DODJELJENO",
    "NA CEKANJU",
    "ODBIJENO",
}

VALID_GRM_STATUSES = {"completed", "completed_with_warnings"}

REQUIRED_V_DN_COLUMNS = {
    "DNID",
    "SIFRADN",
}

REQUIRED_POTREBA_COLUMNS = {
    "potrid",
    "dnid",
    "ident",
    "kolicina",
    "status",
    "kolvhod",
    "opombe",
    "artikel_artid",
    "artikel_artikel",
    "artikel_naziv1",
    "artikel_naziv2",
    "artikel_admid",
    "artikel_barkoda",
    "artikel_em",
    "artklas_kljucevi",
}


def normalize_status(value):
    text = str(value).upper().strip()
    text = (
        text.replace("ÄŒ", "C")
        .replace("Ä†", "C")
        .replace("Å ", "S")
        .replace("Å½", "Z")
        .replace("Ä ", "D")
    )
    return text if text in VALID_STATUSES else "IZREZANO"


def prepare_dataframe(xlsx_path: str, sheet_name: str, tura_name: str):
    print(f"\nCitam: {xlsx_path} -> sheet: '{sheet_name}'")
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name)

    df = df.rename(columns={k: v for k, v in COLUMN_MAP.items() if k in df.columns})
    keep = [c for c in COLUMN_MAP.values() if c in df.columns]
    df = df[keep].copy()

    if "barcode" not in df.columns:
        raise ValueError("Sheet nema stupac 'Barcode potrebe'.")

    df["barcode"] = df["barcode"].astype(str).str.strip()
    df = df[df["barcode"].notna() & (df["barcode"] != "") & (df["barcode"] != "nan")]

    if "status" in df.columns:
        df["status"] = df["status"].apply(normalize_status)
    else:
        df["status"] = "IZREZANO"

    if "kolica" in df.columns:
        df["kolica"] = pd.to_numeric(df["kolica"], errors="coerce")
        df["kolica"] = df["kolica"].where(df["kolica"].notna(), None)
    else:
        df["kolica"] = None

    if "kom" in df.columns:
        df["kom"] = pd.to_numeric(df["kom"], errors="coerce").fillna(1).astype(int)
        df.loc[df["kom"] < 1, "kom"] = 1
    else:
        df["kom"] = 1

    if "scan_count" in df.columns:
        df["scan_count"] = pd.to_numeric(df["scan_count"], errors="coerce").fillna(0).astype(int)
        df.loc[df["scan_count"] < 0, "scan_count"] = 0
        df.loc[df["scan_count"] > df["kom"], "scan_count"] = df["kom"]
    else:
        df["scan_count"] = df.apply(
            lambda row: int(row["kom"]) if row["status"] == "SAVIJENO" else 0,
            axis=1,
        )

    if "vrijeme" in df.columns:
        df["vrijeme"] = (
            df["vrijeme"].astype(str).str.strip().replace("NaT", "").replace("nan", "")
        )
    else:
        df["vrijeme"] = ""

    df["tura"] = tura_name

    cols = [
        "tura",
        "barcode",
        "nalog",
        "broj_rn",
        "part",
        "kom",
        "scan_count",
        "materijal",
        "klasifikacija",
        "debljina",
        "status",
        "kolica",
        "tura_br",
        "operater",
        "vrijeme",
    ]
    for col in cols:
        if col not in df.columns:
            df[col] = None

    return df[cols]


def to_sql_postgres(df: pd.DataFrame, table_name: str):
    # Convert all NaN/NaT values to None so they get mapped to database NULL
    df_clean = df.where(pd.notnull(df), None)
    cols = df_clean.columns.tolist()
    query = f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    
    with get_db() as conn:
        with conn.cursor() as cur:
            records = [tuple(x) for x in df_clean.to_numpy()]
            cur.executemany(query, records)
        conn.commit()


def import_sheet(xlsx_path: str, sheet_name: str, tura_name: str, replace_existing: bool = False):
    df = prepare_dataframe(xlsx_path, sheet_name, tura_name)
    print(f"   Pronadeno {len(df)} redaka.")

    with get_db() as conn:
        ensure_broj_rn_column(conn)
        ensure_kom_column(conn)
        ensure_scan_count_column(conn)
        ensure_klasifikacija_column(conn)
        ensure_tura_br_column(conn)
        
        res = conn.execute(
            "SELECT COUNT(*) FROM dijelovi WHERE tura = %s",
            (tura_name,),
        ).fetchone()
        existing = list(res.values())[0] if res else 0

        if existing > 0 and not replace_existing:
            print(f"   Preskacem: tura '{tura_name}' vec postoji ({existing} redaka).")
            return False

        if existing > 0 and replace_existing:
            conn.execute("DELETE FROM dijelovi WHERE tura = %s", (tura_name,))
            conn.commit()
            print(f"   Obrisano {existing} starih redaka za turu '{tura_name}'.")

    # Ingest rows using executemany helper
    to_sql_postgres(df, "dijelovi")
    print(f"   OK: uvezeno {len(df)} redaka za turu '{tura_name}'.")
    return True


def list_sheets(xlsx_path: str):
    xl = pd.ExcelFile(xlsx_path)
    print(f"\nSheets u fajlu '{xlsx_path}':")
    for i, name in enumerate(xl.sheet_names):
        print(f"  [{i}] {name}")
    return xl.sheet_names


def import_all_sheets(xlsx_path: str, replace_existing: bool = False):
    sheets = list_sheets(xlsx_path)
    imported = 0
    skipped = 0

    for sheet_name in sheets:
        changed = import_sheet(
            xlsx_path=xlsx_path,
            sheet_name=sheet_name,
            tura_name=sheet_name,
            replace_existing=replace_existing,
        )
        if changed:
            imported += 1
        else:
            skipped += 1

    print(f"\nSazetak: uvezeno {imported}, preskoceno {skipped}.")


def fail(message: str):
    raise ValueError(message)


def ensure_column_exists(conn, column_name, col_type):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = 'dijelovi' AND column_name = %s
            """,
            (column_name,),
        )
        res = cur.fetchone()
        if not res:
            cur.execute(f"ALTER TABLE dijelovi ADD COLUMN {column_name} {col_type}")
            conn.commit()


def ensure_broj_rn_column(conn):
    ensure_column_exists(conn, "broj_rn", "TEXT")


def ensure_kom_column(conn):
    ensure_column_exists(conn, "kom", "INTEGER DEFAULT 1")
    with conn.cursor() as cur:
        cur.execute("UPDATE dijelovi SET kom = 1 WHERE kom IS NULL OR kom < 1")
        conn.commit()


def ensure_scan_count_column(conn):
    ensure_column_exists(conn, "scan_count", "INTEGER DEFAULT 0")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dijelovi
            SET scan_count = CASE
                WHEN (scan_count IS NULL OR scan_count = 0) AND status = 'SAVIJENO' THEN kom
                WHEN scan_count IS NULL THEN 0
                WHEN scan_count < 0 THEN 0
                WHEN scan_count > kom THEN kom
                ELSE scan_count
            END
            """
        )
        conn.commit()


def ensure_klasifikacija_column(conn):
    ensure_column_exists(conn, "klasifikacija", "TEXT")


def ensure_tura_br_column(conn):
    ensure_column_exists(conn, "tura_br", "TEXT")


def material_from_child(row):
    for key in ("artikel_artikel", "artikel_naziv1", "ident"):
        value = str(row.get(key, "")).strip()
        if value and value.lower() != "nan":
            return value
    return ""


def read_csv(csv_path: Path):
    return pd.read_csv(csv_path, dtype=str, keep_default_na=False)


def load_grm_response_package(response_dir: str):
    response_path = Path(response_dir)
    if not response_path.exists():
        fail(f"Response folder ne postoji: {response_dir}")

    manifest_files = sorted(response_path.glob("*.manifest.json"))
    if not manifest_files:
        fail("Response folder nema manifest.json datoteku.")
    if len(manifest_files) > 1:
        fail("Response folder ima vise manifest.json datoteka.")

    manifest_path = manifest_files[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status = validate_manifest(manifest)

    request_id = str(manifest["request_id"]).strip()
    files = manifest["files"]
    v_dn_path = response_path / files["v_dn"]
    potreba_path = response_path / files["potreba"]
    if not v_dn_path.exists():
        fail(f"Nedostaje v_dn.csv: {v_dn_path.name}")
    if not potreba_path.exists():
        fail(f"Nedostaje potreba.csv: {potreba_path.name}")

    v_dn_df = read_csv(v_dn_path)
    potreba_df = read_csv(potreba_path)

    validate_columns(v_dn_df, REQUIRED_V_DN_COLUMNS, "v_dn.csv")
    validate_columns(potreba_df, REQUIRED_POTREBA_COLUMNS, "potreba.csv")
    validate_uniques(v_dn_df, "DNID", "v_dn.csv")
    validate_uniques(potreba_df, "potrid", "potreba.csv")

    counts = manifest["counts"]
    expected_v_dn = counts.get("v_dn_rows")
    expected_potreba = counts.get("potreba_rows")
    if expected_v_dn is not None and int(expected_v_dn) != len(v_dn_df):
        fail(f"Manifest count za v_dn ({expected_v_dn}) ne odgovara CSV-u ({len(v_dn_df)}).")
    if expected_potreba is not None and int(expected_potreba) != len(potreba_df):
        fail(f"Manifest count za potreba ({expected_potreba}) ne odgovara CSV-u ({len(potreba_df)}).")

    return {
        "request_id": request_id,
        "status": status,
        "manifest": manifest,
        "v_dn_df": v_dn_df,
        "potreba_df": potreba_df,
        "response_path": response_path,
    }


def validate_manifest(manifest: dict):
    required = {"request_id", "module_id", "contract_version", "status", "files", "counts"}
    missing = sorted(required - set(manifest))
    if missing:
        fail(f"Manifestu nedostaju polja: {', '.join(missing)}")

    status = str(manifest["status"]).strip()
    if status not in VALID_GRM_STATUSES:
        fail(f"Manifest status '{status}' nije ingestibilan.")

    files = manifest["files"]
    if not isinstance(files, dict) or "v_dn" not in files or "potreba" not in files:
        fail("Manifest.files mora sadrzavati 'v_dn' i 'potreba'.")

    counts = manifest["counts"]
    if not isinstance(counts, dict):
        fail("Manifest.counts mora biti objekt.")

    return status


def validate_columns(df: pd.DataFrame, required_columns: set, label: str):
    missing = sorted(required_columns - set(df.columns))
    if missing:
        fail(f"{label} nema obavezne kolone: {', '.join(missing)}")


def validate_uniques(df: pd.DataFrame, column: str, label: str):
    blanks = df[column].astype(str).str.strip() == ""
    if blanks.any():
        fail(f"{label} sadrzi prazne vrijednosti u koloni '{column}'.")

    if df[column].duplicated().any():
        sample = df.loc[df[column].duplicated(), column].astype(str).iloc[0]
        fail(f"{label} sadrzi duplikat kljuca '{column}': {sample}")


def log_sifradn_conflicts(v_dn_df: pd.DataFrame):
    grouped = v_dn_df.groupby("SIFRADN")["DNID"].nunique()
    conflicts = grouped[grouped > 1]
    if not conflicts.empty:
        for sifradn, cnt in conflicts.items():
            print(f"   WARN: SIFRADN '{sifradn}' mapira na {cnt} DNID vrijednosti.")


def build_grm_rows(manifest: dict, v_dn_df: pd.DataFrame, potreba_df: pd.DataFrame):
    request_id = str(manifest["request_id"]).strip()
    parent_map = {
        str(row["DNID"]).strip(): row
        for _, row in v_dn_df.iterrows()
    }

    rows = []
    for _, row in potreba_df.iterrows():
        dnid = str(row["dnid"]).strip()
        potrid = str(row["potrid"]).strip()
        parent = parent_map.get(dnid)
        if parent is None:
            fail(f"Child POTRID {potrid} nema parent DNID {dnid}.")

        rows.append(
            {
                "tura": request_id,
                "barcode": f"R{potrid}",
                "nalog": f"N{dnid}",
                "broj_rn": str(parent.get("SIFRADN", "")).strip(),
                "part": str(row.get("opombe", "")).strip(),
                "kom": 1,
                "scan_count": 0,
                "materijal": material_from_child(row),
                "klasifikacija": str(row.get("artklas_kljucevi", "")).strip(),
                "debljina": None,
                "status": "IZREZANO",
                "kolica": None,
                "tura_br": str(parent.get("OPOMBE", "")).strip(),
                "operater": None,
                "vrijeme": None,
            }
        )
    return pd.DataFrame(rows)


def build_work_order_options(v_dn_df: pd.DataFrame, potreba_df: pd.DataFrame):
    child_counts = (
        potreba_df.groupby("dnid")["potrid"]
        .count()
        .to_dict()
    )
    options = []
    for _, row in v_dn_df.iterrows():
        dnid = str(row["DNID"]).strip()
        sifradn = str(row.get("SIFRADN", "")).strip()
        if not sifradn:
            continue
        options.append(
            {
                "sifradn": sifradn,
                "nalog": f"N{dnid}",
                "dnid": dnid,
                "tura_br": str(row.get("OPOMBE", "")).strip(),
                "child_count": int(child_counts.get(dnid, 0)),
            }
        )
    options.sort(key=lambda item: (item["sifradn"], item["nalog"]))
    return options


def import_grm_response(response_dir: str, selected_sifradn: list[str] | None = None):
    package = load_grm_response_package(response_dir)
    manifest = package["manifest"]
    request_id = package["request_id"]
    status = package["status"]
    v_dn_df = package["v_dn_df"]
    potreba_df = package["potreba_df"]

    print(f"\nGRM import: {request_id}")
    print(f"   Manifest status: {status}")

    log_sifradn_conflicts(v_dn_df)

    warnings = manifest.get("warnings", [])
    if warnings:
        for warning in warnings:
            code = warning.get("code", "UNKNOWN")
            message = warning.get("message", "")
            print(f"   WARN: {code}: {message}")

    selected_set = None
    if selected_sifradn is not None:
        selected_set = {str(item).strip() for item in selected_sifradn if str(item).strip()}
        if not selected_set:
            fail("Odabir radnih naloga je prazan.")
        v_dn_df = v_dn_df[v_dn_df["SIFRADN"].astype(str).str.strip().isin(selected_set)].copy()
        if v_dn_df.empty:
            fail("Nijedan odabrani SIFRADN nije pronaden u response paketu.")
        allowed_dnid = set(v_dn_df["DNID"].astype(str).str.strip())
        potreba_df = potreba_df[potreba_df["dnid"].astype(str).str.strip().isin(allowed_dnid)].copy()
        print(f"   Uzimam samo odabrane naloge: {len(v_dn_df)} parent / {len(potreba_df)} child redaka.")

    df = build_grm_rows(manifest, v_dn_df, potreba_df)

    with get_db() as conn:
        ensure_broj_rn_column(conn)
        ensure_kom_column(conn)
        ensure_scan_count_column(conn)
        ensure_klasifikacija_column(conn)
        ensure_tura_br_column(conn)
        
        res = conn.execute(
            "SELECT COUNT(*) FROM dijelovi WHERE tura = %s",
            (request_id,),
        ).fetchone()
        existing_batch = list(res.values())[0] if res else 0
        
        if existing_batch > 0:
            print(f"   SKIP: request_id '{request_id}' je vec ingestiran ({existing_batch} redaka).")
            return {
                "request_id": request_id,
                "inserted": 0,
                "skipped_existing_batch": True,
                "skipped_rows": existing_batch,
            }

        rows = conn.execute("SELECT tura, barcode FROM dijelovi").fetchall()
        existing_pairs = {(str(r["tura"]), str(r["barcode"])) for r in rows}

    seen_pairs = set()
    filtered_rows = []
    skipped_duplicates = 0
    for row in df.to_dict(orient="records"):
        pair = (row["tura"], row["barcode"])
        if pair in existing_pairs or pair in seen_pairs:
            skipped_duplicates += 1
            print(f"   WARN: duplicate ({row['tura']}, {row['barcode']}) - safe skip.")
            continue
        seen_pairs.add(pair)
        filtered_rows.append(row)

    filtered_df = pd.DataFrame(filtered_rows, columns=df.columns)
    if filtered_df.empty:
        print("   SKIP: nema novih redaka za ingest.")
        return {
            "request_id": request_id,
            "inserted": 0,
            "skipped_existing_batch": False,
            "skipped_rows": skipped_duplicates,
        }

    # Ingest rows using our executemany helper
    to_sql_postgres(filtered_df, "dijelovi")

    print(f"   OK: ingestirano {len(filtered_df)} redaka za turu '{request_id}'.")
    if skipped_duplicates:
        print(f"   WARN: preskoceno duplih redaka: {skipped_duplicates}.")

    return {
        "request_id": request_id,
        "inserted": len(filtered_df),
        "skipped_existing_batch": False,
        "skipped_rows": skipped_duplicates,
        "selected_sifradn_count": len(selected_set) if selected_set is not None else None,
        "parent_rows": len(v_dn_df),
    }


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_tracking_state():
    if not TRACKING_PATH.exists():
        return {"requests": {}}
    return json.loads(TRACKING_PATH.read_text(encoding="utf-8"))


def save_tracking_state(state: dict):
    TRACKING_PATH.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def update_tracking(request_id: str, status: str, **extra):
    state = load_tracking_state()
    requests = state.setdefault("requests", {})
    current = requests.get(request_id, {})
    current.update(extra)
    current["request_id"] = request_id
    current["status"] = status
    current["updated_at"] = iso_now()
    requests[request_id] = current
    save_tracking_state(state)
    return current


def get_tracking(request_id: str):
    return load_tracking_state().get("requests", {}).get(request_id)


def make_request_id(prefix: str = "aldo-poc"):
    return f"{prefix}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"


def build_request_payload(date_from: str, date_to: str, admctr: str, request_id: str | None = None):
    if not request_id:
        request_id = make_request_id()
    return {
        "request_id": request_id,
        "module_id": MODULE_ID,
        "target_drop": TARGET_DROP,
        "contract_version": CONTRACT_VERSION,
        "requested_at": iso_now(),
        "fetch_mode": "date_window",
        "params": {
            "from": date_from,
            "to": date_to,
            "admctr": admctr,
        },
    }


def create_grm_request(date_from: str, date_to: str, admctr: str, request_id: str | None = None):
    REQUEST_INBOX.mkdir(parents=True, exist_ok=True)
    payload = build_request_payload(date_from, date_to, admctr, request_id=request_id)
    request_id = payload["request_id"]

    existing = get_tracking(request_id)
    if existing and existing.get("status") in {"created", "awaiting_response", "ready_to_ingest", "ingested"}:
        fail(f"Request '{request_id}' vec postoji u local trackingu.")

    file_path = REQUEST_INBOX / f"{request_id}.request.json"
    if file_path.exists():
        fail(f"Request file vec postoji: {file_path.name}")

    update_tracking(
        request_id,
        "created",
        request_file=str(file_path),
        response_dir=str(RESPONSE_ROOT / request_id),
        error_file=str(ERROR_ROOT / f"{request_id}.error.json"),
        payload=payload,
    )
    file_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    update_tracking(request_id, "awaiting_response")
    return payload


def get_response_dir(request_id: str):
    return RESPONSE_ROOT / request_id


def get_response_manifest_path(request_id: str):
    return get_response_dir(request_id) / f"{request_id}.manifest.json"


def get_error_manifest_path(request_id: str):
    return ERROR_ROOT / f"{request_id}.error.json"


def poll_grm_request(request_id: str):
    tracked = get_tracking(request_id)
    if not tracked:
        fail(f"Request '{request_id}' nije pronaden u local trackingu.")

    manifest_path = get_response_manifest_path(request_id)
    error_path = get_error_manifest_path(request_id)

    if error_path.exists():
        update_tracking(request_id, "failed", error_file=str(error_path))
        return {
            "request_id": request_id,
            "status": "failed",
            "ready": False,
            "reason": "error_file_present",
            "error_file": str(error_path),
        }

    if not manifest_path.exists():
        update_tracking(request_id, "awaiting_response")
        return {
            "request_id": request_id,
            "status": "awaiting_response",
            "ready": False,
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("request_id", "")).strip() != request_id:
        fail(f"Manifest request_id ne odgovara trazenom requestu '{request_id}'.")
    if str(manifest.get("contract_version", "")).strip() != CONTRACT_VERSION:
        fail(f"Manifest contract_version nije '{CONTRACT_VERSION}'.")
    if "files" not in manifest or "v_dn" not in manifest["files"] or "potreba" not in manifest["files"]:
        fail("Manifest nema potrebne file reference za v_dn i potreba.")

    manifest_status = str(manifest.get("status", "")).strip()
    if manifest_status == "failed":
        update_tracking(request_id, "failed", response_dir=str(get_response_dir(request_id)))
        return {
            "request_id": request_id,
            "status": "failed",
            "ready": True,
            "manifest_status": manifest_status,
        }
    if manifest_status not in VALID_GRM_STATUSES:
        fail(f"Manifest status '{manifest_status}' nije podrzan za pickup.")

    update_tracking(request_id, "ready_to_ingest", response_dir=str(get_response_dir(request_id)))
    return {
        "request_id": request_id,
        "status": "ready_to_ingest",
        "ready": True,
        "manifest_status": manifest_status,
        "response_dir": str(get_response_dir(request_id)),
    }


def get_grm_request_work_orders(request_id: str):
    poll = poll_grm_request(request_id)
    if poll["status"] == "failed":
        return poll
    if not poll["ready"]:
        return poll

    package = load_grm_response_package(poll["response_dir"])
    options = build_work_order_options(package["v_dn_df"], package["potreba_df"])
    return {
        "request_id": request_id,
        "status": "ready_to_select",
        "ready": True,
        "manifest_status": package["status"],
        "total_work_orders": len(options),
        "work_orders": options,
    }


def ingest_grm_request(request_id: str, selected_sifradn: list[str] | None = None):
    poll = poll_grm_request(request_id)
    if poll["status"] == "failed":
        return poll
    if not poll["ready"]:
        return poll

    result = import_grm_response(poll["response_dir"], selected_sifradn=selected_sifradn)
    update_tracking(
        request_id,
        "ingested",
        inserted=result.get("inserted", 0),
        skipped_rows=result.get("skipped_rows", 0),
        skipped_existing_batch=result.get("skipped_existing_batch", False),
        response_dir=poll["response_dir"],
        selected_sifradn=selected_sifradn or [],
    )
    result["status"] = "ingested"
    return result


def wait_and_ingest_grm_request(request_id: str, poll_seconds: int = 5, timeout_seconds: int = 90):
    started = time.time()
    while True:
        poll = poll_grm_request(request_id)
        if poll["status"] == "failed":
            return poll
        if poll["ready"]:
            return ingest_grm_request(request_id)
        if time.time() - started >= timeout_seconds:
            return {
                "request_id": request_id,
                "status": "awaiting_response",
                "ready": False,
                "timed_out": True,
            }
        time.sleep(poll_seconds)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "--grm-response":
        if len(sys.argv) < 3:
            print("Nedostaje path do GRM response foldera.")
            sys.exit(1)
        import_grm_response(sys.argv[2])
        return

    if sys.argv[1] == "--grm-request":
        if len(sys.argv) < 5:
            print("Koristenje: python import_tura.py --grm-request FROM TO ADMCTR [REQUEST_ID]")
            sys.exit(1)
        payload = create_grm_request(
            date_from=sys.argv[2],
            date_to=sys.argv[3],
            admctr=sys.argv[4],
            request_id=sys.argv[5] if len(sys.argv) >= 6 else None,
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    if sys.argv[1] == "--grm-poll":
        if len(sys.argv) < 3:
            print("Koristenje: python import_tura.py --grm-poll REQUEST_ID")
            sys.exit(1)
        result = poll_grm_request(sys.argv[2])
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return

    if sys.argv[1] == "--grm-wait":
        if len(sys.argv) < 3:
            print("Koristenje: python import_tura.py --grm-wait REQUEST_ID [TIMEOUT_SECONDS]")
            sys.exit(1)
        timeout = int(sys.argv[3]) if len(sys.argv) >= 4 else 90
        result = wait_and_ingest_grm_request(sys.argv[2], timeout_seconds=timeout)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return

    xlsx_path = sys.argv[1]
    args = sys.argv[2:]

    replace_existing = "--replace" in args
    import_all = "--all" in args
    args = [arg for arg in args if arg not in {"--replace", "--all"}]

    if import_all:
        import_all_sheets(xlsx_path, replace_existing=replace_existing)
        return

    sheets = list_sheets(xlsx_path)

    if args:
        sheet_input = args[0]
        tura_name = args[1] if len(args) >= 2 else sheet_input
        import_sheet(xlsx_path, sheet_input, tura_name, replace_existing=replace_existing)
        return

    print()
    choice = input("Koji sheet uvesti? (broj ili naziv): ").strip()
    try:
        sheet_name = sheets[int(choice)]
    except (ValueError, IndexError):
        sheet_name = choice

    tura_name = input(f"Naziv ture za bazu (Enter = '{sheet_name}'): ").strip()
    if not tura_name:
        tura_name = sheet_name

    import_sheet(xlsx_path, sheet_name, tura_name, replace_existing=replace_existing)


if __name__ == "__main__":
    main()
