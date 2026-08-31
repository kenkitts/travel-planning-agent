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

Phase 3 (Gateway/OBO auth rearchitecture, added after Phase 2): the
Runtime's execution role no longer has `gateway.grant_invoke()`'s IAM
`InvokeGateway`/SigV4 permission — GatewayStack's own authorizer switches
to JWT-only once configured (same AWS mutual-exclusivity constraint as the
Runtime's own inbound auth), so that IAM grant would be dead permission
once the Gateway requires JWT (same anti-pattern already found and fixed
once for the Runtime's own IAM grant — see decision #78). Instead, the
Runtime's execution role is granted `bedrock-agentcore:GetResourceOauth2Token`
— used by agent.py's build_mcp_client() (via bedrock_agentcore.identity.auth's
IdentityClient) to perform the actual RFC 8693 On-Behalf-Of token exchange
and obtain a Gateway-audienced, per-user JWT. Confirmed live across two
separate AccessDeniedException iterations that this action authorizes
against THREE resource families simultaneously and IAM requires grants on
ALL of them: the credential provider itself, the account's default
token-vault resource (`token-vault/default` — distinct from, and required
in addition to, the credential-provider-within-the-vault ARN; the first
deploy omitted this and failed specifically on this resource), AND the
Runtime's own workload identity
(`workload-identity-directory/default/workload-identity/<agent_runtime_id>`
— the very first deploy omitted this one too). Granting a subset clears
whichever checks that subset covers, then fails AccessDeniedException on
whichever resource is still ungranted — each iteration surfaced exactly
one missing resource at a time. Uses a `{RUNTIME_NAME}-*` wildcard rather than
`self.runtime.agent_runtime_id` (the exact resolved ID) because the latter
is a `Fn::GetAtt` on the Runtime resource itself — referencing it from a
policy statement attached to the Runtime's own execution role creates a
genuine circular CloudFormation dependency (confirmed live:
"Circular dependency between resources: [TravelAgentRuntime...,
TravelAgentRuntimeExecutionRoleDefaultPolicy...]"), since the role must
exist before the Runtime can be created, but the exact ID isn't known
until the Runtime *is* created. The wildcard sidesteps this entirely
(matches the same pattern used by other AgentCore CDK stacks internally
for this exact reason) since `RUNTIME_NAME` is a static, synth-time-known
string, and AWS always appends its random suffix after it. Deliberately
NOT granted:
`GetWorkloadAccessToken`/`GetWorkloadAccessTokenForJWT`/
`GetWorkloadAccessTokenForUserId` — AWS's own docs confirm "Runtime-managed
agent identities cannot retrieve workload access tokens directly,
preventing token extraction and misuse"; the workload access token this
exchange consumes is automatically minted and delivered to agent code via
the `WorkloadAccessToken` request header (read via
`BedrockAgentCoreContext.get_workload_access_token()`), never fetched by
agent code itself. Granting those actions would be the same kind of
misleading, unusable permission decision #78 already removed once.
GatewayStack's credential-provider name is passed to the Runtime as the
`GATEWAY_OBO_PROVIDER_NAME` environment variable so agent.py can reference
it without hardcoding.

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

# The Runtime's own base name (before AWS appends its random "-XXXXXXXXXX"
# suffix to form the actual agent_runtime_id/workload-identity name) — see
# the OBO IAM grant below for why this is needed as a wildcard prefix
# rather than the resolved agent_runtime_id.
RUNTIME_NAME = "travel_planning_agent"


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
        gateway_oauth2_credential_provider: agentcore.CfnOAuth2CredentialProvider | None = None,
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
                            # Strip any __pycache__ directories that came
                            # along with the plain `cp -r` above. Without
                            # this, a stale *.pyc left on disk from a local
                            # `python`/pytest run (bytecode caches aren't
                            # gitignored from Docker's perspective — they
                            # exist as real files at bundle time) gets
                            # copied straight into the deployed asset.
                            # Confirmed live: this caused the Runtime to
                            # silently execute a stale cached agent.py
                            # bytecode for multiple deploys in a row, with
                            # zero difference in the deployed *source* file
                            # (byte-identical, confirmed via diff) — new
                            # code changes appeared to have "no effect"
                            # because they were never actually running.
                            "find /asset-output -depth -type d -name __pycache__ -exec rm -rf {} +",
                        ]
                    ),
                ],
            ),
        )

        self.runtime = agentcore.Runtime(
            self,
            "TravelAgentRuntime",
            runtime_name=RUNTIME_NAME,
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
            # Required alongside CUSTOM_JWT auth for agent.py to ever see the
            # inbound Authorization header at all — confirmed live (Gateway
            # OBO cache diagnostic logging showed
            # "header keys present=['baggage', 'workloadaccesstoken']", no
            # 'Authorization' key) that AWS's own runtime.app.py forwarding
            # code (which does special-case Authorization unconditionally,
            # unlike other headers) only ever sees whatever the AgentCore
            # control plane already decided to hand it — and the control
            # plane gates that on requestHeaderAllowlist explicitly, per
            # AWS's "Pass custom headers to Amazon Bedrock AgentCore
            # Runtime" doc ("With this configuration, the Authorization
            # header from incoming requests is validated against your OIDC
            # provider and forwarded to your agent code"). Without this,
            # _sub_from_authorization_header() always returns None, so
            # get_actor_id()'s JWT `sub`-claim path silently never
            # activates (falling through to the DEFAULT_ACTOR_ID/payload
            # fallback) and the Gateway OBO token cache never gets a real
            # cache key to cache under. Only set when JWT auth is actually
            # configured — there's no Authorization header to allowlist
            # under the IAM/SigV4 default, and requesting it unconditionally
            # would be a no-op at best.
            request_header_configuration=(
                agentcore.RequestHeaderConfiguration(allowlisted_headers=["Authorization"])
                if runtime_oidc_discovery_url
                else None
            ),
            environment_variables={
                "GATEWAY_URL": gateway.gateway_url or "",
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
                # Phase 3: name of GatewayStack's OAuth2 credential provider
                # (see gateway_stack.py's GATEWAY_OBO_PROVIDER_NAME) —
                # agent.py's build_mcp_client() passes this to
                # IdentityClient.get_token(provider_name=...) for the OBO
                # exchange. Empty string (not omitted) when the Gateway's
                # JWT auth isn't configured, so build_mcp_client() can
                # detect "OBO not configured" the same way it already
                # detects "GATEWAY_URL not configured" — via a falsy env
                # var, not a KeyError.
                "GATEWAY_OBO_PROVIDER_NAME": (
                    gateway_oauth2_credential_provider.name
                    if gateway_oauth2_credential_provider is not None
                    else ""
                ),
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

        # Grant the Runtime's execution role permission to read/write
        # AgentCore Memory (short-term events + long-term retrieval).
        memory.grant_full_access(self.runtime)

        # Phase 3: grant permission to perform the OBO token exchange
        # against GatewayStack's credential provider — see this module's
        # docstring for why this replaces gateway.grant_invoke()'s IAM
        # SigV4 grant (removed above) and why GetWorkloadAccessToken* is
        # deliberately NOT granted alongside this.
        if gateway_oauth2_credential_provider is not None:
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="AllowGatewayOboTokenExchange",
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock-agentcore:GetResourceOauth2Token"],
                    resources=[
                        gateway_oauth2_credential_provider.attr_credential_provider_arn,
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:"
                        "token-vault/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:"
                        "workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:"
                        "workload-identity-directory/default/workload-identity/"
                        f"{RUNTIME_NAME}-*",
                    ],
                )
            )
            # AgentCore Identity stores each OAuth2 credential provider's
            # client secret in a Secrets Manager secret it creates and
            # names itself, under a fixed prefix
            # ("bedrock-agentcore-identity!default/oauth2/<provider-name>-*"
            # — confirmed live from the actual denied ARN, which had a
            # random suffix appended to GATEWAY_OBO_PROVIDER_NAME). The
            # GetResourceOauth2Token call above reads this secret
            # server-side to complete the exchange, so the execution role
            # needs read access to it directly — this is a distinct check
            # from the four bedrock-agentcore:* resources above (confirmed
            # live: granting all four still failed on this secret specifically).
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="AllowGatewayOboSecretAccess",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                        "secret:bedrock-agentcore-identity!default/oauth2/"
                        f"{gateway_oauth2_credential_provider.name}-*"
                    ],
                )
            )

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
