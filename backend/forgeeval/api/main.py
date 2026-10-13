from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from forgeeval.auth import AuthContext, authenticate
from forgeeval.db.session import get_session

app = FastAPI(title="ForgeEval API", version="0.1.0")

DbSession = Annotated[Session, Depends(get_session)]


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz(db: DbSession, response: Response) -> dict[str, str]:
    """Readiness: the database answers. The load balancer routes traffic only when this passes."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "down"}
    return {"status": "ok", "database": "up"}


class WhoAmI(BaseModel):
    project_id: str
    key_id: str
    scopes: list[str]


@app.get("/v1/whoami", tags=["auth"])
def whoami(ctx: Annotated[AuthContext, Depends(authenticate)]) -> WhoAmI:
    """Echo the project and scopes the presented API key resolves to."""
    return WhoAmI(project_id=str(ctx.project_id), key_id=str(ctx.key_id), scopes=sorted(ctx.scopes))
