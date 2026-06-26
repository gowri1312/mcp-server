import json
import logging

from azure.storage.blob import BlobServiceClient

logger = logging.getLogger("mcp.storage")

_clients: dict[str, BlobServiceClient] = {}


def _get_client(account_name: str, sas_token: str) -> BlobServiceClient:
    if account_name not in _clients:
        token = sas_token if sas_token.startswith("?") else f"?{sas_token}"
        _clients[account_name] = BlobServiceClient(
            f"https://{account_name}.blob.core.windows.net{token}"
        )
    return _clients[account_name]


def register_storage_tools(mcp, settings):
    @mcp.tool()
    def list_containers() -> str:
        """List all blob containers in the configured Azure Storage account.

        Returns a JSON array of container names.
        """
        try:
            client = _get_client(settings.storage_account_name, settings.storage_sas_token)
            containers = [c["name"] for c in client.list_containers()]
            return json.dumps(containers, indent=2)
        except Exception as e:
            logger.exception("list_containers failed")
            return f"Error: {e}"

    @mcp.tool()
    def list_blobs(container: str, prefix: str = "", max_results: int = 50) -> str:
        """List blobs in a container, optionally filtered by prefix.

        Args:
            container: Container name.
            prefix: Optional path prefix to filter results (e.g. 'reports/2024/').
            max_results: Maximum number of blobs to return, 1-200.
        """
        max_results = max(1, min(max_results, 200))
        try:
            client = _get_client(settings.storage_account_name, settings.storage_sas_token)
            blobs = client.get_container_client(container).list_blobs(name_starts_with=prefix or None)
            result = []
            for i, b in enumerate(blobs):
                if i >= max_results:
                    break
                result.append({
                    "name": b["name"],
                    "size_bytes": b["size"],
                    "last_modified": b["last_modified"].isoformat() if b.get("last_modified") else None,
                    "content_type": b.get("content_settings", {}).get("content_type"),
                })
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.exception("list_blobs failed")
            return f"Error: {e}"

    @mcp.tool()
    def read_blob(container: str, blob_name: str, max_bytes: int = 65536) -> str:
        """Read the text content of a blob from Azure Storage.

        Best for plain-text, JSON, CSV, or Markdown files. Binary files will be unreadable.

        Args:
            container: Container name.
            blob_name: Full blob path/name.
            max_bytes: Maximum bytes to read (default 64 KB, max 512 KB).
        """
        max_bytes = max(1, min(max_bytes, 524288))
        try:
            client = _get_client(settings.storage_account_name, settings.storage_sas_token)
            blob_client = client.get_blob_client(container=container, blob=blob_name)
            data = blob_client.download_blob(max_concurrency=1).readall()
            if len(data) > max_bytes:
                data = data[:max_bytes]
                suffix = f"\n\n[truncated at {max_bytes} bytes]"
            else:
                suffix = ""
            return data.decode("utf-8", errors="replace") + suffix
        except Exception as e:
            logger.exception("read_blob failed")
            return f"Error: {e}"
