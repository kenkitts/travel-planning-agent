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
                  (for the Memory ID). Its Okta JWT authorizer config
                  (DESIGN.md decisions #26-35) comes from OKTA_ISSUER and
                  OKTA_AUDIENCE environment variables — the same .env this
                  project's own cli/agent_client.py reads (see
                  .env.template) — rather than being hardcoded here, so the
                  same repo works against any Okta org/app without a code
                  change. OKTA_AUDIENCE (not OKTA_CLIENT_ID) is what the
                  Runtime authorizer actually validates against — see
                  runtime_stack.py's authorizer_configuration comment for
                  why allowedAudience is used instead of allowedClients for
                  Okta-issued tokens.
"""
import os
import sys
from pathlib import Path

import aws_cdk as cdk

from stacks.gateway_stack import GatewayStack
from stacks.memory_stack import MemoryStack
from stacks.runtime_stack import RuntimeStack
from stacks.tools_stack import ToolsStack

# Reuse cli/agent_client.py's tiny .env loader instead of duplicating it,
# so `cdk deploy` picks up the same OKTA_ISSUER/OKTA_CLIENT_ID as the CLI/
# web UI clients from one shared .env at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
from agent_client import load_dotenv  # noqa: E402

load_dotenv()

app = cdk.App()

env = cdk.Environment(region="us-east-1")

okta_issuer = os.environ.get("OKTA_ISSUER")
okta_audience = os.environ.get("OKTA_AUDIENCE")
if not okta_issuer or not okta_audience:
    raise RuntimeError(
        "OKTA_ISSUER and OKTA_AUDIENCE must be set (see .env.template) to deploy "
        "TravelAgentRuntimeStack's JWT authorizer configuration."
    )

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
    okta_issuer=okta_issuer,
    okta_audience=okta_audience,
)
runtime_stack.add_dependency(gateway_stack)
runtime_stack.add_dependency(memory_stack)

# Applied to every taggable resource across all four stacks.
cdk.Tags.of(app).add("auto-delete", "no")
cdk.Tags.of(app).add("project", "travel-planning-agent")

app.synth()
