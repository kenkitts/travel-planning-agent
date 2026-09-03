"""GatewayStack: AgentCore Gateway exposing the agent's tools over MCP.

Creates a single AgentCore Gateway with three targets:
  - Web Search (AWS-managed MCP connector, connectorId="web-search")
  - Lambda target wrapping the weather tool (Open-Meteo)
  - Lambda target wrapping the places tool (Amazon Location Service)

Auth was IAM-only (design decision #15) via GatewayAuthorizer.using_aws_iam()
through Phase 3's Gateway/OBO auth rearchitecture. It now switches to JWT
Bearer Token auth (GatewayAuthorizer.using_custom_jwt()) as an OPTIONAL
config, mirroring RuntimeStack's own optional-JWT pattern: IAM remains the
default when gateway_oidc_discovery_url is omitted, switching to JWT only
when it's provided. AWS's own docs confirm the Runtime/Gateway JWT-vs-IAM
mutual exclusivity applies identically here — "You can configure your agent
runtime... or gateway... to accept JWT bearer tokens" (same
CustomJWTAuthorizerConfiguration shape for both resource types). When JWT
is configured, the caller (this project's Runtime, via agent.py's
build_mcp_client()) must present a Gateway-audienced JWT obtained via
RFC 8693 On-Behalf-Of token exchange (AgentCore Identity's
GetResourceOauth2Token, ON_BEHALF_OF_TOKEN_EXCHANGE flow) rather than
SigV4-signing the call — see DESIGN.md's Phase 3 section for the full
design, and _add_oauth2_credential_provider() below for the exchange
infrastructure.

Credential access from the Gateway to each Lambda target uses the Gateway's
own execution role (GATEWAY_IAM_ROLE credential provider type), which is the
default AgentCore pattern for Lambda targets owned by the same account. This
is unrelated to (and unaffected by) the Gateway's own *inbound* authorizer
switching to JWT above — GATEWAY_IAM_ROLE is about how the Gateway calls
*out* to its own Lambda targets, not how callers authenticate *into* the
Gateway.

For the Lambda targets, `add_lambda_target` automatically grants the
Gateway's role `lambda:InvokeFunction` on the target function. The Web
Search connector target has no such automatic grant — the Gateway's role
must be explicitly given `bedrock-agentcore:InvokeGateway` (to dispatch
through itself) and `bedrock-agentcore:InvokeWebSearch` (on the AWS-owned
web-search tool ARN), or every web search call fails at runtime with
"Execution role is not authorized for connector web-search" even though the
target and gateway both show status READY. This exact two-statement policy
is AWS's documented setup for the Web Search connector's gateway service
role (see the "Configure the Gateway Service Role" section of the Web
Search connector target docs) — confirmed against a live deployment.

Also configures CloudWatch observability (application logs + X-Ray traces)
for the Gateway. AgentCore does NOT enable this by default for gateway
resources (unlike Runtime, which gets a log group automatically) — without
it, tool call failures (e.g. "An internal error occurred. Please retry
later." from a target) are opaque, with no way to see the actual upstream
error, request/response bodies, or trace/span IDs. Wired up per AWS's
documented SDK pattern (Add observability to your Amazon Bedrock AgentCore
resources): a dedicated log group, one delivery source each for
APPLICATION_LOGS and TRACES on the Gateway's own ARN, a CloudWatch Logs
destination for the former and an X-Ray destination for the latter (X-Ray
is the only supported trace destination type; CloudWatch Transaction
Search must be enabled once per account/region for X-Ray to actually land
spans in CloudWatch — see README's Observability section), and a
CfnDelivery connecting each source to its destination.

Gateway-routed inference (a fourth target, added alongside the three tool
targets above): a `bedrock-mantle` connector inference target — the
route to a hand-rolled `provider`-type target against `bedrock-runtime`
(the originally-attempted approach; still findable in this file's git
history) was abandoned after two successive live-deploy failures: (1) a
Coral `UnknownOperationException` from an incorrect `providerPath`
(`bedrock-runtime`'s Anthropic-native route is
`/anthropic/v1/messages`, not `/v1/messages`, which is `bedrock-mantle`'s
own convention that every documented provider-target example happens to
use), then (2) once that path was corrected, a real Anthropic `401`
("Credential should be scoped to correct service: 'bedrock'") — this
Gateway's `GATEWAY_IAM_ROLE` outbound SigV4 signing has no way to know it
should sign as service `bedrock` for that specific route, and every one
of AWS's own documented examples for `bedrock-runtime`'s Anthropic-native
path use a Bedrock API key, not a hand-signed SigV4 request through a
generic HTTP proxy. Rather than provisioning a new Bedrock API key +
AgentCore Identity `API_KEY` credential provider (a new secret/credential
to manage, changing this feature's security posture), switched to the
built-in `bedrock-mantle` connector (`connector_id="bedrock-mantle"`):
AWS's own purpose-built integration handles the endpoint/path/auth
wiring internally instead of this project hand-rolling it. This does
still need its own, distinct IAM grant — a `bedrock-mantle` connector
target genuinely calls `bedrock-mantle:ListModels`/`CreateInference`/
`CallWithBearerToken` (confirmed against AWS's own
`AmazonBedrockMantleInferenceAccess` managed policy, granted here as
individually-listed actions rather than that policy's wildcards) on top
of the `bedrock:InvokeModel`/`InvokeModelWithResponseStream` grant this
target always needed — this Gateway's role had zero Bedrock permissions
of any kind before this. This target is additive and always created;
agent.py's own use of it is opt-in via GATEWAY_INFERENCE_URL (see
agent.py and DESIGN.md's Gateway-routed-inference decision) — rate
limiting/RBAC policy on top of this target is deliberately deferred to a
fast-follow, not part of this pass.

OAuth2 credential provider (Phase 3, added when Gateway JWT auth is
configured): a CfnOAuth2CredentialProvider resource ("CustomOauth2" vendor)
pointing at a dedicated Okta "API Services" app configured for the Token
Exchange grant type — distinct from both the web-login app and Phase 2's
Runtime-exchange app (three separate Okta apps total across this project's
auth rearchitecture). `onBehalfOfTokenExchangeConfig` is set to
`grantType: TOKEN_EXCHANGE` with `actorTokenContent: NONE` — this Okta org's
Token Exchange grant does not expect or support a separate `actor_token`
(confirmed against Phase 2's own token-exchange call, which authenticates
purely via HTTP Basic client-credential auth + `subject_token`, no actor
token at all). The client secret is passed as a plain literal
(`client_secret`, not `client_secret_config`'s Secrets Manager reference)
— confirmed live that `CreateOauth2CredentialProvider` rejects
`clientSecretConfig` for `CLIENT_SECRET_BASIC`/`CLIENT_SECRET_POST` client
authentication with `"clientSecret is required..."`, regardless of
`client_authentication_method` being set explicitly; this matches how
`WebStack`'s own `OidcClientSecret`/`RuntimeOidcClientSecret` are already
handled (a plain literal via `SecretValue.unsafe_plain_text()`, not a
reference) — the value still ultimately comes from `.env` (loaded by
`cdk/app.py`), not a hardcoded literal in source. agent.py's
build_mcp_client() calls AgentCore Identity's
GetResourceOauth2Token (via bedrock_agentcore.identity.auth's
IdentityClient/requires_access_token, ON_BEHALF_OF_TOKEN_EXCHANGE flow)
against this provider by name, using the workload access token AgentCore
Runtime already delivers automatically — no manual GetWorkloadAccessToken*
call in agent code (see RuntimeStack's docstring for why that permission
is deliberately NOT granted to the Runtime's execution role).
"""
from pathlib import Path

