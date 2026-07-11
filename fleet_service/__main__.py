from __future__ import annotations

import uvicorn

from fleet_service.app import app
from fleet_service.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
