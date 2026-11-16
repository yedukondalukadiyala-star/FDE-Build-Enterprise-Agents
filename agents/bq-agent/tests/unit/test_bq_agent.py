# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic unit tests for `bq-agent` configuration, tool contract, and A2A card."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.cloud import bigquery

from app import tools
from app.agent import app as adk_app
from app.agent import root_agent
from app.app_utils.a2a import attach_a2a_routes
from app.fast_api_app import BUG_DATABASE_SKILL


def test_agent_and_app_configuration() -> None:
    """Agent name, app name, model config, and single tool wiring match spec."""
    assert root_agent.name == "bq_agent"
    assert adk_app.name == "app"
    assert len(root_agent.tools) == 1
    assert root_agent.tools[0] is tools.find_similar_bugs


def test_find_similar_bugs_signature() -> None:
    """Tool exposes exact `find_similar_bugs(query: str) -> str` signature."""
    sig = inspect.signature(tools.find_similar_bugs)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "query"
    assert params[0].default is inspect.Parameter.empty


def test_find_similar_bugs_empty_query() -> None:
    """Blank queries return a clear 'no similar bugs found' message without network calls."""
    result = tools.find_similar_bugs("   ")
    assert "no similar bugs found" in result.lower()


def test_find_similar_bugs_vector_search_and_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embeds with text-embedding-004 in BQ region, runs parameterized VECTOR_SEARCH, and orders by distance."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "unit-proj")
    monkeypatch.setenv("BQ_DATASET", "code_assist_bugs")
    monkeypatch.setenv("BQ_TABLE", "known_bugs")
    monkeypatch.setenv("BQ_LOCATION", "us-central1")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-004")

    captured: dict[str, Any] = {}

    mock_genai = MagicMock()
    fake_vector = [0.125] * 768
    mock_genai.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=fake_vector)]
    )

    def fake_get_genai_client(project_id: str, location: str) -> Any:
        captured["genai_project"] = project_id
        captured["genai_location"] = location
        return mock_genai

    # Return out-of-order rows to verify ascending cosine-distance ordering too.
    fake_rows = [
        {
            "bug_id": "BUG-1009",
            "title": "JWT signature expired",
            "description": "Clock skew causes token expiry",
            "resolution": "Add leeway=30s and sync NTP.",
            "distance": 0.3125,
        },
        {
            "bug_id": "BUG-1001",
            "title": "FastAPI 500 on startup",
            "description": "Pydantic v2 upgrade breaks model config",
            "resolution": "Replace class Config with ConfigDict and call model_rebuild().",
            "distance": 0.0842,
        },
    ]

    mock_bq = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = fake_rows
    mock_bq.query.return_value = mock_job

    def fake_get_bq_client(project_id: str, location: str) -> Any:
        captured["bq_project"] = project_id
        captured["bq_location"] = location
        return mock_bq

    monkeypatch.setattr(tools, "_get_genai_client", fake_get_genai_client)
    monkeypatch.setattr(tools, "_get_bq_client", fake_get_bq_client)

    output = tools.find_similar_bugs(
        "FastAPI crashes with 500 PydanticUserError on startup"
    )

    # Verify embedding call uses text-embedding-004 in us-central1
    assert captured["genai_project"] == "unit-proj"
    assert captured["genai_location"] == "us-central1"
    mock_genai.models.embed_content.assert_called_once_with(
        model="text-embedding-004",
        contents="FastAPI crashes with 500 PydanticUserError on startup",
    )

    # Verify read-only VECTOR_SEARCH SQL and FLOAT64 array parameter
    assert captured["bq_project"] == "unit-proj"
    assert captured["bq_location"] == "us-central1"
    sql_arg = mock_bq.query.call_args.args[0]
    kwargs = mock_bq.query.call_args.kwargs
    assert "VECTOR_SEARCH" in sql_arg
    assert "`unit-proj.code_assist_bugs.known_bugs`" in sql_arg
    assert "'description_embedding'" in sql_arg
    assert "distance_type => 'COSINE'" in sql_arg
    assert "top_k => 3" in sql_arg
    assert "ORDER BY distance ASC" in sql_arg
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "MERGE", "ALTER"):
        assert forbidden not in sql_arg.upper()

    job_config: bigquery.QueryJobConfig = kwargs["job_config"]
    assert kwargs["location"] == "us-central1"
    assert len(job_config.query_parameters) == 1
    param = job_config.query_parameters[0]
    assert isinstance(param, bigquery.ArrayQueryParameter)
    assert param.name == "query_embedding"
    assert param.array_type == "FLOAT64"
    assert len(param.values) == 768

    # Verify output fields and ascending cosine distance ordering
    assert "BUG-1001" in output
    assert "BUG-1009" in output
    assert "0.0842" in output
    assert "0.3125" in output
    assert "ConfigDict" in output
    assert output.index("BUG-1001") < output.index("BUG-1009")


def test_find_similar_bugs_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty BigQuery result returns a clear 'no similar bugs found' string."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1] * 768)]
    )
    mock_bq = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    monkeypatch.setattr(tools, "_get_genai_client", lambda **_: mock_genai)
    monkeypatch.setattr(tools, "_get_bq_client", lambda **_: mock_bq)

    output = tools.find_similar_bugs("obscure nonexistent error")
    assert "no similar bugs found" in output.lower()


def test_find_similar_bugs_graceful_degradation_on_embedding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding failure returns a graceful unavailability message instead of raising."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.side_effect = RuntimeError("Vertex quota exceeded")
    monkeypatch.setattr(tools, "_get_genai_client", lambda **_: mock_genai)

    output = tools.find_similar_bugs("FastAPI 500 error")
    assert "temporarily unavailable" in output.lower()


def test_find_similar_bugs_graceful_degradation_on_bigquery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BigQuery failure returns a graceful unavailability message instead of raising."""
    mock_genai = MagicMock()
    mock_genai.models.embed_content.return_value = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.2] * 768)]
    )
    mock_bq = MagicMock()
    mock_bq.query.side_effect = RuntimeError("BigQuery backend unavailable")

    monkeypatch.setattr(tools, "_get_genai_client", lambda **_: mock_genai)
    monkeypatch.setattr(tools, "_get_bq_client", lambda **_: mock_bq)

    output = tools.find_similar_bugs("Postgres too many connections")
    assert "temporarily unavailable" in output.lower()


@pytest.mark.asyncio
async def test_a2a_agent_card_single_skill_and_public_url() -> None:
    """A2A card exposes a single semantic-search skill with an example and public URL."""
    test_app = FastAPI()
    runner = Runner(
        app=adk_app,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    await attach_a2a_routes(
        test_app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path="/a2a/app",
        skills=[BUG_DATABASE_SKILL],
        app_url="http://203.0.113.42:8080",
    )

    client = TestClient(test_app)
    for path in (
        "/a2a/app/.well-known/agent-card.json",
        "/.well-known/agent-card.json",
    ):
        resp = client.get(path)
        assert resp.status_code == 200
        card = resp.json()
        assert len(card["skills"]) == 1
        skill = card["skills"][0]
        assert skill["id"] == "semantic_bug_search"
        assert skill["examples"] == [
            "FastAPI returns a 500 on startup after a dependency bump."
        ]
        urls = [i["url"] for i in card["supportedInterfaces"]]
        assert "http://203.0.113.42:8080/a2a/app" in urls
