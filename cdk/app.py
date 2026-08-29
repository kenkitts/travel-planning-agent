#!/usr/bin/env python3
"""CDK app entrypoint for the Travel Planning Agent.

Stack wiring:
  ToolsStack   -> weather + places Lambda functions
  GatewayStack -> AgentCore Gateway (Web Search + the two Lambda targets),
                  depends on ToolsStack for the Lambda function references
  MemoryStack  -> AgentCore Memory (short-term + long-term strategies),
                  independent of the other two stacks
  RuntimeStack -> hosts the Strands agent on AgentCore Runtime, depends on
                  both GatewayStack (for the Gateway URL) and MemoryStack
                  (for the Memory ID). Uses IAM/SigV4 auth (DESIGN.md
                  decision #37) — no Okta configuration needed here
                  anymore.
  WebStack     -> hosts the web UI on ECS Fargate behind a plain
                  (non-OIDC) ALB (see DESIGN.md's Phase 1 auth
                  rearchitecture decision, superseding decision #37/#38's
                  ALB-OIDC framing; PLAN.md Phase 10/11), depends on
                  RuntimeStack (for the Runtime ARN) and MemoryStack (for
                  the Memory ID). The web server itself now runs the OIDC
                  flow against Okta directly — its Okta app configuration
                  comes from WEB_* environment variables (see
                  .env.template), loaded from a .env file at the repo
                  root by _load_dotenv() below.
"""
import os
from pathlib import Path

import aws_cdk as cdk

from stacks.gateway_stack import GatewayStack
from stacks.memory_stack import MemoryStack
from stacks.runtime_stack import RuntimeStack
from stacks.tools_stack import ToolsStack
from stacks.web_stack import WebStack


def _load_dotenv(path: Path = None) -> None:
    """Load simple KEY=VALUE lines from a .env file into os.environ.

    Small standalone copy of a loader that once lived in the CLI's
    `agent_client.py` (removed along with the rest of that project's
    Okta-specific plumbing per DESIGN.md decision #37, and later removed
    entirely along with the CLI itself). Only `TravelAgentWebStack`'s
    config (`WEB_CERTIFICATE_ARN`/`WEB_OIDC_*`) still needs a `.env` file,
    so this lives directly in the CDK app rather than as a dependency of
    an unrelated module. Real environment variables already set are never
    overwritten.
    """
    dotenv_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not dotenv_path.is_file():
        return
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

app = cdk.App()

env = cdk.Environment(region="us-east-1")

tools_stack = ToolsStack(app, "TravelAgentToolsStack", env=env)

gateway_stack = GatewayStack(
    app,
    "TravelAgentGatewayStack",
    env=env,
    weather_function=tools_stack.weather_function,
    places_function=tools_stack.places_function,
)
gateway_stack.add_dependency(tools_stack)

memory_stack = MemoryStack(app, "TravelAgentMemoryStack", env=env)

runtime_stack = RuntimeStack(
    app,
    "TravelAgentRuntimeStack",
    env=env,
    gateway=gateway_stack.gateway,
    memory=memory_stack.memory,
)
runtime_stack.add_dependency(gateway_stack)
runtime_stack.add_dependency(memory_stack)

# WebStack is optional: it needs a real ACM certificate and a real Okta
# OIDC app registered for this web server (DESIGN.md's Phase 1 auth
# rearchitecture decision — neither of which this CDK app can create
# itself), so it's only constructed if that configuration is actually
# present. Running `cdk deploy --all` (or synth) without
# WEB_CERTIFICATE_ARN set still deploys/synths the other four stacks
# normally.
web_certificate_arn = os.environ.get("WEB_CERTIFICATE_ARN")
if web_certificate_arn:
    required_web_vars = {
        "WEB_HOSTNAME": os.environ.get("WEB_HOSTNAME"),
        "WEB_OIDC_ISSUER": os.environ.get("WEB_OIDC_ISSUER"),
        "WEB_OIDC_AUTHORIZATION_ENDPOINT": os.environ.get("WEB_OIDC_AUTHORIZATION_ENDPOINT"),
        "WEB_OIDC_TOKEN_ENDPOINT": os.environ.get("WEB_OIDC_TOKEN_ENDPOINT"),
        "WEB_OIDC_CLIENT_ID": os.environ.get("WEB_OIDC_CLIENT_ID"),
        "WEB_OIDC_CLIENT_SECRET": os.environ.get("WEB_OIDC_CLIENT_SECRET"),
    }
    missing = [name for name, value in required_web_vars.items() if not value]
    if missing:
        raise RuntimeError(
            f"WEB_CERTIFICATE_ARN is set, but missing: {', '.join(missing)} "
            "(see .env.template) — all WEB_* vars are required together to "
            "deploy TravelAgentWebStack."
        )

    web_stack = WebStack(
        app,
        "TravelAgentWebStack",
        env=env,
        runtime=runtime_stack.runtime,
        memory=memory_stack.memory,
        certificate_arn=web_certificate_arn,
        web_hostname=required_web_vars["WEB_HOSTNAME"],
        oidc_issuer=required_web_vars["WEB_OIDC_ISSUER"],
        oidc_authorization_endpoint=required_web_vars["WEB_OIDC_AUTHORIZATION_ENDPOINT"],
        oidc_token_endpoint=required_web_vars["WEB_OIDC_TOKEN_ENDPOINT"],
        oidc_client_id=required_web_vars["WEB_OIDC_CLIENT_ID"],
        oidc_client_secret=required_web_vars["WEB_OIDC_CLIENT_SECRET"],
    )
    web_stack.add_dependency(runtime_stack)
    web_stack.add_dependency(memory_stack)

# Applied to every taggable resource across all deployed stacks.
cdk.Tags.of(app).add("auto-delete", "no")
cdk.Tags.of(app).add("project", "travel-planning-agent")

app.synth()
