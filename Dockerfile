FROM python:3.12-slim

# Postavi radni direktorij
WORKDIR /app

# Kopiraj requirements i instaliraj zavisnosti
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Kopiraj ostatak aplikacije
COPY backend /app/backend
COPY frontend /app/frontend

# Postavi radni direktorij na backend za pokretanje uvicorna
WORKDIR /app/backend

# Eksponiraj port
EXPOSE 8000

# Pokretanje aplikacije
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
