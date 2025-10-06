import os
import json
import logging
import datetime as dt
import azure.functions as func
import requests

"""
This module implements the main HTTP triggered function for the AIOps agent.

It executes the following high‑level workflow when invoked:

1. **Query Log Analytics via KQL** – fetches recent logs or traces to provide
   realtime telemetry context.  The workspace ID and KQL query are pulled
   from environment variables.  The Azure SDK for Python is used to
   authenticate with the workspace via a managed identity.

2. **Run Retrieval‑Augmented Generation (RAG)** – performs a semantic
   search against Azure Cognitive Search (vector or keyword index) to pull
   relevant runbooks, incident reports and configuration snapshots.  These
   documents are passed to the language model as grounding context.

3. **Invoke Azure OpenAI** – passes the user’s question, sample logs and
   retrieved documents to an LLM to generate a root cause analysis and plan
   of action.  The prompt instructs the model to return a strict JSON
   object containing the summary, confidence, proposed actions and
   evidence links.

4. **Optionally Execute Remediation** – inspects the returned JSON plan and
   triggers low/medium risk actions via a secondary HTTP endpoint.  High
   risk actions are skipped for human approval.  All actions results are
   appended to the plan for transparency.

5. **Post an Adaptive Card to Microsoft Teams** – builds a rich card that
   summarises the incident, root cause, executed actions and evidence.  The
   card JSON can be customised or extended; this example posts to a
   pre‑configured Teams webhook.

To deploy this function you must provide the following environment
variables in your Azure Function App configuration:

```
LOG_ANALYTICS_WORKSPACE_ID=<workspace guid>
KQL_QUERY=<Kusto query to sample logs>
SEARCH_ENDPOINT=<https endpoint for Cognitive Search>
SEARCH_INDEX=<name of the search index>
SEARCH_API_KEY=<Cognitive Search admin/query key>
OPENAI_ENDPOINT=<Azure OpenAI resource endpoint>
OPENAI_API_KEY=<API key for OpenAI>
OPENAI_DEPLOYMENT=<deployment name, e.g. gpt-4o>
TEAMS_WEBHOOK_URL=<incoming Teams webhook URL>
REMEDIATION_URL=<HTTP endpoint for remediation function>
REMEDIATION_KEY=<optional function key for remediation>
```

See the README in the repository for a full walk‑through of how to
configure these services.
"""

# Azure Monitor Logs client dependencies
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

# Azure Cognitive Search client
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

# Azure OpenAI
import openai


def _run_kql(workspace_id: str, query: str) -> list[dict]:
    """Query Log Analytics for recent logs.

    Returns a list of dictionaries mapping column names to values.
    """
    credential = DefaultAzureCredential()
    client = LogsQueryClient(credential=credential)
    try:
        resp = client.query_workspace(
            workspace_id,
            query,
            timespan=dt.timedelta(minutes=30),
        )
    except Exception as ex:
        logging.error("Failed to query logs: %s", ex)
        return []

    if resp.status == LogsQueryStatus.PARTIAL:
        table = resp.partial_data[0]
    elif resp.status == LogsQueryStatus.SUCCESS:
        table = resp.tables[0] if resp.tables else None
    else:
        table = None

    rows: list[dict] = []
    if table:
        cols = [c.name for c in table.columns]
        for r in table.rows:
            rows.append(dict(zip(cols, r)))
    return rows


def _rag_search(client: SearchClient, query: str, top_k: int = 5) -> list[dict]:
    """Perform a semantic search against Cognitive Search and return a list of
    document dictionaries.
    """
    results = client.search(
        search_text=query,
        top=top_k,
        include_total_count=False,
    )
    docs = []
    for doc in results:
        docs.append({
            "id": doc.get("id") or doc.get("doc_id") or "",
            "title": doc.get("title") or "",
            "content": doc.get("content") or doc.get("chunk") or "",
            "url": doc.get("url") or ""
        })
    return docs


