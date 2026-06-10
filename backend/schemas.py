from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime

class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    username: str
    role: str
    display_name: str

class StatusUpdate(BaseModel):
    status: str
    kolica: Optional[int] = None

class KolicaUpdate(BaseModel):
    kolica: int

class KomUpdate(BaseModel):
    kom: int

class TijekUpdate(BaseModel):
    scan_count: int

class AdminKolicaUpdate(BaseModel):
    kolica: Optional[int] = None

class GrmRequestParams(BaseModel):
    from_date: str
    to_date: str
    admctr: str
    request_id: Optional[str] = None

class GrmSelectionIngest(BaseModel):
    sifradn: List[str]

class KomImportApply(BaseModel):
    token: str

class DioResponse(BaseModel):
    id: int
    tura: str
    barcode: str
    nalog: Optional[str] = None
    broj_rn: Optional[str] = None
    part: Optional[str] = None
    kom: int
    scan_count: int
    materijal: Optional[str] = None
    klasifikacija: Optional[str] = None
    debljina: Optional[float] = None
    status: str
    kolica: Optional[int] = None
    tura_br: Optional[str] = None
    operater: Optional[str] = None
    vrijeme: Optional[str] = None # We format it as string in main.py

    model_config = ConfigDict(from_attributes=True)

class ScanResponse(BaseModel):
    found: bool
    has_izrezano: bool
    remaining_scans: Optional[int] = None
    total: Optional[int] = None
    statusi: Optional[List[str]] = None
    completed_progress: Optional[str] = None
    row: DioResponse
