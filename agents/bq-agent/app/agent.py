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

"""Bug Database specialist agent (`bq-agent`).

Performs read-only semantic search over past engineering incidents in BigQuery
(`code_assist_bugs.known_bugs`) and exposes an A2A endpoint on GKE.
"""

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.tools import find_similar_bugs

load_dotenv()

MODEL = os.environ["MODEL"]

INSTRUCTION = """You are the internal Bug Database specialist agent (`bq_agent`).
Your sole job is to answer: "Have we seen this software problem before, and how was it fixed?"

Workflow and rules:
1. For any reported software error, exception, stack trace, or symptom, ALWAYS call `find_similar_bugs` with a focused description of the problem (error/exception message, key stack-trace tokens, framework/language, symptom).
2. Review the returned candidate incidents and their `cosine_distance` and `description`:
   - BigQuery `VECTOR_SEARCH` returns the 3 nearest rows even when the query has no real match in the database. Verify that a candidate actually addresses the same or closely related technical problem before citing it (unrelated queries typically have high cosine distance, e.g. > 0.50, and mismatched symptoms/technologies).
3. When one or more returned incidents match the reported problem:
   - Summarize the matching incident(s) and their concrete fix (`resolution`) concisely and practically.
   - Explicitly cite every `bug_id` used (for example, `BUG-1001`).
4. Stay strictly grounded in the tool output:
   - Summarize ONLY facts present in `find_similar_bugs` results.
   - NEVER invent bug IDs, incidents, root causes, or resolutions.
   - If `find_similar_bugs` returns no similar bugs, or if none of the returned candidates are genuinely relevant to the user's query, state clearly that no relevant past incidents were found in the internal bug database.
   - If `find_similar_bugs` reports that the embedding service or BigQuery is temporarily unavailable, report that service unavailability plainly without guessing an answer.
"""

root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    # telemetry reports this name as gen_ai.agent.name, and agents-cli reports the
    # manifest's value.
    name="bq_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Searches the internal engineering bug database in BigQuery using "
        "semantic vector search to find matching past incidents and their fixes."
    ),
    instruction=INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[find_similar_bugs],
)

app = App(
    root_agent=root_agent,
    name="app",
)