def _build_prompt(user_question: str, logs_sample: list[dict], kb_docs: list[dict]) -> tuple[str, str]:
    """Construct the system and user messages for the OpenAI call.
    
    The system message describes the agent’s responsibilities.  The user
    message provides the question, the RAG snippets and a preview of
    sampled logs.  The LLM is instructed to return a JSON payload with
    explicit fields for the root cause summary, confidence, actions and
    evidence.
    """
    # Build RAG context from docs
    kb_snippets = "\n\n".join(
        [f"TITLE: {d['title']}\nCONTENT:\n{d['content'][:1000]}" for d in kb_docs]
    )
    # Sample logs to avoid exceeding token limits
    logs_preview = json.dumps(logs_sample[:20], indent=2)

    system = (
        "You are an AIOps reasoning agent. You must:\n"
        "1) Correlate metrics, logs and incidents.\n"
        "2) Explain the likely root cause succinctly.\n"
        "3) Propose a JSON plan with safe remediation actions.\n"
        "Only return a JSON object in your final response."
    )

    user = (
        f"QUESTION:\n{user_question}\n\n"
        f"CONTEXT_KB:\n{kb_snippets}\n\n"
        f"CONTEXT_LOGS_SAMPLE:\n{logs_preview}\n\n"
        "Return a strict JSON object with the following schema:\n"
        "{\n"
        "  \"rca_summary\": \"string summarising the suspected root cause\",\n"
        "  \"confidence\": number between 0 and 1,\n"
        "  \"actions\": [\n"
        "     {\"name\": \"action_name\", \"params\": {\"key\": \"value\"}, \"risk\": \"low|medium|high\"}\n"
        "  ],\n"
        "  \"evidence\": {\"kql_name\": \"string\", \"kql_snippet\": \"string\", \"links\": [\"url\"]}\n"
        "}"
    )
    return system, user


def _call_openai(system: str, user: str, endpoint: str, deployment: str, api_key: str) -> dict:
    """Call Azure OpenAI to generate the analysis plan.
    
    The function sets up the appropriate configuration and expects the
    model to return a JSON string which is parsed and returned as a
    dictionary.  Any parsing errors are logged and a fallback is
    returned.
    """
    openai.api_type = "azure"
    openai.api_base = endpoint
    openai.api_version = "2024-02-01"
    openai.api_key = api_key

    try:
        response = openai.ChatCompletion.create(
            engine=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        content = response["choices"][0]["message"]["content"]
        plan = json.loads(content)
        return plan
    except Exception as ex:
        logging.error("OpenAI call failed: %s", ex)
        return {
            "rca_summary": "Unable to generate analysis due to error.",
            "confidence": 0.0,
            "actions": [],
            "evidence": {}
        }


def _maybe_remediate(plan: dict, remediation_url: str, remediation_key: str | None = None) -> list[dict]:
    """Execute remediation actions with low or medium risk via HTTP POST.
    
    A simple policy is applied: low and medium risk actions are executed
    immediately, high risk actions are skipped for later approval.  The
    results of each POST are captured for audit.
    """
    results = []
    for act in plan.get("actions", []):
        name = act.get("name")
        params = act.get("params", {})
        risk = (act.get("risk") or "").lower()
        if risk in ("low", "medium") and remediation_url:
            payload = {"action": name, "params": params}
            headers = {"Content-Type": "application/json"}
            if remediation_key:
                headers["x-functions-key"] = remediation_key
            try:
                resp = requests.post(remediation_url, headers=headers, json=payload, timeout=20)
                results.append({
                    "action": name,
                    "status": resp.status_code,
                    "response": resp.text[:300]
                })
            except Exception as ex:
                results.append({
                    "action": name,
                    "status": "error",
                    "response": str(ex)
                })
        else:
            results.append({
                "action": name,
                "status": "skipped",
                "reason": "risk too high or remediation endpoint missing"
            })
    return results


def _build_adaptive_card(plan: dict, incident: dict) -> dict:
    """Construct an Adaptive Card payload for Teams from the analysis plan.

    The card summarises the incident context, suspected root cause,
    actions and evidence.  Links to dashboards or incidents are passed
    through from the incident context dictionary.
    """
    kql = plan.get("evidence", {}).get("kql_snippet", "")
    kql_name = plan.get("evidence", {}).get("kql_name", "")
    links = ", ".join(plan.get("evidence", {}).get("links", []))
    actions_text = "\n".join([
        f"• {a.get('name')} {json.dumps(a.get('params', {}))}" for a in plan.get("actions", [])
    ])

    card = {
        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": f"RCA: {incident['title']}", "wrap": True, "size": "Large", "weight": "Bolder"},
            {"type": "TextBlock", "text": f"Environment: {incident['environment']} • Severity: {incident['severity']} • Started: {incident['start_time_local']}", "wrap": True, "isSubtle": True},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Incident ID", "value": incident.get("id", "")},
                    {"title": "Service", "value": incident.get("service_name", "")},
                    {"title": "Region", "value": incident.get("region", "")},
                    {"title": "Change Correlation", "value": incident.get("change_ref", "n/a")},
                ],
            },
            {"type": "TextBlock", "text": "Suspected Root Cause", "weight": "Bolder", "spacing": "Medium"},
            {"type": "TextBlock", "text": plan.get("rca_summary", ""), "wrap": True},
            {"type": "TextBlock", "text": "Actions Executed", "weight": "Bolder", "spacing": "Medium"},
            {"type": "TextBlock", "text": actions_text, "wrap": True},
            {"type": "TextBlock", "text": "KQL & Evidence", "weight": "Bolder", "spacing": "Medium"},
            {
                "type": "RichTextBlock",
                "inlines": [
                    {"type": "TextRun", "text": "KQL: ", "weight": "Bolder"},
                    {"type": "TextRun", "text": kql_name, "isSubtle": True},
                ],
            },
            {"type": "TextBlock", "text": kql, "wrap": True, "fontType": "Monospace"},
            {
                "type": "RichTextBlock",
                "inlines": [
                    {"type": "TextRun", "text": "Evidence: ", "weight": "Bolder"},
                    {"type": "TextRun", "text": links, "isSubtle": True},
                ],
            },
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "View Dashboard", "url": incident.get("dashboard_url", "https://")},
            {"type": "Action.OpenUrl", "title": "Open Incident", "url": incident.get("incident_url", "https://")},
        ],
    }
    return card


