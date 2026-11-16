# bq-agent

Standalone ADK Python specialist agent exposing an **A2A** interface on **GKE** to answer *"Have we seen this software problem before, and how was it fixed?"* via read-only semantic vector search over `code_assist_bugs.known_bugs` in BigQuery (`us-central1`).

## Capabilities

- **Model:** Configurable via `MODEL` env var (`gemini-3.5-flash` by default, served via Vertex AI).
- **Single Tool (`find_similar_bugs`):**
  1. Embeds free-text incident queries with `text-embedding-004` (`768`-dim) in `us-central1`.
  2. Executes a parameterized, read-only BigQuery `VECTOR_SEARCH` (`distance_type => 'COSINE'`, `top_k => 3`) over `description_embedding`, ordered by ascending cosine distance.
  3. Returns `bug_id`, `title`, `description`, `resolution`, and cosine distance, with graceful degradation on empty results or backend errors.
- **A2A Interface:** Serves a single-skill agent card (`semantic_bug_search`) with a representative example query at `/a2a/app/.well-known/agent-card.json` (and `/.well-known/agent-card.json`) over an external GKE `LoadBalancer`.
- **Authentication:** Application Default Credentials locally; GKE Workload Identity (`bq-agent-app` with `roles/bigquery.jobUser`, `roles/bigquery.dataViewer`, and `roles/aiplatform.user`) in cluster.

## Commands

| Command | Description |
| --- | --- |
| `agents-cli install` | Install dependencies using `uv` |
| `agents-cli run "<query>"` | Smoke-test the agent locally |
| `agents-cli eval run` | Run agent evaluation (`final_response_quality`, `hallucination`, `custom_response_quality`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli lint` | Run code quality checks (`ruff`, `ty`, `codespell`) |
| `agents-cli deploy` | Provision GKE infrastructure via Terraform and deploy `bq-agent` |
