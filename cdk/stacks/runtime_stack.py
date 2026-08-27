"""RuntimeStack: AgentCore Runtime hosting the Strands travel planning agent.

Deploys the agent/ directory as a code asset (zip-based, no Docker) to
AgentCore Runtime. Bundling pip-installs agent/requirements.txt into the
asset before upload, matching the standard Lambda-style dependency bundling
pattern since AgentRuntimeArtifact.from_code_asset does not do this itself.

Auth is Okta-issued JWT bearer tokens via RuntimeAuthorizerConfiguration.using_jwt()
(DESIGN.md decisions #26-35), superseding the original IAM-only auth
(decision #15). `requestHeaderAllowlist=["Authorization"]` forwards the
raw bearer token to the agent process via `context.request_headers` —
confirmed against AWS's own docs (inbound-jwt-authorizer.html,
runtime-header-allowlist.html): the JWT authorizer alone validates the
token at the edge but does not automatically inject decoded claims into
the invocation context, so `agent/agent.py` decodes the forwarded header
itself (via PyJWT, signature verification skipped since the Runtime
authorizer already did it) to derive `actor_id` from the `sub` claim.

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
import os
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
    """AgentCore Runtime hosting the travel planning agent, Okta JWT auth."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        gateway: agentcore.IGateway,
        memory: agentcore.IMemory,
        okta_issuer: str,
        okta_audience: str,
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
            # Okta-issued JWT bearer tokens (DESIGN.md decisions #26-35),
            # replacing IAM-only auth (decision #15) entirely — AgentCore
            # Runtime supports one authorizer configuration at a time, so
            # this is a full cutover, not an additional option. discoveryUrl
            # must end with /.well-known/openid-configuration.
            #
            # allowed_audience (not allowed_clients) is used deliberately:
            # Okta access tokens carry the client identifier in a `cid`
            # claim, not the standard OAuth `client_id` claim AgentCore's
            # allowedClients check validates against (confirmed against a
            # real decoded Okta access token during this project's own
            # deployment — client_id was absent entirely) — allowedClients
            # would silently reject every real Okta token. allowedAudience
            # checks the `aud` claim instead, which Okta's authorization
            # server sets from its own configured Audience field — set to
            # the static string "api://travel-planning-agent" (Okta Admin
            # Console: Security > API > Authorization Servers > Settings),
            # not the Runtime's own ARN, so a Runtime recreation (new
            # auto-generated ARN suffix) can never invalidate already-
            # issued/cached tokens.
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                discovery_url=f"{okta_issuer}/.well-known/openid-configuration",
                allowed_audience=[okta_audience],
            ),
            # Forwards the raw Authorization header to the agent process via
            # context.request_headers, so agent/agent.py can decode the
            # already-validated JWT itself to read the `sub` claim (decision
            # #34) — this is NOT automatic from the JWT authorizer alone.
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=["Authorization"],
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
