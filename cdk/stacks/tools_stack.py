"""ToolsStack: Lambda functions for the travel planning agent's tools.

Deploys the weather (Open-Meteo) and places (Amazon Location Service
geo-places) Lambda functions that later get attached as AgentCore Gateway
Lambda targets in GatewayStack. Each function gets a dedicated execution
role scoped to only the permissions it needs:
  - weather: no AWS API calls (Open-Meteo is a plain HTTPS call), so only
    the default Lambda execution role (CloudWatch Logs) is needed.
  - places: needs `geo-places:SearchText` on the default Places provider.
"""
from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "lambdas"


class ToolsStack(Stack):
    """Weather and places tool Lambda functions."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.weather_function = self._create_weather_function()
        self.places_function = self._create_places_function()

    def _create_weather_function(self) -> lambda_.Function:
        return lambda_.Function(
            self,
            "WeatherFunction",
            function_name="travel-agent-weather-tool",
            description=(
                "Travel planning agent tool: daily weather forecast via "
                "Open-Meteo (no API key required)."
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS_DIR / "weather")),
            timeout=Duration.seconds(15),
            memory_size=256,
        )

    def _create_places_function(self) -> lambda_.Function:
        function = lambda_.Function(
            self,
            "PlacesFunction",
            function_name="travel-agent-places-tool",
            description=(
                "Travel planning agent tool: place search and geographic "
                "day-sequencing via Amazon Location Service (geo-places)."
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS_DIR / "places")),
            timeout=Duration.seconds(15),
            memory_size=256,
        )

        # geo-places uses a fixed, account-agnostic "default" provider resource
        # per region rather than a customer-owned Place Index resource.
        function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AllowGeoPlacesSearchText",
                effect=iam.Effect.ALLOW,
                actions=["geo-places:SearchText"],
                resources=[f"arn:aws:geo-places:{self.region}::provider/default"],
            )
        )

        return function
