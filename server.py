import hmac
import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from config import settings
from tools import register_sql_tools, register_storage_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health" or not settings.mcp_api_key:
            return await call_next(request)
        key = (
            request.headers.get("x-api-key")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or request.query_params.get("api_key")
            or request.query_params.get("code")
            or ""
        )
        if not hmac.compare_digest(key, settings.mcp_api_key):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app):
    logger.info("starting MCP server (auth_mode=%s)", settings.auth_mode)
    yield
    logger.info("shutting down")


mcp = FastMCP("gowri-mcp", lifespan=lifespan)

register_sql_tools(mcp, settings)
register_storage_tools(mcp, settings)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.tool()
def status() -> str:
    """Return the server's runtime configuration summary (no secret values).

    Use this to confirm the server is connected to the right SQL server and storage account.
    """
    return (
        f"auth_mode={settings.auth_mode} "
        f"sql_server={settings.sql_server or '(not set)'} "
        f"sql_database={settings.sql_database or '(not set)'} "
        f"storage_account={settings.storage_account_name or '(not set)'}"
    )


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=settings.port)
