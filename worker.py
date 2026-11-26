import os
from workers import WorkerEntrypoint, wsgi

from app import app, init_db

_db_initialized = False


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        global _db_initialized

        db_url = getattr(self.env, "DATABASE_URL", None)
        if db_url:
            os.environ["DATABASE_URL"] = db_url
            app.config["DATABASE_URL"] = db_url

        secret_key = getattr(self.env, "SECRET_KEY", None)
        if secret_key:
            os.environ["SECRET_KEY"] = secret_key
            app.config["SECRET_KEY"] = secret_key

        if not _db_initialized and app.config.get("DATABASE_URL"):
            with app.app_context():
                init_db()
            _db_initialized = True

        return await wsgi.fetch(app, request, self.env)
