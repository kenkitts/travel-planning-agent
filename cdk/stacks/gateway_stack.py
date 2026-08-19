"""GatewayStack: AgentCore Gateway exposing the agent's tools over MCP.

Creates a single AgentCore Gateway with three targets:
  - Web Search (AWS-managed MCP connector, connectorId="web-search")
  - Lambda target wrapping the weather tool (Open-Meteo)
  - Lambda target wrapping the places tool (Amazon Location Service)

Auth is IAM-only (design decision #15) via GatewayAuthorizer.using_aws_iam().
Credential access from the Gateway to each Lambda target uses the Gateway's
own execution role (GATEWAY_IAM_ROLE credential provider type), which is the
default AgentCore pattern for Lambda targets owned by the same account.

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
"""
from pathlib import Path

from aws_cdk import Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "lambdas"

WEB_SEARCH_CONNECTOR_ID = "web-search"
WEB_SEARCH_CONFIGURATION_NAME = "WebSearch"


class GatewayStack(Stack):
    """AgentCore Gateway with Web Search + weather + places targets."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        weather_function: lambda_.IFunction,
        places_function: lambda_.IFunction,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.gateway = agentcore.Gateway(
            self,
            "TravelAgentGateway",
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
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
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
