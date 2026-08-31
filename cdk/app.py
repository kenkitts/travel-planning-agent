#!/usr/bin/env python3
"""CDK app entrypoint for the Travel Planning Agent.

Stack wiring:
  ToolsStack   -> weather + places Lambda functions
  GatewayStack -> AgentCore Gateway (Web Search + the two Lambda targets),
                  depends on ToolsStack for the Lambda function references.
                  Auth is IAM-only by default — switches to JWT Bearer
                  Token auth (DESIGN.md's Phase 3 decision) only when the
                  GATEWAY_OIDC_* environment variables are present,
                  independently of whether WebStack is deployed this run
                  (mirrors RuntimeStack's own optional-JWT pattern below).
                  When JWT is configured, also provisions a
                  CfnOAuth2CredentialProvider for RFC 8693 On-Behalf-Of
                  token exchange — see gateway_stack.py's module docstring.
  MemoryStack  -> AgentCore Memory (short-term + long-term strategies),
                  independent of the other two stacks
  RuntimeStack -> hosts the Strands agent on AgentCore Runtime, depends on
                  both GatewayStack (for the Gateway URL and, once
                  GATEWAY_OIDC_* is configured, the OAuth2 credential
                  provider it needs to grant OBO-exchange permission
                  against) and MemoryStack (for the Memory ID). Uses
                  IAM/SigV4 auth by default (DESIGN.md decision #37) —
                  switches to JWT Bearer Token auth (DESIGN.md's Phase 2
                  decision) only when the WEB_RUNTIME_OIDC_* environment
                  variables are present, independently of whether WebStack
                  itself is being deployed this run (RuntimeStack has no
                  hard dependency on WEB_CERTIFICATE_ARN/WEB_OIDC_* — see
                  WebStack's own note below).
  WebStack     -> hosts the web UI on ECS Fargate behind a plain
                  (non-OIDC) ALB (see DESIGN.md's Phase 1 auth
                  rearchitecture decision, superseding decision #37/#38's
                  ALB-OIDC framing; PLAN.md Phase 10/11), depends on
                  RuntimeStack (for the Runtime ARN) and MemoryStack (for
                  the Memory ID). The web server itself now runs the OIDC
                  flow against Okta directly — its Okta app configuration
                  comes from WEB_* environment variables (see
                  .env.template), loaded from a .env file at the repo
                  root by _load_dotenv() below. WEB_RUNTIME_OIDC_* (a
                  second, separate Okta app used for RFC 8693 Token
                  Exchange — DESIGN.md's Phase 2 decision) is required
                  together with the rest of WEB_* whenever
                  WEB_CERTIFICATE_ARN is set, since the web server cannot
                  call the Runtime at all without it once the Runtime's
                  own authorizer is switched to JWT (see above) — these
                  are the *same* WEB_RUNTIME_OIDC_* values RuntimeStack
                  reads independently above; they must describe the same
                  Okta app/audience/scope for the exchanged token to be
                  accepted by the Runtime at all.
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

# GatewayStack's JWT authorizer is optional and independent of WebStack —
# see this module's docstring. Same discovery-URL-derivation convention as
# RuntimeStack's WEB_RUNTIME_OIDC_ISSUER handling below, applied to a
# third, separate Okta "API Services" app dedicated to this exchange
# (distinct from the web-login app and Phase 2's Runtime-exchange app —
# three Okta apps total across this project's auth rearchitecture).
gateway_oidc_issuer = os.environ.get("GATEWAY_OIDC_ISSUER")
gateway_oidc_discovery_url = None
gateway_oidc_allowed_audience = None
gateway_oidc_allowed_clients = None
gateway_oidc_allowed_scopes = None
gateway_oidc_client_id = os.environ.get("GATEWAY_OIDC_CLIENT_ID")
gateway_oidc_client_secret = os.environ.get("GATEWAY_OIDC_CLIENT_SECRET")
if gateway_oidc_issuer:
    gateway_oidc_discovery_url = (
        gateway_oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    )
    gateway_oidc_audience = os.environ.get("GATEWAY_OIDC_AUDIENCE")
    gateway_oidc_scope = os.environ.get("GATEWAY_OIDC_SCOPE")
    if gateway_oidc_audience:
        gateway_oidc_allowed_audience = [gateway_oidc_audience]
    if gateway_oidc_client_id:
        gateway_oidc_allowed_clients = [gateway_oidc_client_id]
    if gateway_oidc_scope:
        gateway_oidc_allowed_scopes = [gateway_oidc_scope]

gateway_stack = GatewayStack(
    app,
    "TravelAgentGatewayStack",
    env=env,
    weather_function=tools_stack.weather_function,
    places_function=tools_stack.places_function,
    gateway_oidc_discovery_url=gateway_oidc_discovery_url,
    gateway_oidc_allowed_audience=gateway_oidc_allowed_audience,
    gateway_oidc_allowed_clients=gateway_oidc_allowed_clients,
    gateway_oidc_allowed_scopes=gateway_oidc_allowed_scopes,
    gateway_oidc_client_id=gateway_oidc_client_id,
    gateway_oidc_client_secret=gateway_oidc_client_secret,
)
gateway_stack.add_dependency(tools_stack)

memory_stack = MemoryStack(app, "TravelAgentMemoryStack", env=env)

# RuntimeStack's JWT authorizer is optional and independent of WebStack —
# see this module's docstring. When present, WEB_RUNTIME_OIDC_ISSUER is
# used to derive discovery_url per the OIDC convention (Okta and most
# providers serve discovery metadata at
# {issuer}/.well-known/openid-configuration — confirmed against Phase 1's
# own WEB_OIDC_* setup, which uses explicit endpoint vars rather than a
# single discovery_url specifically to avoid this kind of derived-URL
# assumption for its own config; RuntimeStack's JWT authorizer construct
# itself requires a discovery_url shape, unlike Phase 1's ALB/web-server
# OIDC config, so there's no equivalent explicit-endpoints option here).
#
# allowed_clients validates a `client_id` claim on the exchanged JWT,
# narrowing acceptance to tokens issued specifically to the
# WEB_RUNTIME_OIDC_CLIENT_ID app (defense in depth on top of
# allowed_audience/allowed_scopes). Previously NOT wired here — confirmed
# live that this Okta org's issued tokens carried the client identifier
# in a `cid` claim, not `client_id` (the same mismatch found once during
# the original, later-reverted Okta-JWT cutover, decisions #26-35, and
# again during Phase 2's rollout). Re-enabled now that the org's client
# registrations have been reconfigured to include a real `client_id`
# claim — see runtime_stack.py's _build_authorizer_configuration()
# docstring for the fuller history.
runtime_oidc_issuer = os.environ.get("WEB_RUNTIME_OIDC_ISSUER")
runtime_oidc_discovery_url = None
runtime_oidc_allowed_audience = None
runtime_oidc_allowed_clients = None
runtime_oidc_allowed_scopes = None
if runtime_oidc_issuer:
    runtime_oidc_discovery_url = (
        runtime_oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    )
    runtime_oidc_audience = os.environ.get("WEB_RUNTIME_OIDC_AUDIENCE")
    runtime_oidc_client_id = os.environ.get("WEB_RUNTIME_OIDC_CLIENT_ID")
    runtime_oidc_scope = os.environ.get("WEB_RUNTIME_OIDC_SCOPE")
    if runtime_oidc_audience:
        runtime_oidc_allowed_audience = [runtime_oidc_audience]
    if runtime_oidc_client_id:
        runtime_oidc_allowed_clients = [runtime_oidc_client_id]
    if runtime_oidc_scope:
        runtime_oidc_allowed_scopes = [runtime_oidc_scope]

runtime_stack = RuntimeStack(
    app,
    "TravelAgentRuntimeStack",
    env=env,
    gateway=gateway_stack.gateway,
    memory=memory_stack.memory,
    runtime_oidc_discovery_url=runtime_oidc_discovery_url,
    runtime_oidc_allowed_audience=runtime_oidc_allowed_audience,
    runtime_oidc_allowed_clients=runtime_oidc_allowed_clients,
    runtime_oidc_allowed_scopes=runtime_oidc_allowed_scopes,
    gateway_oauth2_credential_provider=gateway_stack.oauth2_credential_provider,
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
        # Phase 2: a second, separate Okta app used for RFC 8693 Token
        # Exchange (DESIGN.md's Phase 2 decision) — required together
        # with the rest of WEB_* since /api/chat cannot call the Runtime
        # at all without it once RuntimeStack's authorizer is switched to
        # JWT (see this module's docstring).
        "WEB_RUNTIME_OIDC_ISSUER": os.environ.get("WEB_RUNTIME_OIDC_ISSUER"),
        "WEB_RUNTIME_OIDC_TOKEN_ENDPOINT": os.environ.get("WEB_RUNTIME_OIDC_TOKEN_ENDPOINT"),
        "WEB_RUNTIME_OIDC_CLIENT_ID": os.environ.get("WEB_RUNTIME_OIDC_CLIENT_ID"),
        "WEB_RUNTIME_OIDC_CLIENT_SECRET": os.environ.get("WEB_RUNTIME_OIDC_CLIENT_SECRET"),
        "WEB_RUNTIME_OIDC_AUDIENCE": os.environ.get("WEB_RUNTIME_OIDC_AUDIENCE"),
        "WEB_RUNTIME_OIDC_SCOPE": os.environ.get("WEB_RUNTIME_OIDC_SCOPE"),
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
        runtime_oidc_issuer=required_web_vars["WEB_RUNTIME_OIDC_ISSUER"],
        runtime_oidc_token_endpoint=required_web_vars["WEB_RUNTIME_OIDC_TOKEN_ENDPOINT"],
        runtime_oidc_client_id=required_web_vars["WEB_RUNTIME_OIDC_CLIENT_ID"],
        runtime_oidc_client_secret=required_web_vars["WEB_RUNTIME_OIDC_CLIENT_SECRET"],
        runtime_oidc_audience=required_web_vars["WEB_RUNTIME_OIDC_AUDIENCE"],
        runtime_oidc_scope=required_web_vars["WEB_RUNTIME_OIDC_SCOPE"],
    )
    web_stack.add_dependency(runtime_stack)
    web_stack.add_dependency(memory_stack)

# Applied to every taggable resource across all deployed stacks.
cdk.Tags.of(app).add("auto-delete", "no")
cdk.Tags.of(app).add("project", "travel-planning-agent")

app.synth()
