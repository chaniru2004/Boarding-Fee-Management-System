# Boarding Fee Management System

Flask boarding fee management app configured for Neon PostgreSQL on Vercel.

## Vercel setup

Add this environment variable in the Vercel project:

```text
DATABASE_URL
```

Use the Neon PostgreSQL connection string supplied by Vercel Storage. The app creates its tables automatically on first startup.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="your-neon-postgres-url"
python app.py
```

