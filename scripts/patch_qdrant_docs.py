"""Apply the Qdrant doc insertion patch to CONFIGURATION.md."""

from pathlib import Path

path = Path("backend/docs/CONFIGURATION.md")
content = path.read_text(encoding="utf-8")

needle = (
    "This integration is retrieval-only. Document insertion, indexing, and graph\n"
    "mutation remain in LightRAG and are not exposed as Agent tools or BerkshireAgent\n"
    "APIs.\n\n### Tool Groups"
)

if needle not in content:
    raise SystemExit("needle not found")

addition = (
    "This integration is retrieval-only. Document insertion, indexing, and graph\n"
    "mutation remain in LightRAG and are not exposed as Agent tools or BerkshireAgent\n"
    "APIs.\n\n"
    "### Qdrant Knowledge Retrieval\n\n"
    "Qdrant integration is disabled by default. It is an alternative provider for the same read-only `knowledge_search` tool: an operator picks RAGFlow, LightRAG, or Qdrant by which entry appears in the `tools:` list \u2014 the entries share one name, and on duplicate names BerkshireAgent keeps the **first** configured entry, so configure exactly one. Works against self-hosted Qdrant and Qdrant Cloud (the same REST API). The provider is read-only: document insertion, payload updates, and collection management remain in Qdrant and are not exposed as Agent tools or BerkshireAgent APIs.\n\n"
    "```yaml\n"
    "tool_groups:\n"
    "  - name: knowledge\n\n"
    "tools:\n"
    "  - name: knowledge_search\n"
    "    group: knowledge\n"
    "    use: deerflow.community.qdrant.tools:knowledge_search_tool\n"
    "    base_url: https://your-cluster.qdrant.io  # Qdrant Cloud or self-hosted\n"
    "    api_key: $QDRANT_API_KEY                   # Omit only for unauthenticated trusted servers\n"
    "    collection_name: my_documents              # REQUIRED: collection to search\n"
    "    vector_name: \"\"                            # Optional: named vector (default = unnamed)\n"
    "    query_vector:                              # REQUIRED: dense embedding to search with\n"
    "      - 0.0123\n"
    "      - -0.0456\n"
    "      # ... one float per embedding dimension, e.g. 768 / 1024 / 1536\n"
    "    limit: 5                                   # Max points returned (1-1000)\n"
    "    score_threshold: 0.7                       # Optional: filter low-score matches\n"
    "    timeout: 30\n"
    "    max_chars_per_chunk: 800\n"
    "    max_total_chars: 8000\n\n"
    "  - name: list_knowledge_bases\n"
    "    group: knowledge\n"
    "    use: deerflow.community.qdrant.tools:list_knowledge_bases_tool\n"
    "```\n\n"
    "The Agent does not embed the user's query itself \u2014 BerkshireAgent's runtime is vector-agnostic. The operator supplies a pre-computed dense vector via `query_vector` (one float per embedding dimension), or wires a Qdrant-side inference model and extends the tool to forward `query_text` instead. `vector_name` selects a named vector when the collection stores multiple vectors per point (omit it for the unnamed/default vector). `score_threshold` filters low-confidence matches in Qdrant itself; the returned points keep their `score` in the formatted text so the Agent can prioritize citations. `limit` is bounded to 1\u20131000 by the schema (matching Qdrant's server-side cap). The `api_key` is sent as the `api-key` header and redacted from every model-visible error and server log; blank values are treated as unauthenticated. `base_url` must not contain embedded username or password information, and for Docker or Kubernetes it must be reachable from the Gateway container or Pod \u2014 `localhost` refers to that container or Pod, not the host machine.\n\n"
    "Qdrant point ids (numeric or UUID) are never exposed to the Agent. Citations use the operator-readable payload field \u2014 store a `source`, `file_path`, `document`, `title`, or `text` field when ingesting documents so each chunk surfaces a usable label. When the payload contains a `text` (or `content` / `chunk` / `page_content` / `body`) field the formatter uses it as the cited chunk; otherwise it falls back to a compact `key=value` rendering of the payload so the model still sees the chunk anchor.\n\n"
    "The companion `list_knowledge_bases` tool calls Qdrant's `GET /collections` endpoint and returns `{\"collections\": [\"name1\", \"name2\", ...]}` as JSON. Enable it when the Agent should be able to discover configured collection names before issuing a search. Both tools honor `timeout` and reuse the same credentials and base URL \u2014 the `knowledge_search` entry's settings apply.\n\n"
    "### Tool Groups"
)

new_content = content.replace(needle, addition, 1)
path.write_text(new_content, encoding="utf-8")
print("OK new length:", len(new_content))