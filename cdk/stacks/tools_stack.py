"""ToolsStack: Lambda functions for the travel planning agent's tools.

Deploys the weather (Open-Meteo) and places (Amazon Location Service
geo-places) Lambda functions that later get attached as AgentCore Gateway
Lambda targets in GatewayStack. Each function gets a dedicated execution
role scoped to only the permissions it needs:
  - weather: no AWS API calls (Open-Meteo is a plain HTTPS call), so only
    the default Lambda execution role (CloudWatch Logs) is needed.
  - places: needs `geo-places:SearchText` on the default Places provider.

Observability (added as part of the project's observability pass, see
DESIGN.md): both functions get X-Ray active tracing (`Tracing.ACTIVE`,
which auto-grants the execution role `xray:PutTraceSegments`/
`PutTelemetryRecords` — no manual IAM statement needed) and an explicit
log group with `RetentionDays.ONE_MONTH` instead of Lambda's default
"never expire" behavior, matching the retention already established for
the Gateway's own log group in `gateway_stack.py`.

This stack also owns the project's single, account/region-wide 100%
X-Ray sampling rule (`CfnSamplingRule`) — X-Ray sampling rules aren't a
per-resource/per-stack concept (there's no `resource_arn` scoping this to
just this stack's Lambdas), so it's defined once here (this stack has no
stronger claim to it than any other; it's simply deployed first) rather
than duplicated or arbitrarily split across stacks.
"""
from pathlib import Path

from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_xray as xray
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "lambdas"


class ToolsStack(Stack):
    """Weather and places tool Lambda functions."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.weather_function = self._create_weather_function()
        self.places_function = self._create_places_function()
        self._create_full_sampling_rule()

    def _create_weather_function(self) -> lambda_.Function:
        function = lambda_.Function(
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
            tracing=lambda_.Tracing.ACTIVE,
        )
        # Observability pass: Lambda already auto-created this function's
        # default log group (/aws/lambda/travel-agent-weather-tool) on an
        # earlier deploy, lazily — not managed by CDK. Passing
        # `log_group=logs.LogGroup(log_group_name=...)` here would try to
        # *create* a new log group with that same name and fail
        # ("...already exists", confirmed live) — the same category of
        # problem as Runtime's own log group (see runtime_stack.py's
        # RuntimeLogRetention comment). `LogRetention` is the correct
        # tool for setting retention on a log group CDK doesn't own.
        logs.LogRetention(
            self,
            "WeatherFunctionLogRetention",
            log_group_name="/aws/lambda/travel-agent-weather-tool",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        return function

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
            tracing=lambda_.Tracing.ACTIVE,
        )
        # See WeatherFunctionLogRetention above for why LogRetention
        # (not a directly-created logs.LogGroup) is required here too.
        logs.LogRetention(
            self,
            "PlacesFunctionLogRetention",
            log_group_name="/aws/lambda/travel-agent-places-tool",
            retention=logs.RetentionDays.ONE_MONTH,
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

    def _create_full_sampling_rule(self) -> xray.CfnSamplingRule:
        """Account/region-wide 100% X-Ray sampling rule (observability pass).

        X-Ray's built-in default sampling rule only records 1 request/sec
        plus 5% of any additional requests — for this project's actual
        traffic profile (a small, known group of users, not a high-volume
        service; see README), that default could easily mean the one
        request you're actively trying to debug never gets sampled.
        `priority=1` (lower number = evaluated first) with `service_name:
        "*"` overrides X-Ray's own built-in default rule (fixed priority
        `9999`) for every traced service in this account/region — Lambdas
        (Tracing.ACTIVE), the Gateway, the Runtime, and the Web ECS
        service's ADOT-sourced spans alike.
        """
        return xray.CfnSamplingRule(
            self,
            "FullSamplingRule",
            sampling_rule=xray.CfnSamplingRule.SamplingRuleProperty(
                rule_name="travel-agent-full-sampling",
                priority=1,
                fixed_rate=1.0,
                reservoir_size=1,
                service_name="*",
                service_type="*",
                host="*",
                http_method="*",
                url_path="*",
                resource_arn="*",
                version=1,
            ),
        )
