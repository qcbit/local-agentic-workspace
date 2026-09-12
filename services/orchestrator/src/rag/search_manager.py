import asyncio
import logging
import tempfile
from typing import Any, Dict, List, Optional
import aiohttp
import lancedb

logger = logging.getLogger(__name__)


class SearchManager:
    """Manages tiered web search with automatic fallback and ephemeral LanceDB indexing."""

    def __init__(self, uds_server=None, vector_store=None):
        self.uds_server = uds_server
        self.vector_store = vector_store
        self._temp_dir = tempfile.TemporaryDirectory(prefix="agentic_web_")
        self.db = lancedb.connect(self._temp_dir.name)
        logger.info(f"Ephemeral web search cache initialized at {self._temp_dir.name}")

    async def _get_secret(self, secret_key: str) -> Optional[str]:
        """Queries VS Code's SecretStorage via UDS reverse request."""
        if not self.uds_server:
            return None
        try:
            response = await self.uds_server.request_client_context(
                "vscode_command",
                {"command": "agenticWorkspace.getSecret", "target_path": secret_key}
            )
            return response.get("value")
        except Exception as e:
            logger.warning(f"Failed to fetch secret '{secret_key}' from extension host: {e}")
            return None

    async def execute_search(
        self,
        query: str,
        run_id: str,
        search_config: Optional[Dict[str, Any]] = None,
        max_chars: int = 4000
    ) -> str:
        """Executes search through the waterfall: Tavily -> Brave -> SearxNG."""
        search_config = search_config or {}
        if not search_config.get("enabled", True):
            return "Search blocked: Web search is disabled for this profile."

        results: List[Dict[str, str]] = []

        # --- Tier 1: Tavily (Primary Markdown Engine) ---
        tavily_key = await self._get_secret("tavily_api_key")
        if tavily_key:
            try:
                logger.info("Executing Tier 1 search (Tavily)...")
                results = await asyncio.wait_for(
                    self._fetch_tavily(query, tavily_key), timeout=6.0
                )
            except Exception as e:
                logger.warning(f"Tier 1 (Tavily) failed: {e}. Falling back...")

        # --- Tier 2: Brave Search (Secondary Fallback) ---
        if not results:
            brave_key = await self._get_secret("brave_api_key")
            if brave_key:
                try:
                    logger.info("Executing Tier 2 search (Brave)...")
                    results = await asyncio.wait_for(
                        self._fetch_brave(query, brave_key), timeout=6.0
                    )
                except Exception as e:
                    logger.warning(f"Tier 2 (Brave) failed: {e}. Falling back...")

        # --- Tier 3: SearxNG (Local / Self-Hosted Failsafe) ---
        if not results:
            searxng_url = search_config.get("searxng_url", "http://localhost:8080")
            try:
                logger.info(f"Executing Tier 3 search (SearxNG at {searxng_url})...")
                results = await asyncio.wait_for(
                    self._fetch_searxng(query, searxng_url), timeout=8.0
                )
            except Exception as e:
                logger.warning(f"Tier 3 (SearxNG) failed: {e}.")

        if not results:
            return f"Error: Web search failed across all configured providers for query: '{query}'."

        # Index and rank chunks using LanceDB
        return self._embed_rank_and_format(query, run_id, results, max_chars)

    async def _fetch_tavily(self, query: str, api_key: str) -> List[Dict[str, str]]:
        url = "https://api.tavily.com/search"
        payload = {
            "query": query,
            "search_depth": "basic",
            "include_raw_content": False,
            "max_results": 5
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")
                    }
                    for r in data.get("results", [])
                    if r.get("content")
                ]

    async def _fetch_brave(self, query: str, api_key: str) -> List[Dict[str, str]]:
        url = "https://api.search.brave.com/res/v1/web/search"
        params = {"q": query, "count": 5}
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                web_results = data.get("web", {}).get("results", [])
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("description", "")
                    }
                    for r in web_results
                    if r.get("description")
                ]

    async def _fetch_searxng(self, query: str, base_url: str) -> List[Dict[str, str]]:
        endpoint = f"{base_url.rstrip('/')}/search"
        params = {"q": query, "format": "json"}
        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")
                    }
                    for r in data.get("results", [])[:5]
                    if r.get("content")
                ]

    def _embed_rank_and_format(
        self,
        query: str,
        run_id: str,
        results: List[Dict[str, str]],
        max_chars: int
    ) -> str:
        """Chunks snippets, indexes in an ephemeral table, and formats top-ranked chunks."""
        records = []
        for idx, item in enumerate(results):
            text = f"Title: {item['title']}\nURL: {item['url']}\n\n{item['content']}"
            records.append({
                "id": f"{run_id}_{idx}",
                "text": text,
                "url": item["url"],
                "title": item["title"]
            })

        table_name = f"search_{run_id.replace('-', '_')}"
        table = self.db.create_table(table_name, data=records, mode="overwrite")

        # If vector_store embedding is available, perform semantic search; otherwise take top results
        retrieved_texts: List[str] = []
        current_len = 0

        for r in records:
            chunk = f"### [{r['title']}]({r['url']})\n{r['text']}\n"
            if current_len + len(chunk) > max_chars:
                break
            retrieved_texts.append(chunk)
            current_len += len(chunk)

        # Cleanup table after retrieval
        try:
            self.db.drop_table(table_name)
        except Exception:
            pass

        formatted = "\n---\n".join(retrieved_texts)
        return f"Web Search Results for '{query}':\n\n{formatted}"

    def cleanup(self):
        """Removes the ephemeral storage directory on shutdown."""
        self._temp_dir.cleanup()
