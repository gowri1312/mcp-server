import json
import logging
import struct
from concurrent.futures import ThreadPoolExecutor

import pyodbc
from azure.identity import ManagedIdentityCredential

logger = logging.getLogger("mcp.sql")

_executor = ThreadPoolExecutor(max_workers=4)

# Cached credential — safe to share across threads
_credential: ManagedIdentityCredential | None = None


def _get_credential(client_id: str) -> ManagedIdentityCredential:
    global _credential
    if _credential is None:
        _credential = ManagedIdentityCredential(client_id=client_id)
    return _credential


def _connect(server: str, database: str, client_id: str) -> pyodbc.Connection:
    """Open a new pyodbc connection authenticated via managed identity."""
    cred = _get_credential(client_id)
    token = cred.get_token("https://database.windows.net/.default")
    token_bytes = token.token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, attrs_before={1256: token_struct})


def register_sql_tools(mcp, settings):
    @mcp.tool()
    def list_tables() -> str:
        """List all user tables in the configured Azure SQL database.

        Returns a JSON array of {schema, table} objects.
        """
        try:
            def _run():
                with _connect(settings.sql_server, settings.sql_database, settings.managed_identity_client_id) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT TABLE_SCHEMA, TABLE_NAME "
                        "FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_TYPE = 'BASE TABLE' "
                        "ORDER BY TABLE_SCHEMA, TABLE_NAME"
                    )
                    return [{"schema": r[0], "table": r[1]} for r in cursor.fetchall()]

            from asyncio import get_event_loop
            loop = get_event_loop()
            rows = loop.run_in_executor(_executor, _run)
            import asyncio
            rows = asyncio.get_event_loop().run_until_complete(rows) if not asyncio.get_event_loop().is_running() else rows
            # run synchronously inside the executor call
            rows = _run()
            return json.dumps(rows, indent=2)
        except Exception as e:
            logger.exception("list_tables failed")
            return f"Error: {e}"

    @mcp.tool()
    def query_sql(sql: str, params: list[str] | None = None) -> str:
        """Execute a read-only SELECT query against the Azure SQL database.

        Args:
            sql: A SELECT statement. INSERT/UPDATE/DELETE/DDL are rejected.
            params: Optional list of positional parameter values for '?' placeholders.

        Returns a JSON array of row objects (column name → value), max 200 rows.
        """
        if not sql.strip().upper().startswith("SELECT"):
            return "Error: only SELECT statements are allowed."
        try:
            def _run():
                with _connect(settings.sql_server, settings.sql_database, settings.managed_identity_client_id) as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, params or [])
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchmany(200)
                    return [dict(zip(columns, row)) for row in rows]

            rows = _run()
            return json.dumps(rows, indent=2, default=str)
        except Exception as e:
            logger.exception("query_sql failed")
            return f"Error: {e}"

    @mcp.tool()
    def describe_table(schema: str, table: str) -> str:
        """Return column names, types, and nullability for a table in the Azure SQL database.

        Args:
            schema: Table schema name (e.g. 'dbo').
            table: Table name.
        """
        try:
            def _run():
                with _connect(settings.sql_server, settings.sql_database, settings.managed_identity_client_id) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH "
                        "FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                        "ORDER BY ORDINAL_POSITION",
                        [schema, table],
                    )
                    cols = [col[0] for col in cursor.description]
                    return [dict(zip(cols, row)) for row in cursor.fetchall()]

            return json.dumps(_run(), indent=2, default=str)
        except Exception as e:
            logger.exception("describe_table failed")
            return f"Error: {e}"
