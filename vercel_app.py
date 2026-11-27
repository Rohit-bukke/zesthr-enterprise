import os
import shutil
from pathlib import Path
from django.core.wsgi import get_wsgi_application
from django.core.management import call_command

os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ.get("DJANGO_SETTINGS_MODULE", "payroll_project.settings.development"))

# In Vercel serverless environment, ensure SQLite db is seeded or migrated into /tmp
if os.environ.get("VERCEL"):
    tmp_db = Path("/tmp/db.sqlite3")
    base_dir = Path(__file__).resolve().parent
    local_db = base_dir / "db.sqlite3"

    # If an existing db.sqlite3 exists in repository, copy it to /tmp so data is present!
    if not tmp_db.exists():
        if local_db.exists():
            try:
                shutil.copy2(local_db, tmp_db)
                print("Copied existing db.sqlite3 to /tmp/db.sqlite3")
            except Exception as e:
                print(f"Error copying db.sqlite3: {e}")
        else:
            try:
                call_command("migrate", interactive=False)
                print("Ran migrations on /tmp/db.sqlite3")
            except Exception as e:
                print(f"Migration error: {e}")

app = get_wsgi_application()
