import json
import azure.functions as func

"""
This lightweight HTTP triggered function executes safe remediation actions
requested by the AIOps agent.  It implements a simple policy that only
allows known actions, acting as a guardrail against arbitrary command
execution.  Each accepted action should correspond to a script or API
call in your infrastructure.  Replace the TODO section with real
implementations (e.g. Azure CLI calls, SDK operations or integration
with your own automation platform).
"""

# Define the whitelist of safe actions.
SAFE_ACTIONS: set[str] = {"scale_db", "toggle_feature_flag", "restart_service"}


async def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Invalid JSON."}),
            mimetype="application/json",
            status_code=400,
        )

    action = data.get("action")
    params = data.get("params", {})

    if not action or action not in SAFE_ACTIONS:
        return func.HttpResponse(
            json.dumps({
                "status": "denied",
                "reason": "unsafe or unknown action",
                "allowed_actions": list(SAFE_ACTIONS),
            }),
            mimetype="application/json",
            status_code=403,
        )

    # TODO: implement real remediation logic for each action.  The
    # implementation below simply echoes the input as a success.
    # Example: if action == "scale_db": call ARM or CLI to scale
    # database; if action == "toggle_feature_flag": update config in
    # App Configuration or LaunchDarkly, etc.
    result = {
        "status": "ok",
        "action": action,
        "params": params,
        "message": "Action executed (simulation).",
    }

    return func.HttpResponse(
        json.dumps(result),
        mimetype="application/json",
    )