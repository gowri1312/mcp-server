from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 8000
    auth_mode: str = "apikey"   # "apikey" | "none"

    # API key auth
    mcp_api_key: str = ""

    # User-assigned managed identity client ID (for SQL access)
    managed_identity_client_id: str = ""

    # Azure SQL
    sql_server: str = ""        # e.g. my-server.database.windows.net
    sql_database: str = ""

    # Azure Storage — separate subscription, accessed via account-level SAS token
    storage_account_name: str = ""
    storage_sas_token: str = ""   # leading "?" optional


settings = Settings()
