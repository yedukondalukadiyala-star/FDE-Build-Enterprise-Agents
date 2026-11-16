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

"""Read-only semantic bug search tool backed by Vertex AI embeddings + BigQuery."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import google.auth
from google import genai
from google.cloud import bigquery

logger = logging.getLogger(__name__)

DEFAULT_BQ_DATASET = "code_assist_bugs"
DEFAULT_BQ_TABLE = "known_bugs"
DEFAULT_BQ_LOCATION = "us-central1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-004"


def _resolve_project_id() -> str:
    """Resolves the Google Cloud project ID from env or Application Default Credentials."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project_id:
        return project_id
    _, adc_project = google.auth.default()
    if not adc_project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set and ADC has no default project."
        )
    return adc_project


def _get_genai_client(project_id: str, location: str) -> genai.Client:
    """Builds a Vertex AI GenAI client scoped to the embedding region."""
    return genai.Client(vertexai=True, project=project_id, location=location)


def _get_bq_client(project_id: str, location: str) -> bigquery.Client:
    """Builds a BigQuery client using Application Default Credentials."""
    return bigquery.Client(project=project_id, location=location)


def _row_field(row: Any, name: str) -> Any:
    """Reads a field from a BigQuery Row, dict, or object."""
    if isinstance(row, Mapping):
        return row[name]
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name)


def find_similar_bugs(query: str) -> str:
    """Searches the internal BigQuery bug database for similar past incidents.

    Embeds the free-text problem description with ``text-embedding-004`` in the
    table's region and executes a read-only BigQuery ``VECTOR_SEARCH`` using
    cosine distance (``top_k => 3``).

    Args:
        query: Free-text software problem description (exception, key
            stack-trace terms, language/framework, symptom).

    Returns:
        Formatted matches with ``bug_id``, ``title``, ``description``,
        ``resolution``, and cosine ``distance`` ordered by ascending distance,
        a clear message when no similar bugs are found, or a graceful
        degradation message if the embedding service or BigQuery is unavailable.
    """
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return "No similar bugs found: query was empty."

    bq_dataset = os.environ.get("BQ_DATASET", DEFAULT_BQ_DATASET).strip()
    bq_table = os.environ.get("BQ_TABLE", DEFAULT_BQ_TABLE).strip()
    bq_location = os.environ.get("BQ_LOCATION", DEFAULT_BQ_LOCATION).strip()
    embedding_model = os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()

    try:
        project_id = _resolve_project_id()
    except Exception as exc:
        logger.warning("Failed to resolve GCP project for bug search: %s", exc)
        return (
            "Bug database search is temporarily unavailable: could not resolve "
            f"Google Cloud project ({type(exc).__name__})."
        )

    try:
        genai_client = _get_genai_client(project_id=project_id, location=bq_location)
        embed_response = genai_client.models.embed_content(
            model=embedding_model,
            contents=cleaned_query,
        )
        embeddings = getattr(embed_response, "embeddings", None) or []
        raw_values = embeddings[0].values if embeddings else None
        if not raw_values:
            return (
                "Bug database search is temporarily unavailable: embedding "
                "model returned no vector."
            )
        query_vector = [float(val) for val in raw_values]
    except Exception as exc:
        logger.warning("Embedding generation failed in find_similar_bugs: %s", exc)
        return (
            "Bug database search is temporarily unavailable while generating "
            f"query embeddings ({type(exc).__name__}: {exc})."
        )

    sql = (
        "SELECT\n"
        "  base.bug_id AS bug_id,\n"
        "  base.title AS title,\n"
        "  base.description AS description,\n"
        "  base.resolution AS resolution,\n"
        "  distance\n"
        "FROM VECTOR_SEARCH(\n"
        f"  TABLE `{project_id}.{bq_dataset}.{bq_table}`,\n"
        "  'description_embedding',\n"
        "  (SELECT @query_embedding AS description_embedding),\n"
        "  top_k => 3,\n"
        "  distance_type => 'COSINE'\n"
        ")\n"
        "ORDER BY distance ASC"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("query_embedding", "FLOAT64", query_vector)
        ]
    )

    try:
        bq_client = _get_bq_client(project_id=project_id, location=bq_location)
        query_job = bq_client.query(sql, job_config=job_config, location=bq_location)
        rows = list(query_job.result())
    except Exception as exc:
        logger.warning("BigQuery VECTOR_SEARCH failed in find_similar_bugs: %s", exc)
        return (
            "Bug database search is temporarily unavailable while querying "
            f"BigQuery ({type(exc).__name__}: {exc})."
        )

    if not rows:
        return "No similar bugs found in the internal bug database."

    sorted_rows = sorted(rows, key=lambda r: float(_row_field(r, "distance")))
    formatted_matches = [
        f"Top {len(sorted_rows)} candidate incident(s) ordered by cosine distance "
        "(0.0 = identical, higher = less similar):"
    ]
    for idx, row in enumerate(sorted_rows, start=1):
        bug_id = _row_field(row, "bug_id")
        title = _row_field(row, "title")
        description = _row_field(row, "description")
        resolution = _row_field(row, "resolution")
        distance = float(_row_field(row, "distance"))
        formatted_matches.append(
            f"\n[{idx}] bug_id: {bug_id}\n"
            f"    cosine_distance: {distance:.4f}\n"
            f"    title: {title}\n"
            f"    description: {description}\n"
            f"    resolution: {resolution}"
        )

    return "\n".join(formatted_matches)
