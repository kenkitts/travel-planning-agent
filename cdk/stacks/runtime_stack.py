"""RuntimeStack: AgentCore Runtime hosting the Strands travel planning agent.

Deploys the agent/ directory as a code asset (zip-based, no Docker) to
AgentCore Runtime. Bundling pip-installs agent/requirements.txt into the
asset before upload, matching the standard Lambda-style dependency bundling
pattern since AgentRuntimeArtifact.from_code_asset does not do this itself.

Auth is IAM/SigV4 (RuntimeAuthorizerConfiguration.using_iam()) by default —
this is a reversion of the Okta-JWT cutover from DESIGN.md decisions #26-35
(decision #37 in DESIGN.md explains why): once TravelAgentWebStack's OIDC-
authenticated ALB became the human-facing identity boundary, the Runtime's
own JWT authorizer was redundant plumbing rather than a second real trust
boundary. actor_id was passed explicitly in the invocation payload (see
agent/agent.py's get_actor_id()) by the web UI's ECS task, which already
knew who the user was from the ALB's verified OIDC claims — rather than
being derived from a bearer token inside the Runtime.

Phase 2 (auth rearchitecture, added after Phase 1's ALB removal): auth
switches to JWT Bearer Token (RuntimeAuthorizerConfiguration.using_jwt())
when runtime_oidc_discovery_url is provided to this stack's constructor —
still IAM by default when omitted, preserving this stack's standalone
deployability (no Okta prerequisite) for use cases like testing the
Runtime directly via the AgentCore console. AWS's own docs confirm this
is a hard, mutually-exclusive switch, not an additive one: "An AgentCore
Runtime can support either IAM SigV4 or JWT Bearer Token based inbound
auth, but not both simultaneously." When JWT is configured, the caller
(TravelAgentWebStack's web server) must present a Runtime-audienced JWT
obtained via RFC 8693 Token Exchange (see web/auth.py's
exchange_token_for_runtime()) instead of SigV4-signing the call — see
DESIGN.md's Phase 2 section for the full design.

The agent process reads its Gateway URL and Memory ID from environment
variables (GATEWAY_URL, MEMORY_ID) — this stack wires those from the
GatewayStack and MemoryStack outputs, and grants the Runtime's execution
role the permissions it needs to invoke the Bedrock model and call the
Gateway.

OTEL tracing (confirmed against a real deployment, contrary to the "no
config needed for Runtime-hosted agents" framing in the AWS docs — that
framing matches the `agentcore create`/`agentcore deploy` CLI flow, whose
generated template bakes in ADOT; a hand-built code-asset zip via
AgentRuntimeArtifact.from_code_asset does not get that for free) requires
three things together, all present below: (1) aws-opentelemetry-distro in
agent/requirements.txt, (2) the "opentelemetry-instrument" entrypoint
wrapper on agent_runtime_artifact, and (3) the AGENT_OBSERVABILITY_ENABLED
/ OTEL_PYTHON_DISTRO / OTEL_PYTHON_CONFIGURATOR environment variables.
tracing_enabled=True on the Runtime construct is a separate, fourth
requirement — it provisions the traces-delivery pipeline that ships
already-emitted spans to CloudWatch, but does not itself cause any spans
to be emitted.
"""
from pathlib import Path

from aws_cdk import BundlingOptions, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"

# Matches the model ID used as the default in agent/agent.py.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5"


