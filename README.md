# Kolica Scan API

Aplikacija za praćenje i skeniranje dijelova u proizvodnji.

## Lokalno pokretanje (bez Coolify/Dockera)

Za pokretanje aplikacije na lokalnom računalu, slijedite ove korake:

### 1. Preduvjeti
- Instaliran **Python 3.10+**
- Pristup **PostgreSQL** bazi podataka

### 2. Instalacija zavisnosti
Preporučuje se korištenje virtualnog okruženja:
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r backend/requirements.txt
```

### 3. Konfiguracija
Kopirajte `.env.example` u `.env` i prilagodite postavke:
```bash
cp backend/.env.example backend/.env
```
Uredite `backend/.env` i postavite vašu `DATABASE_URL`.

### 4. Pokretanje aplikacije
Pokrenite Uvicorn poslužitelj iz `backend` direktorija:
```bash
cd backend
python -m uvicorn main:app --reload
```
Aplikacija će biti dostupna na: `http://127.0.0.1:8000`
Swagger dokumentacija: `http://127.0.0.1:8000/docs`

## Pokretanje putem Dockera
Ako želite testirati u okruženju sličnom produkciji:
```bash
docker build -t kolica-app .
docker run -p 8000:8000 --env-file backend/.env kolica-app
```

## Struktura projekta
- `backend/`: FastAPI aplikacija i skripte za uvoz podataka.
- `frontend/`: Statičke datoteke (HTML/JS).
- `Dockerfile`: Konfiguracija za kontejnerizaciju.
