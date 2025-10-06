# Hyper‑Personalized Multimodal AIOps Agent

This repository contains a reference implementation of a hyper‑personalized
multimodal AIOps agent built on **serverless Azure services**.  The
solution ingests telemetry (logs, metrics, documents and images),
performs retrieval‑augmented reasoning with a large language model, and
optionally executes remediation actions.  A ready‑to‑use Microsoft Teams
Adaptive Card is included to broadcast root cause analyses and
recommendations to your operations teams.

The implementation is designed to accompany *Edition 26* of the
**Dominant Forces in AI** newsletter and is intended to give
developers and solution architects a working example of a virtual SRE
agent that can be deployed to the Azure cloud.

## Repository structure

```
aiops_code/
├── function_app/                # Main AIOps agent Azure Function
│   ├── __init__.py              # Orchestrates logs, RAG, LLM and remediation
│   ├── function.json            # HTTP trigger configuration
│   └── requirements.txt         # Python dependencies
├── remediation/                 # Secondary function for executing safe actions
│   ├── __init__.py
│   └── function.json
├── teams_rca_card_template.json # Adaptive Card schema for Teams posts
└── README.md                    # This documentation
```

## Prerequisites

Before deploying the AIOps agent you will need:

1. **Azure subscription and resource group** where your resources will live.
2. **Log Analytics Workspace** for ingesting logs and metrics.  Note the
   workspace ID and ensure your services send telemetry to it.
3. **Azure Cognitive Search** index populated with runbooks, past
   incidents, architecture diagrams or configuration snapshots.  If you
   wish to use vector search you will need to generate embeddings for
   your documents ahead of time.
4. **Azure OpenAI Service** (or another model provider) with a
   GPT‑4‑class model deployment.  Record the resource endpoint, API
   key and deployment name.
5. **Microsoft Teams Incoming Webhook** set up in a channel where you
   want root cause analyses to be posted.
6. **Azure Storage or Blob container** if you intend to index PDF or
   image documents for the search index.

## Quick start

### 1. Clone and prepare the repository

```
git clone https://github.com/Huzefaaa2/AIOps.git
cd AIOps/aiops_code
```

### 2. Create the search index

Use the Azure Portal or the Azure CLI to create a Cognitive Search
service and an index.  The index schema should include at minimum:

- `id` – unique identifier (`Edm.String`)
- `title` – document title (`Edm.String`)
- `content` – full text content (`Edm.String`)
- `url` – link back to the source document (`Edm.String`)

If you choose vector search, also include an `embedding` field of type
`Collection(Edm.Single)` and populate it with embeddings generated from
your documents using the same OpenAI model you will deploy.

Populate the index with your runbooks, incident post‑mortems and
architecture documentation.  The AIOps agent will use this as its
knowledge base for retrieval augmented generation (RAG).

### 3. Deploy the remediation function

The remediation function executes low/medium risk actions on your
infrastructure.  It is packaged separately to allow for distinct
security boundaries.  Deploy it first so you can reference its URL in
the main agent configuration.

1. Create a new Azure Function App targeting Python (3.10 or newer) in
   your resource group.
2. Ensure the setting `FUNCTIONS_WORKER_RUNTIME` is set to `python`.
3. Deploy the contents of `aiops_code/remediation` via Zip Deploy or
   the Azure Functions Core Tools.  The simplest way from the root of
   this repository is:

   ```
   func azure functionapp publish <your-remediation-app-name> \
     --python \
     --build local \
     --no-bundler \
     --source ./aiops_code/remediation
   ```

4. Note the URL of the function (e.g.
   `https://<your-remediation-app>.azurewebsites.net/api/remediation`) and,
   if enabled, the function key.  You will refer to this when
   configuring the agent.

### 4. Deploy the main AIOps agent

1. Create another Azure Function App for the agent (or deploy to the
   same app using a different route).  Enable **Managed Identity** so
   the function can authenticate to Log Analytics via Azure AD.
2. In the Function App Configuration blade, add the following
   application settings:

   | Setting                      | Description |
   |------------------------------|-------------|
   | `LOG_ANALYTICS_WORKSPACE_ID` | Workspace ID of your Log Analytics instance |
   | `KQL_QUERY`                  | KQL query used to sample recent logs (defaults to `AppTraces | where Timestamp > ago(30m) | take 100`) |
   | `SEARCH_ENDPOINT`            | Endpoint URL of your Cognitive Search service |
   | `SEARCH_INDEX`               | Name of the index created in step 2 |
   | `SEARCH_API_KEY`             | Admin or query key for your search service |
   | `OPENAI_ENDPOINT`            | Endpoint URL of your Azure OpenAI resource |
   | `OPENAI_API_KEY`             | API key for your OpenAI resource |
   | `OPENAI_DEPLOYMENT`          | Deployment name of your GPT‑4 model |
   | `TEAMS_WEBHOOK_URL`          | Incoming webhook URL for posting Adaptive Cards |
   | `REMEDIATION_URL`            | URL of the remediation function you deployed in step 3 |
   | `REMEDIATION_KEY`            | Function key for the remediation endpoint (optional) |

3. Deploy the contents of `aiops_code/function_app` to your Function
   App.  Using the Functions Core Tools:

   ```
   func azure functionapp publish <your-agent-app-name> \
     --python \
     --build local \
     --no-bundler \
     --source ./aiops_code/function_app
   ```

### 5. Invoke the agent

Once deployed, you can test the agent by sending an HTTP request to
the function endpoint.  For example:

```
POST https://<agent-app>.azurewebsites.net/api/aiops-agent
Content-Type: application/json

{
  "question": "Why did the response time spike overnight?",
  "incident": {
    "title": "Payments API latency spike",
    "environment": "prod",
    "severity": "Sev2",
    "start_time_local": "2025-10-06T10:00:00",
    "id": "INC-12345",
    "service_name": "payments-api",
    "region": "uk-south",
    "change_ref": "deploy 1042",
    "dashboard_url": "https://portal.azure.com/",
    "incident_url": "https://dev.azure.com/"
  }
}
```

The response will include the root cause summary, a list of proposed
actions (and their execution results if applicable), the documents used
for grounding, and the HTTP status of the Teams message post.

### 6. Customise the agent

- **Modify the KQL query** to suit your telemetry.  For example, you
  might query across multiple tables or focus on specific services.
- **Extend the search index** with additional fields such as severity
  tags, runbook categories or component names.  Update the retrieval
  logic in `function_app/__init__.py` accordingly.
- **Add more remediation actions** by editing the whitelist in
  `remediation/__init__.py` and implementing the corresponding
  automation logic.
- **Adjust the model prompt** in `_build_prompt()` to control how
  verbose the root cause summary is and to tailor the JSON schema.
- **Style the Adaptive Card** by editing `teams_rca_card_template.json`
  or the `_build_adaptive_card()` function to match your brand.

## Contributing

Pull requests are welcome!  If you discover issues or have ideas to
improve the agent—for example integrating with Prometheus or adding
role‑specific summarisation—feel free to open an issue or submit a PR.

## License

This project is licensed under the **GPL‑3.0**.  See the [LICENSE](../LICENSE) file for
details.