class RuntimeStack(Stack):
    """AgentCore Runtime hosting the travel planning agent, IAM auth by
    default (JWT auth when Okta config is provided — see module docstring)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        gateway: agentcore.IGateway,
        memory: agentcore.IMemory,
        runtime_oidc_discovery_url: str | None = None,
        runtime_oidc_allowed_audience: list[str] | None = None,
        runtime_oidc_allowed_clients: list[str] | None = None,
        runtime_oidc_allowed_scopes: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path=str(AGENT_DIR),
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            # "opentelemetry-instrument" wraps the process with the ADOT auto-
            # instrumentation entrypoint — required for code-asset (zip)
            # AgentCore Runtime deployments to actually emit OTEL spans.
            # Without this wrapper, aws-opentelemetry-distro is present in
            # requirements.txt but never invoked, and the Runtime's `spans`
            # log stream / X-Ray traces stay empty despite tracing_enabled
            # on the CDK Runtime construct (confirmed against a real
            # deployment: that flag only wires the traces-delivery pipeline,
            # not the process instrumentation itself).
            entrypoint=["opentelemetry-instrument", "agent.py"],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "pip install -r requirements.txt -t /asset-output/",
                            "cp -r . /asset-output",
                        ]
                    ),
                ],
            ),
        )

        self.runtime = agentcore.Runtime(
            self,
            "TravelAgentRuntime",
            runtime_name="travel_planning_agent",
            description="Strands travel planning agent (itinerary builder).",
            agent_runtime_artifact=agent_runtime_artifact,
            # Auth is IAM/SigV4 by default (DESIGN.md decision #37,
            # reverting decisions #26-35's Okta JWT cutover) — switches to
            # JWT Bearer Token auth (DESIGN.md's Phase 2 decision) only
            # when runtime_oidc_discovery_url is explicitly provided. This
            # is a hard, mutually-exclusive switch, not additive — AWS's
            # own docs confirm "An AgentCore Runtime can support either
            # IAM SigV4 or JWT Bearer Token based inbound auth, but not
            # both simultaneously" — so RuntimeStack keeps IAM as the
            # default rather than requiring every deployment to configure
            # Okta, preserving this stack's existing standalone
            # deployability (e.g. testing the Runtime directly via the
            # AgentCore console, with no web-hosting/Okta prerequisites
            # at all — see README's "Web UI" section).
            authorizer_configuration=self._build_authorizer_configuration(
                runtime_oidc_discovery_url,
                runtime_oidc_allowed_audience,
                runtime_oidc_allowed_clients,
                runtime_oidc_allowed_scopes,
            ),
            environment_variables={
                "GATEWAY_URL": gateway.gateway_url,
                "MEMORY_ID": memory.memory_id,
                "AWS_REGION": self.region,
                "MODEL_ID": DEFAULT_MODEL_ID,
                # Required alongside the opentelemetry-instrument entrypoint
                # wrapper (see agent_runtime_artifact above) for ADOT to
                # activate AgentCore's GenAI-specific span processing and
                # route telemetry to this Runtime's own CloudWatch log group
                # (spans/otel-rt-logs streams) instead of a no-op exporter.
                "AGENT_OBSERVABILITY_ENABLED": "true",
                "OTEL_PYTHON_DISTRO": "aws_distro",
                "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
            },
            # Creates the traces-delivery pipeline (delivery source ->
            # destination -> delivery) that routes this Runtime's X-Ray
            # trace segments into CloudWatch Logs. This is plumbing only —
            # it does not instrument the agent process itself. The actual
            # span emission comes from the opentelemetry-instrument
            # entrypoint wrapper + AGENT_OBSERVABILITY_ENABLED above.
            # Requires CloudWatch Transaction Search enabled once per
            # account (confirmed already enabled here: TransactionSearchXRayAccess
            # resource policy + trace segment destination = CloudWatchLogs,
            # 100% indexing) for spans to reach the GenAI Observability
            # dashboard.
            tracing_enabled=True,
        )

        # Claude Sonnet invocation for the agent's own reasoning. The "us."
        # cross-region inference profile for Claude Sonnet 5 routes actual
        # model invocations to us-east-1, us-east-2, and us-west-2 (confirmed
        # via bedrock:GetInferenceProfile) — granting foundation-model
        # access only in self.region is insufficient and causes
        # AccessDeniedException on requests routed to the other regions.
        CROSS_REGION_MODEL_REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowBedrockModelInvocation",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/*"
                    for region in CROSS_REGION_MODEL_REGIONS
                ]
                + [f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"],
            )
        )

        # Grant the Runtime's execution role permission to call the Gateway
        # (MCP tool invocation) — matches the AgentCore Runtime -> Gateway
        # IAM auth path used by the agent's MCP client.
        gateway.grant_invoke(self.runtime)

        # Grant the Runtime's execution role permission to read/write
        # AgentCore Memory (short-term events + long-term retrieval).
        memory.grant_full_access(self.runtime)

    @staticmethod
    def _build_authorizer_configuration(
        discovery_url: str | None,
        allowed_audience: list[str] | None,
        allowed_clients: list[str] | None,
        allowed_scopes: list[str] | None,
    ) -> agentcore.RuntimeAuthorizerConfiguration:
        """IAM (default) unless discovery_url is provided, then JWT.

        discovery_url alone is the switch — allowed_audience/allowed_clients/
        allowed_scopes are optional refinements once JWT is selected (the
        JWT authorizer itself requires at least one of allowed_audience/
        allowed_clients/allowed_scopes/custom_claims to be set — enforced
        by AgentCore, not re-validated here).

        allowed_clients validates a `client_id` claim on the presented JWT.
        This project's Okta org previously did not populate that claim —
        Okta placed the client identifier in a `cid` claim instead
        (confirmed live twice: once during the original, later-reverted
        Okta-JWT cutover — decisions #26-35 — and again during Phase 2's
        RFC 8693 token-exchange rollout, where a real exchanged token was
        rejected with "Claim 'client_id' value mismatch with configuration"
        even though `allowedAudience`/`allowedScopes` both matched
        correctly). The client registrations were later reconfigured to
        include a real `client_id` claim, and `cdk/app.py` now wires
        WEB_RUNTIME_OIDC_CLIENT_ID into this parameter again as defense in
        depth on top of allowed_audience/allowed_scopes — narrowing
        acceptance to tokens issued specifically to that app, not just any
        token with a matching audience/scope. If a future Okta org (or a
        reverted claims config) drops the `client_id` claim again, every
        real token will be rejected with the same mismatch error — that's
        the first thing to check if JWT auth suddenly starts failing after
        an Okta-side change.
        """
        if discovery_url is None:
            return agentcore.RuntimeAuthorizerConfiguration.using_iam()
        return agentcore.RuntimeAuthorizerConfiguration.using_jwt(
            discovery_url=discovery_url,
            allowed_clients=allowed_clients,
            allowed_audience=allowed_audience,
            allowed_scopes=allowed_scopes,
        )
