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

## Database Tools & Features

- **Remove Demo Data**: Run `python clean_demo_data.py` (or `python clean_demo_data.py '<postgresql://...>'`) to remove demo residents and rooms from PostgreSQL.
- **Mark Residents as Left (Vacate Rooms)**: Mark previous members who have left the boarding via the **Mark as Left** button. Their room bed is immediately vacated for new members, future monthly fee generations are stopped, and their payment/receipt history is safely preserved.
- **Safe Resident Deletion**: Permanently delete residents via the **Delete** button on the Residents page or Edit Resident page without PostgreSQL foreign-key constraint errors.
- **WhatsApp Payment Reminders**: On the **Reminders** page, click **Send WhatsApp Notice** beside any resident to open WhatsApp with a pre-filled reminder message.
