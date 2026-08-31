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


class GatewayStack(Stack):
    """AgentCore Gateway with Web Search + weather + places targets."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        weather_function: lambda_.IFunction,
        places_function: lambda_.IFunction,
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

        self.oauth2_credential_provider = None
        if gateway_oidc_discovery_url and gateway_oidc_client_id and gateway_oidc_client_secret:
            self.oauth2_credential_provider = self._add_oauth2_credential_provider(
                gateway_oidc_discovery_url,
                gateway_oidc_client_id,
                gateway_oidc_client_secret,
            )

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
