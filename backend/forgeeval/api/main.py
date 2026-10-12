from typing import Annotated, Any

from fastapi import Depends, FastAPI, Response, status
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from forgeeval.api import compare, registry, runs, traces
from forgeeval.auth import AuthContext, authenticate
from forgeeval.db.session import get_session

app = FastAPI(title="ForgeEval API", version="0.1.0")
app.include_router(traces.router)
app.include_router(registry.router)
app.include_router(runs.router)
app.include_router(compare.router)

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


def custom_openapi() -> dict[str, Any]:
    """Add the per-item ingest schema, which the batch body references but FastAPI can't see."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    item = traces.TraceIn.model_json_schema(ref_template="#/components/schemas/{model}")
    components.update(item.pop("$defs", {}))
    components["TraceIn"] = item
    schema["components"]["securitySchemes"] = {
        "ApiKey": {"type": "http", "scheme": "bearer", "description": "fe_<prefix>_<secret>"}
    }
    schema["security"] = [{"ApiKey": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
