import asyncio
import json
import kuzu
import logging
import os
import time
from opentelemetry import trace

from services.orchestrator.src.prompts import load_prompt

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

class SemanticGraphStore:
    def __init__(self, workspace_root: str):
        # 1. Create only the parent directory
        parent_dir = os.path.join(workspace_root, '.agentic_workspace')
        os.makedirs(parent_dir, exist_ok=True)
        
        # 2. Provide a file path for the database, not a directory
        self.db_path = os.path.join(parent_dir, 'semantic.kuzu')
        
        # 3. Initialize embedded DB
        self.db = kuzu.Database(self.db_path)
        self.conn = kuzu.Connection(self.db)
        self._initialize_schema()

    def _initialize_schema(self):
        """Idempotent schema creation utilizing OpenCypher DDL."""
        # Query existing catalog to avoid RuntimeError on re-initialization
        result = self.conn.execute("CALL show_tables() RETURN name")
        existing_tables = {row[0] for row in result}

        # 1. Entity Nodes
        if 'User' not in existing_tables:
            self.conn.execute("CREATE NODE TABLE User (id STRING, role STRING, PRIMARY KEY (id))")
            # Seed the default user
            self.conn.execute("CREATE (u:User {id: 'default_user', role: 'Engineer'})")
            
        if 'Project' not in existing_tables:
            self.conn.execute("CREATE NODE TABLE Project (path STRING, PRIMARY KEY (path))")
            
        if 'Tool' not in existing_tables:
            self.conn.execute("CREATE NODE TABLE Tool (name STRING, PRIMARY KEY (name))")

        # 2. Relationship Edges (with temporal validity for evolving preferences)
        if 'PREFERS' not in existing_tables:
            self.conn.execute("CREATE REL TABLE PREFERS (FROM User TO Tool, weight DOUBLE, valid_until TIMESTAMP)")
            
        if 'ENFORCES' not in existing_tables:
            self.conn.execute("CREATE REL TABLE ENFORCES (FROM Project TO Tool, description STRING)")
            
        if 'AVOIDS' not in existing_tables:
            self.conn.execute("CREATE REL TABLE AVOIDS (FROM User TO Tool, reason STRING)")

        logger.info("KùzuDB semantic graph schema verified.")

    async def extract_and_store(self, trajectory_text: str, workspace_scope: str, llm_provider):
        with tracer.start_as_current_span("semantic_graph_extraction") as span:
            span.set_attribute("workspace_scope", workspace_scope)
            span.set_attribute("llm.provider", type(llm_provider).__name__)
            
            template = load_prompt("semantic_extraction_v1")
            prompt = template.format(workspace_scope=workspace_scope, trajectory_text=trajectory_text)

            try:
                context = [{"role": "user", "content": prompt}]
                
                t0 = time.time()
                raw_response = await asyncio.to_thread(llm_provider.generate, context)
                span.set_attribute("latency.llm_extraction_ms", (time.time() - t0) * 1000)
                
                clean_json = raw_response.strip()
                if clean_json.startswith("```json"):
                    clean_json = clean_json.split("```json")[1].split("```")[0].strip()
                    
                extracted_data = json.loads(clean_json)
                
                t1 = time.time()
                await asyncio.to_thread(self._execute_upserts, extracted_data, workspace_scope)
                span.set_attribute("latency.kuzudb_upsert_ms", (time.time() - t1) * 1000)
                
            except json.JSONDecodeError as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, "LLM produced invalid JSON"))
                logger.warning("Semantic extraction failed: LLM output was not valid JSON.")
            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                logger.error(f"Semantic extraction worker failed: {e}")

    def _execute_upserts(self, data: dict, workspace_scope: str):
        """Executes temporal OpenCypher queries to version and update preferences."""
        
        # 1. Ensure the active project node exists
        self.conn.execute("MERGE (p:Project {path: $path})", {"path": workspace_scope})

        # 2. Handle Temporal Conflict Resolution (Retire old edges)
        for migration in data.get("supersedes", []):
            old_tool = str(migration.get("replaces", "")).lower()
            if old_tool:
                # Cap the validity window of the superseded tool
                self.conn.execute(
                    """
                    MATCH (u:User {id: 'default_user'})-[r:PREFERS]->(t:Tool {name: $old_name})
                    WHERE r.valid_until IS NULL
                    SET r.valid_until = current_timestamp()
                    """,
                    {"old_name": old_tool}
                )
                logger.info(f"🕰️ Semantic graph: Retired preference for '{old_tool}'.")

        # 3. Upsert New Preferences
        # Combine standard preferences with the 'new' tools from the supersedes list
        active_tools = set([str(t).lower() for t in data.get("preferences", [])])
        for migration in data.get("supersedes", []):
            if migration.get("new"):
                active_tools.add(str(migration.get("new")).lower())

        for tool_name in active_tools:
            self.conn.execute("MERGE (t:Tool {name: $name})", {"name": tool_name})
            
            # Upsert the PREFERS edge. If it already exists and is active, increment its weight.
            # If it doesn't exist, create it with no valid_until timestamp.
            self.conn.execute(
                """
                MATCH (u:User {id: 'default_user'}), (t:Tool {name: $name})
                MERGE (u)-[r:PREFERS {valid_until: NULL}]->(t)
                ON CREATE SET r.weight = 1.0
                ON MATCH SET r.weight = r.weight + 0.1
                """, 
                {"name": tool_name}
            )

        # 4. Upsert Avoidances
        for tool in data.get("avoidances", []):
            tool_name = str(tool).lower()
            self.conn.execute("MERGE (t:Tool {name: $name})", {"name": tool_name})
            self.conn.execute(
                """
                MATCH (u:User {id: 'default_user'}), (t:Tool {name: $name})
                MERGE (u)-[r:AVOIDS]->(t)
                """, 
                {"name": tool_name}
            )

        logger.info(f"🧠 Semantic graph updated successfully for workspace: {workspace_scope}")