from aws_cdk import Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "lambdas"

WEB_SEARCH_CONNECTOR_ID = "web-search"
WEB_SEARCH_CONFIGURATION_NAME = "WebSearch"
GATEWAY_LOG_GROUP_NAME = "/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/travel-planning-agent-gateway"

# Name of the OAuth2 credential provider agent.py references by name (via
# GATEWAY_OBO_PROVIDER_NAME, wired through RuntimeStack's environment
# variables — see cdk/app.py) when performing the OBO token exchange.
GATEWAY_OBO_PROVIDER_NAME = "travel-planning-agent-gateway-obo"

# Default per-user token-per-minute budget for the inference target. A
# starting value, not a load-tested ceiling — see _add_inference_rate_limit()
# for the rationale. AWS's own Gateway rate-limit docs note token limits use
# budget-based enforcement (estimated pre-call, reconciled post-call against
# actual usage), so this is a soft, best-effort cap, not a hard per-request
# guarantee.
INFERENCE_TOKENS_PER_MINUTE_PER_USER = 50_000


class GatewayStack(Stack):
    """AgentCore Gateway with Web Search + weather + places targets."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        weather_function: lambda_.IFunction,
        places_function: lambda_.IFunction,
        model_id: str,
        gateway_oidc_discovery_url: str | None = None,
        gateway_oidc_allowed_audience: list[str] | None = None,
        gateway_oidc_allowed_clients: list[str] | None = None,
        gateway_oidc_allowed_scopes: list[str] | None = None,
        gateway_oidc_client_id: str | None = None,
        gateway_oidc_client_secret: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Construct ID is "...V2", not "TravelAgentGateway" — this is the
        # real, permanent logical ID after Phase 3's live cutover, not a
        # placeholder to rename back. AWS's Gateway API forbids changing
        # an existing Gateway's authorizerType in place ("Authorizer type
        # cannot be updated for an existing gateway"), so switching this
        # Gateway from IAM to JWT required CloudFormation to fully
        # replace it — which only happens if the logical ID changes (a
        # same-ID property change is treated as an in-place update
        # attempt, which the AWS API then rejects). Renaming this again
        # later would force another full replacement (new gatewayId, all
        # three targets recreated) for no functional reason — keep this
        # ID as-is going forward.
        self.gateway = agentcore.Gateway(
            self,
            "TravelAgentGatewayV2",
            gateway_name="travel-planning-agent-gateway",
            description=(
                "Gateway exposing web search, weather, and places tools to "
                "the travel planning agent."
            ),
            protocol_configuration=agentcore.GatewayProtocol.mcp(
                instructions=(
                    "Tools for building travel itineraries: search the web "
                    "for current destination information, look up weather "
                    "forecasts, and search/sequence points of interest."
                ),
            ),
            authorizer_configuration=self._build_authorizer_configuration(
                gateway_oidc_discovery_url,
                gateway_oidc_allowed_audience,
                gateway_oidc_allowed_clients,
                gateway_oidc_allowed_scopes,
            ),
        )

        self.web_search_target = self._add_web_search_target()
        self._grant_web_search_invoke()
        self.weather_target = self.gateway.add_lambda_target(
            "WeatherTarget",
            gateway_target_name="weather-tool",
            description="Daily weather forecast tool (Open-Meteo).",
            lambda_function=weather_function,
            tool_schema=agentcore.ToolSchema.from_local_asset(
                str(LAMBDAS_DIR / "weather" / "tool_schema.json")
            ),
        )
        self.places_target = self.gateway.add_lambda_target(
            "PlacesTarget",
            gateway_target_name="places-tool",
            description="Place search and geographic sequencing tool (Amazon Location Service).",
            lambda_function=places_function,
            tool_schema=agentcore.ToolSchema.from_local_asset(
                str(LAMBDAS_DIR / "places" / "tool_schema.json")
            ),
        )

        self._configure_observability()

        # Grant IAM permissions BEFORE creating the target — the target's
        # own creation synchronously calls bedrock-mantle:ListModels to
        # discover available models (confirmed live: CREATE_FAILED with
        # "GatewayTarget ... failed to stabilize ... 403 ... no
        # identity-based policy allows the bedrock-mantle:ListModels
        # action" when the target and the IAM grant were created in the
        # same deploy without an explicit ordering dependency — creating
        # both resources in one CloudFormation changeset doesn't
        # guarantee the IAM policy attaches before the target's own
        # creation-time model-discovery call runs). Depends on
        # policy_dependable specifically (not just self.gateway.role) —
        # the role resource itself existing doesn't guarantee its policy
        # attachment has finished; policy_dependable is the actual
        # construct CDK's own AddToPrincipalPolicyResult recommends for
        # this exact ordering guarantee.
        inference_grant = self._grant_bedrock_inference_invoke(model_id)
        self.inference_target = self._add_inference_target(model_id)
        self.inference_target.node.add_dependency(inference_grant.policy_dependable)        # Gateway's inference targets are reachable at
        # {gatewayUrl}/inference/{path} — gateway_url above is the MCP
        # endpoint (".../mcp"), so this is the Gateway's own base HTTPS
        # URL with "/inference" appended, not a value AWS's Gateway
        # construct exposes directly. Always computed (this target always
        # exists) — agent.py treats an *unset* GATEWAY_INFERENCE_URL env
        # var as "don't use it", not this stack as the on/off switch (see
        # RuntimeStack/agent.py for the actual opt-in).
        #
        # Built from gateway_id + region, NOT by string-splitting
        # self.gateway.gateway_url — found via a live deploy that
        # self.gateway.gateway_url is a CDK token (resolved only at
        # CloudFormation deploy time, not a real Python string at synth
        # time), so calling .rsplit('/mcp', 1) on it is a no-op against
        # the token's placeholder representation, not the real resolved
        # URL: the deployed value came back as ".../mcp/inference", not
        # ".../inference" (confirmed via a live get-agent-runtime call
        # after deploying). gateway_id is used the same token-safe way
        # for gateway_identifier= elsewhere in this file.
        self.inference_url = (
            f"https://{self.gateway.gateway_id}.gateway.bedrock-agentcore."
            f"{self.region}.amazonaws.com/inference"
        )

        # Per-user token-per-minute budget on the inference target, added
        # as a fast-follow to the deferred rate-limiting/RBAC decision
        # above (DESIGN.md). Dimensioned on the caller's JWT `sub` claim
        # (not IAM principal) — this Gateway currently defaults to IAM
        # auth (see _build_authorizer_configuration()), under which every
        # caller through this project's own Runtime shares a single IAM
        # principal, so a JWT-sub-scoped limit only takes effect once/if
        # this Gateway's authorizer is switched to JWT (Phase 3's optional
        # config). Kept JWT-scoped anyway rather than IAM-scoped, since
        # per-*user* fairness (not just "this one Runtime's aggregate
        # usage") is the actual goal here, and this is the dimension that
        # achieves that once JWT auth is in place; a wildcard "*" entry
        # still gives every distinct caller their own isolated budget in
        # the meantime (or forever, if this Gateway stays IAM-only) rather
        # than silently having no effect for unmatched callers (see AWS's
        # own rate-limit best-practices doc on always including a
        # catch-all entry).
        self._add_inference_rate_limit(model_id)

        self.oauth2_credential_provider = None
        if gateway_oidc_discovery_url and gateway_oidc_client_id and gateway_oidc_client_secret:
            self.oauth2_credential_provider = self._add_oauth2_credential_provider(
                gateway_oidc_discovery_url,
                gateway_oidc_client_id,
                gateway_oidc_client_secret,
            )

    def _add_inference_rate_limit(self, model_id: str) -> agentcore.CfnGatewayRateLimit:
        """Per-user TPM budget on the bedrock-mantle inference target.

        dimensionKeys=["qualifiedModelId", "$.context.jwt.sub"]: scopes the
        budget to one caller calling one model, so a single heavy user (or
        a single expensive model, if more are ever added) can't exhaust a
        budget shared across everyone/everything. qualifiedModelId comes
        first and $.context.jwt.sub second (not the reverse) because AWS's
        rate-limit API enforces that a wildcard ("*") may only appear in
        trailing dimension positions — confirmed live: the first deploy
        attempt at ["$.context.jwt.sub", "qualifiedModelId"] with
        {sub: "*", qualifiedModelId: <concrete>} was rejected outright
        ("Wildcard '*' may only appear at trailing positions. Found
        non-wildcard value at position 1 after '*'", 400 InvalidRequest),
        and CloudFormation rolled back cleanly. This entry uses a concrete
        qualifiedModelId (this agent only ever calls exactly one model,
        matching this file's own no-unnecessary-scope precedent elsewhere)
        with sub="*" trailing, which is a valid ordering under that
        constraint and still isolates each distinct caller into their own
        budget bucket.

        qualifiedModelId is asserted from AWS's own rate-limit example
        payloads to be the bare foundation-model ID (e.g.
        "anthropic.claude-fable-5"), not a `us.`-prefixed cross-region
        inference-profile ID — the same bare-ID convention already found
        live for bedrock-mantle's own model resolution (see
        _add_inference_target()'s docstring and agent.py's
        GATEWAY_INFERENCE_MODEL_ID). Strips the same "us." prefix here
        rather than importing agent.py's constant, since this is
        CDK-side/synth-time logic with no dependency on the agent package.

        A single entry, not a specific-user + wildcard pair like AWS's
        tiered-access example — this project has no user tiers (see
        DESIGN.md; this agent has one flat user population), so every
        caller gets the same, single per-user budget rather than needing a
        named entry per person.

        Depends on the inference target explicitly: this rate limit is
        conceptually scoped to that target's model, and AWS's rate-limit
        API needs the gateway (and, in practice, a stable target/model to
        route against) to already exist — matching this file's existing
        dependency-ordering precedent for the inference target itself.
        """
        bare_model_id = model_id[len("us.") :] if model_id.startswith("us.") else model_id
        rate_limit = agentcore.CfnGatewayRateLimit(
            self,
            "InferenceRateLimit",
            gateway_identifier=self.gateway.gateway_id,
            rate_limit_id="inference-tpm-per-user",
            description=(
                f"Per-user token budget ({INFERENCE_TOKENS_PER_MINUTE_PER_USER} "
                "TPM) on the bedrock-mantle inference target."
            ),
            dimension_keys=["qualifiedModelId", "$.context.jwt.sub"],
            entries=[
                agentcore.CfnGatewayRateLimit.LimitEntryProperty(
                    dimensions={
                        "qualifiedModelId": bare_model_id,
                        "$.context.jwt.sub": "*",
                    },
                    tokens=[
                        agentcore.CfnGatewayRateLimit.RateConfigProperty(
                            rate=INFERENCE_TOKENS_PER_MINUTE_PER_USER,
                            period="minute",
                        ),
                    ],
                ),
            ],
        )
        rate_limit.node.add_dependency(self.inference_target)
        return rate_limit

    @staticmethod
    def _build_authorizer_configuration(
        discovery_url: str | None,
        allowed_audience: list[str] | None,
        allowed_clients: list[str] | None,
        allowed_scopes: list[str] | None,
    ) -> agentcore.IGatewayAuthorizerConfig:
        """AWS IAM (default) unless discovery_url is provided, then JWT.

        Mirrors RuntimeStack's own _build_authorizer_configuration() —
        discovery_url alone is the switch; allowed_audience/allowed_clients/
        allowed_scopes are optional refinements once JWT is selected. See
        this module's docstring for the mutual-exclusivity rationale.
        """
        if discovery_url is None:
            return agentcore.GatewayAuthorizer.using_aws_iam()
        return agentcore.GatewayAuthorizer.using_custom_jwt(
            discovery_url=discovery_url,
            allowed_audience=allowed_audience,
            allowed_clients=allowed_clients,
            allowed_scopes=allowed_scopes,
        )

    def _add_oauth2_credential_provider(
        self,
        discovery_url: str,
        client_id: str,
        client_secret: str,
    ) -> agentcore.CfnOAuth2CredentialProvider:
        """Provision the OBO token-exchange credential provider.

        See this module's docstring for the full design. Passes
        `client_secret` as a plain literal (not `client_secret_config`'s
        Secrets Manager reference) — confirmed live against a real
        deployment that `CreateOauth2CredentialProvider` rejects
        `clientSecretConfig` for `CLIENT_SECRET_BASIC`/`CLIENT_SECRET_POST`
        client authentication (the only two methods this Okta org's
        exchange app supports) with `"clientSecret is required for
        CLIENT_SECRET_BASIC and CLIENT_SECRET_POST authentication
        methods"`, regardless of `client_authentication_method` being set
        explicitly. This matches how `WebStack`'s own OidcClientSecret/
        RuntimeOidcClientSecret are already handled (a plain literal
        passed directly via `SecretValue.unsafe_plain_text()`, not a
        reference) — the client secret still ultimately comes from a real
        secret store (`.env`, loaded by `cdk/app.py`), not a hardcoded
        value in source.
        """
        return agentcore.CfnOAuth2CredentialProvider(
            self,
            "GatewayOauth2CredentialProvider",
            name=GATEWAY_OBO_PROVIDER_NAME,
            credential_provider_vendor="CustomOauth2",
            oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
                custom_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                    oauth_discovery=agentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                        discovery_url=discovery_url,
                    ),
                    client_id=client_id,
                    client_authentication_method="CLIENT_SECRET_BASIC",
                    client_secret=client_secret,
                    on_behalf_of_token_exchange_config=agentcore.CfnOAuth2CredentialProvider.OnBehalfOfTokenExchangeConfigProperty(
                        grant_type="TOKEN_EXCHANGE",
                        token_exchange_grant_type_config=agentcore.CfnOAuth2CredentialProvider.TokenExchangeGrantTypeConfigProperty(
                            actor_token_content="NONE",
                        ),
                    ),
                ),
            ),
        )


    def _configure_observability(self) -> None:
        """Wire up CloudWatch application logs + X-Ray traces for the Gateway.

        See this module's docstring for the full rationale. Both the logs
        and traces deliveries are created together — AWS's docs mark the
        traces delivery source/destination as "required" alongside logs
        for gateway resources, not optional.
        """
        log_group = logs.LogGroup(
            self,
            "GatewayLogGroup",
            log_group_name=GATEWAY_LOG_GROUP_NAME,
            retention=logs.RetentionDays.ONE_MONTH,
        )

        # Construct IDs and `name=` values are "...V2"/"...-v2" — the real,
        # permanent identifiers after Phase 3's live cutover (same
        # rationale as self.gateway's own "V2" ID above: AWS's
        # CloudWatch Logs delivery-source API rejects updating a
        # DeliverySource's ResourceArn in place once the Gateway it
        # points at is replaced — "Update to existing Delivery Source
        # with new ResourceId is not allowed" — so these needed a full
        # logical-ID (and name, since names must be unique per account)
        # change to force clean replacement alongside the Gateway itself).
        logs_source = logs.CfnDeliverySource(
            self,
            "GatewayLogsDeliverySourceV2",
            name="travel-planning-agent-gateway-logs-source-v2",
            log_type="APPLICATION_LOGS",
            resource_arn=self.gateway.gateway_arn,
        )
        traces_source = logs.CfnDeliverySource(
            self,
            "GatewayTracesDeliverySourceV2",
            name="travel-planning-agent-gateway-traces-source-v2",
            log_type="TRACES",
            resource_arn=self.gateway.gateway_arn,
        )

        logs_destination = logs.CfnDeliveryDestination(
            self,
            "GatewayLogsDeliveryDestination",
            name="travel-planning-agent-gateway-logs-destination",
            delivery_destination_type="CWL",
            destination_resource_arn=log_group.log_group_arn,
        )
        traces_destination = logs.CfnDeliveryDestination(
            self,
            "GatewayTracesDeliveryDestination",
            name="travel-planning-agent-gateway-traces-destination",
            delivery_destination_type="XRAY",
        )

        logs_delivery = logs.CfnDelivery(
            self,
            "GatewayLogsDelivery",
            delivery_source_name=logs_source.name,
            delivery_destination_arn=logs_destination.attr_arn,
        )
        logs_delivery.node.add_dependency(logs_source)
        logs_delivery.node.add_dependency(logs_destination)

        traces_delivery = logs.CfnDelivery(
            self,
            "GatewayTracesDelivery",
            delivery_source_name=traces_source.name,
            delivery_destination_arn=traces_destination.attr_arn,
        )
        traces_delivery.node.add_dependency(traces_source)
        traces_delivery.node.add_dependency(traces_destination)

        self.log_group = log_group

    def _grant_web_search_invoke(self) -> None:
        """Grant the Gateway's own service role permission to call the
        managed Web Search connector on the agent's behalf.

        Two statements, matching AWS's documented Web Search connector
        service-role setup exactly:
          - bedrock-agentcore:InvokeGateway on this account's gateways —
            the dispatch path the connector call routes through.
          - bedrock-agentcore:InvokeWebSearch on the AWS-owned tool ARN
            arn:aws:bedrock-agentcore:{region}:aws:tool/web-search.v1 (note
            the literal "aws" account segment: the tool is owned by the
            service, not this account).
        """
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeGateway",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
            )
        )
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeWebSearch",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:InvokeWebSearch"],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:aws:tool/web-search.v1"],
            )
        )

    def _add_web_search_target(self) -> agentcore.CfnGatewayTarget:
        """Add the AWS-managed Web Search connector as a Gateway target.

        There is no L2 construct for connector targets yet, so this uses the
        L1 CfnGatewayTarget directly. Shape verified against the AWS
        "Introducing Web Search on Amazon Bedrock AgentCore" blog post and
        the AWS::BedrockAgentCore::GatewayTarget CloudFormation reference.
        """
        return agentcore.CfnGatewayTarget(
            self,
            "WebSearchTarget",
            name="web-search-tool",
            description="AWS-managed web search connector for grounding itinerary suggestions.",
            gateway_identifier=self.gateway.gateway_id,
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    connector=agentcore.CfnGatewayTarget.ConnectorTargetConfigurationProperty(
                        source=agentcore.CfnGatewayTarget.ConnectorSourceProperty(
                            connector_id=WEB_SEARCH_CONNECTOR_ID,
                        ),
                        configurations=[
                            agentcore.CfnGatewayTarget.ConnectorConfigurationProperty(
                                name=WEB_SEARCH_CONFIGURATION_NAME,
                                parameter_values={},
                            ),
                        ],
                    ),
                ),
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                ),
            ],
        )

    def _grant_bedrock_inference_invoke(self, model_id: str) -> iam.AddToPrincipalPolicyResult:
        """Grant the Gateway's own service role permission to invoke the
        Bedrock model behind the inference target, scoped to the exact
        model/inference-profile ARN — not a wildcard across all Bedrock
        models, matching this project's existing narrow-scoping precedent
        (the Lambda targets above are scoped to their exact function
        ARNs, not lambda:InvokeFunction on "*").

        Both InvokeModel and InvokeModelWithResponseStream are granted,
        plus bedrock-mantle:ListModels and bedrock-mantle:CreateInference
        (scoped to this account/region's default Bedrock Mantle project)
        and bedrock-mantle:CallWithBearerToken — the exact action set
        AWS's own AmazonBedrockMantleInferenceAccess managed policy
        grants for bedrock-mantle inference, used here as individually
        -listed actions (not that policy's Get*/List* wildcards) to keep
        this scoped no more broadly than what's actually needed, matching
        this project's existing narrow-scoping precedent (the Lambda
        targets above are scoped to their exact function ARNs, not
        lambda:InvokeFunction on "*"). `model_id` (e.g.
        "us.anthropic.claude-sonnet-5") is a cross-region inference
        profile ID, not a plain foundation-model ID — its ARN uses the
        `inference-profile` resource type, not `foundation-model`
        (confirmed against AWS's own IAM policy examples for inference
        profiles). ListModels/CreateInference need the account's default
        Bedrock Mantle project ARN
        (arn:aws:bedrock-mantle:{region}:{account}:project/default,
        confirmed against AWS's own "Projects (OpenAI-compatible)" doc
        example) — CallWithBearerToken has no resource-level scoping
        available (its own managed-policy example uses Resource: "*").

        Returns the last statement's AddToPrincipalPolicyResult so the
        caller can make the inference target's creation explicitly
        depend on it via .policy_dependable — see this method's call
        site for why that ordering guarantee is required.
        """
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeBedrockInferenceModel",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
                ],
            )
        )
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="BedrockMantleInference",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-mantle:ListModels",
                    "bedrock-mantle:CreateInference",
                ],
                resources=[
                    f"arn:aws:bedrock-mantle:{self.region}:{self.account}:project/default",
                ],
            )
        )
        return self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="BedrockMantleCallWithBearerToken",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-mantle:CallWithBearerToken"],
                resources=["*"],
            )
        )

    def _add_inference_target(self, model_id: str) -> agentcore.CfnGatewayTarget:
        """Add a bedrock-mantle connector inference target.

        Switched from a hand-rolled bedrock-runtime provider target
        (endpoint + explicit operations/provider_path) after two
        successive live-deploy failures with that approach: (1) the
        provider_path needed to reach bedrock-runtime's Anthropic-native
        route is "/anthropic/v1/messages", not "/v1/messages"
        (bedrock-mantle's own convention, which every documented
        provider-target example happens to use) — found via a live Coral
        com.amazon.coral.service#UnknownOperationException; (2) once the
        path was corrected, the actual call failed with a real Anthropic
        401 "Credential should be scoped to correct service: 'bedrock'"
        — GATEWAY_IAM_ROLE's generic SigV4 signing has no way to know it
        should sign as service "bedrock" for this specific route, and
        every one of AWS's own documented examples for bedrock-runtime's
        Anthropic-native path use an API key, not a hand-signed SigV4
        request through a generic HTTP proxy. Rather than provisioning a
        new Bedrock API key + AgentCore Identity API_KEY credential
        provider (a real new secret/credential to manage, changing this
        feature's security posture), switched to the built-in
        bedrock-mantle connector: AWS's own purpose-built integration
        handles the endpoint/path/auth wiring internally rather than
        this project hand-rolling it, and it correctly reports its own
        model list via the same ListModels call that failed on the first
        live-deploy attempt this session (which is what surfaces here as
        a straightforward, fixable IAM gap rather than another
        undocumented path/auth mismatch to reverse-engineer).

        connector_id="bedrock-mantle" auto-configures endpoint, path
        rewriting, and model-ID-prefix stripping — no explicit
        endpoint/operations/provider_path needed, unlike the provider
        target this replaces. `model_id` is still passed as an explicit
        models allowlist on this account's Gateway-side rate-limiting/
        RBAC surface even though the connector itself does its own model
        discovery — matching decision #92's ECS-task-role precedent of
        no unnecessary broad access (this agent only ever calls exactly
        one model).
        """
        return agentcore.CfnGatewayTarget(
            self,
            # "...V2" — forces a fresh AWS::BedrockAgentCore::GatewayTarget
            # replacement rather than an in-place update. AWS's own API
            # rejects an in-place provider->connector target-type change
            # ("Target configuration cannot be updated from provider to
            # connector") — confirmed live: the first deploy attempt at
            # this logical ID hit exactly that error and cleanly rolled
            # back (UPDATE_ROLLBACK_COMPLETE, no partial state). Same
            # replace-via-logical-ID-change pattern already used elsewhere
            # in this file for the Gateway resource itself and its log-
            # delivery sources, for the analogous authorizerType
            # immutability constraint.
            "InferenceTargetV2",
            name="bedrock-inference-v2",
            description="Routes agent model calls through the Gateway for centralized governance.",
            gateway_identifier=self.gateway.gateway_id,
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                inference=agentcore.CfnGatewayTarget.InferenceTargetConfigurationProperty(
                    connector=agentcore.CfnGatewayTarget.InferenceConnectorTargetConfigurationProperty(
                        source=agentcore.CfnGatewayTarget.InferenceConnectorSourceProperty(
                            connector_id="bedrock-mantle",
                        ),
                    ),
                ),
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                ),
            ],
        )