def _post_to_teams(card_json: dict, webhook_url: str) -> int | None:
    """Send the Adaptive Card to a Teams incoming webhook.

    Returns the HTTP status code if the post is attempted, otherwise None
    when the webhook URL is not configured.
    """
    if not webhook_url:
        logging.warning("No Teams webhook configured, skipping card post.")
        return None
    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(webhook_url, headers=headers, data=json.dumps(card_json), timeout=15)
        return response.status_code
    except Exception as ex:
        logging.error("Failed to post to Teams: %s", ex)
        return None


async def main(req: func.HttpRequest) -> func.HttpResponse:
    """Main entry point for the AIOps agent function.

    Expects a JSON payload with an optional `question` string and an
    optional `incident` dictionary.  See the README for example
    requests.  The function returns a JSON response containing the
    generated plan and the status of posting to Teams.
    """
    logging.info("AIOps agent function triggered.")
    try:
        body = req.get_json() if req.method == "POST" else {}
    except Exception:
        body = {}

    # Extract inputs
    question = body.get(
        "question",
        "Why did latency spike in the last 30 minutes?"
    )
    # Default incident context used if none provided by caller
    incident_ctx = body.get("incident", {
        "title": "Service latency spike",
        "environment": "prod",
        "severity": "Sev2",
        "start_time_local": dt.datetime.now().isoformat(timespec="seconds"),
        "id": "INC-XXXXX",
        "service_name": "unknown-service",
        "region": "unknown-region",
        "change_ref": "unknown-change",
        "dashboard_url": "https://portal.azure.com/",
        "incident_url": "https://dev.azure.com/",
    })

    # Read env vars
    workspace_id = os.getenv("LOG_ANALYTICS_WORKSPACE_ID", "")
    kql_query = os.getenv(
        "KQL_QUERY",
        "AppTraces | where Timestamp > ago(30m) | take 100"
    )
    search_endpoint = os.getenv("SEARCH_ENDPOINT", "")
    search_index = os.getenv("SEARCH_INDEX", "")
    search_api_key = os.getenv("SEARCH_API_KEY", "")
    openai_endpoint = os.getenv("OPENAI_ENDPOINT", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    openai_deployment = os.getenv("OPENAI_DEPLOYMENT", "")
    teams_webhook = os.getenv("TEAMS_WEBHOOK_URL", "")
    remediation_url = os.getenv("REMEDIATION_URL", "")
    remediation_key = os.getenv("REMEDIATION_KEY", None)

    # 1) Query logs via KQL
    logs_sample = _run_kql(workspace_id, kql_query) if workspace_id else []

    # 2) RAG search
    kb_docs: list[dict] = []
    if search_endpoint and search_index and search_api_key:
        search_client = SearchClient(
            endpoint=search_endpoint,
            index_name=search_index,
            credential=AzureKeyCredential(search_api_key),
        )
        kb_docs = _rag_search(search_client, question, top_k=5)

    # 3) Call LLM
    system_msg, user_msg = _build_prompt(question, logs_sample, kb_docs)
    plan = _call_openai(system_msg, user_msg, openai_endpoint, openai_deployment, openai_api_key)

    # 4) Execute remediation for low/medium risk
    results = _maybe_remediate(plan, remediation_url, remediation_key)
    # Append results to plan actions for transparency
    if results:
        plan.setdefault("actions", [])
        plan["actions"] += [
            {
                "name": f"result:{r['action']}",
                "params": r,
                "risk": "n/a"
            } for r in results
        ]

    # 5) Build and post Teams card
    card_payload = _build_adaptive_card(plan, incident_ctx)
    status = _post_to_teams(card_payload, teams_webhook)

    # Build response
    response_body = {
        "ok": True,
        "teams_post_status": status,
        "plan": plan,
        "kb_docs_used": [d["title"] for d in kb_docs],
    }
    return func.HttpResponse(
        json.dumps(response_body, indent=2),
        mimetype="application/json",
    )