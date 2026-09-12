"""ObservabilityStack: CloudWatch Alarms + Dashboard for the travel
planning agent, added as a fast-follow once this project's §2h logs/
traces (decisions #97-105) and the AWS DevOps Agent integration (§2j)
had both actually been used in practice — the exact condition decisions
#102/#103 named for revisiting the original "no alarms, no dashboard"
deferral.

Scope (see DESIGN.md's Phase 22 section for the full grill-me rationale):
alarms + dashboard only, wired against the 4 stacks that emit resource-
level metrics today (Tools, Gateway, Runtime, Web). AgentCore Memory has
no observability instrumentation at all yet (no spans/logs enabled) and
is deliberately out of scope here — that's agent-code work (ADOT
instrumentation in agent.py), not a CDK-only alarm/dashboard pass, and
gets its own later phase.

Dependent on (not independent of, unlike DevOpsStack) ToolsStack,
GatewayStack, RuntimeStack, and — when deployed — WebStack, since every
alarm here watches a specific, named resource in one of those stacks
(ALB ARN, ECS cluster/service name, Lambda function name, Runtime/Gateway
ARN) passed in as a real CDK object/property, not a hardcoded string or
an SSM-parameter indirection. WebStack's resources are optional
constructor arguments — this project's WebStack itself is conditionally
constructed (only when WEB_CERTIFICATE_ARN is set), so the ALB/ECS alarms
here are skipped entirely (not error, not stubbed) when WebStack isn't
part of a given deploy.

Notification: a single new SNS topic, one email subscription
(kenkitts@amazon.com) — the same address already used for
DevOpsStack's cost-budget alerts, but a materially different mechanism:
AWS Budgets supports a native EMAIL subscriber type with no SNS
involved at all, while CloudWatch Alarms have no equivalent — an
AlarmAction must be an SNS topic ARN. This is the first SNS topic
anywhere in this project.

Threshold philosophy: count-based ("any error/5xx at all"), not
percentage-based, for everything except ECS CPU/Memory. Confirmed via
real live metrics before deciding (not assumed): this account's actual
traffic is 1-7 Lambda invocations/day and ECS memory has sat flat at
~6-10% since deployment — at this volume, a percentage-rate threshold
(e.g. "5xx rate > 1%") is statistically meaningless (a single request
can swing it from 0% to 100%), whereas "any error in a 5-minute window"
is both the statistically correct framing and the most useful one, since
every single error at this scale is worth knowing about. CPU/Memory keep
AWS's standard >80% percentage thresholds (matching the AWS ECS/Fargate
IDR alarming best-practices reference) since resource saturation is not
volume-sensitive the same way error counts are.
"""
from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

# The single notification address for this stack's SNS topic — matches
# DevOpsStack's BUDGET_NOTIFICATION_EMAIL constant's value, kept as an
# independent constant here rather than importing across stacks, since
# these two stacks are otherwise unrelated and this project has no
# existing convention for sharing config constants between stack modules.
ALARM_NOTIFICATION_EMAIL = "kenkitts@amazon.com"

# Matches runtime_stack.py's own RUNTIME_NAME constant value — duplicated
# rather than imported, for the same reason as ALARM_NOTIFICATION_EMAIL
# above. This is the static runtime name AgentCore uses to build the
# `Name` dimension value on Runtime CloudWatch metrics
# ("{RUNTIME_NAME}::DEFAULT", confirmed live via cloudwatch:ListMetrics —
# note this is a different naming scheme than the Runtime's *log group*
# name, which uses the dynamic agent_runtime_id with a hyphen separator,
# not this static name with a double-colon separator).
RUNTIME_NAME = "travel_planning_agent"

# AgentCore's own CloudWatch namespace (Runtime and Gateway both publish
# here) — confirmed via AWS's "AgentCore generated runtime/gateway
# observability data" docs, not assumed. Both resource types use a
# `Resource` dimension holding the resource's own ARN.
AGENTCORE_NAMESPACE = "AWS/Bedrock-AgentCore"

ERROR_ALARM_PERIOD = Duration.minutes(5)
ERROR_ALARM_EVALUATION_PERIODS = 1
ERROR_ALARM_THRESHOLD = 1  # count-based: "any error at all" (see module docstring)

RESOURCE_ALARM_PERIOD = Duration.minutes(5)
RESOURCE_ALARM_EVALUATION_PERIODS = 3  # matches the AWS ECS/Fargate IDR reference
RESOURCE_ALARM_THRESHOLD_PERCENT = 80


class ObservabilityStack(Stack):
    """CloudWatch Alarms (SNS-notified) + a CloudWatch Dashboard covering
    Lambda, Gateway, Runtime, and (when deployed) ALB/ECS Web resources."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        weather_function: lambda_.IFunction,
        places_function: lambda_.IFunction,
        gateway_arn: str,
        runtime_arn: str,
        web_load_balancer: elbv2.IApplicationLoadBalancer | None = None,
        web_target_group: elbv2.IApplicationTargetGroup | None = None,
        web_cluster: ecs.ICluster | None = None,
        web_fargate_service: ecs.IBaseService | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = self._create_notification_topic()

        widgets: list[cloudwatch.IWidget] = []

        widgets.extend(self._create_lambda_alarms_and_widgets(weather_function, places_function))
        widgets.extend(self._create_agentcore_alarms_and_widgets(gateway_arn, runtime_arn))
        if web_load_balancer is not None:
            widgets.extend(
                self._create_web_alarms_and_widgets(
                    web_load_balancer, web_target_group, web_cluster, web_fargate_service
                )
            )

        cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name="travel-planning-agent",
            widgets=[widgets],
        )

    def _create_notification_topic(self) -> sns.Topic:
        """A single SNS topic for every alarm in this stack — the first
        SNS topic anywhere in this project (see module docstring for why
        this differs from DevOpsStack's native-EMAIL Budgets alert)."""
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name="travel-planning-agent-alarms",
            display_name="Travel Planning Agent Alarms",
        )
        topic.add_subscription(subscriptions.EmailSubscription(ALARM_NOTIFICATION_EMAIL))
        return topic

    def _alarm_action(self) -> cw_actions.SnsAction:
        return cw_actions.SnsAction(self.topic)

    def _create_lambda_alarms_and_widgets(
        self, weather_function: lambda_.IFunction, places_function: lambda_.IFunction
    ) -> list[cloudwatch.IWidget]:
        """Alarm #1/#2 (weather/places Lambda errors) + volume widgets."""
        widgets: list[cloudwatch.IWidget] = []
        for name, fn in (("Weather", weather_function), ("Places", places_function)):
            errors = fn.metric_errors(period=ERROR_ALARM_PERIOD, statistic="Sum")
            cloudwatch.Alarm(
                self,
                f"{name}LambdaErrorsAlarm",
                alarm_name=f"travel-agent-{name.lower()}-lambda-errors",
                alarm_description=f"Any error from the {name} tool Lambda in a 5-minute window.",
                metric=errors,
                threshold=ERROR_ALARM_THRESHOLD,
                evaluation_periods=ERROR_ALARM_EVALUATION_PERIODS,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(self._alarm_action())

            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"{name} Lambda — Invocations & Errors",
                    left=[fn.metric_invocations(statistic="Sum")],
                    right=[errors],
                    width=12,
                )
            )
        return widgets

    def _create_agentcore_alarms_and_widgets(
        self, gateway_arn: str, runtime_arn: str
    ) -> list[cloudwatch.IWidget]:
        """Alarm #8/#9 (Gateway/Runtime SystemErrors) + volume/latency
        widgets.

        Confirmed live (2026-09-12) via `cloudwatch:ListMetrics`/
        `GetMetricData` that AgentCore never publishes a Gateway or
        Runtime metric under a bare `{"Resource": arn}` dimension set —
        every real published series also carries at least `Operation`
        (a fixed value per resource type: `InvokeGateway` for Gateway,
        `InvokeAgentRuntime` for Runtime), and usually
        `Method`/`Protocol`/`ComputeType`/a per-tool `Name` too. A plain
        cloudwatch.Metric() only matches an *exact* published dimension
        set, so `{"Resource": arn}` alone queried a metric identity that
        simply doesn't exist — which is why both the SystemErrors alarms
        and these widgets previously showed zero datapoints (the alarms
        sat in an artificial, evidence-free OK via
        TreatMissingData=notBreaching, not genuine confirmation of no
        errors).

        Two different fixes for two different CloudWatch constructs,
        since they have genuinely different capabilities here:
        - Widgets use a SEARCH-based MathExpression (`_agentcore_search_metric`),
          which matches any published series with the given dimension
          keys present regardless of what other keys/values also exist,
          aggregating them into one series — the standard pattern for
          "known dimensions, unknown/varying extra dimensions."
        - Alarms CANNOT use SEARCH at all — confirmed live via a real
          `UPDATE_FAILED` deploy attempt ("SEARCH is not supported on
          Metric Alarms") and independently by CDK's own
          SearchExpressionProps docs ("A search expression cannot be
          used within an Alarm."). Alarms instead target the one
          minimal, *fixed* rolled-up dimension set AgentCore also
          publishes for SystemErrors alongside the granular per-Method/
          per-Name breakdowns — confirmed live to exist and to actually
          receive data: `{Resource, Operation, Protocol="MCP"}` for
          Gateway, `{Resource, Operation, Name=<runtime-endpoint-name>}`
          for Runtime. Neither carries Method/tool-Name, so both are
          safe to hardcode without silently missing errors from any
          particular tool/method — AgentCore itself rolls those up into
          this one series.

        AgentCore's CDK L2 constructs (Gateway/Runtime) expose no
        metric_* helper methods, confirmed by inspecting the installed
        aws_bedrockagentcore module directly before writing this."""
        widgets: list[cloudwatch.IWidget] = []
        for name, resource_arn, operation, rollup_dims in (
            ("Gateway", gateway_arn, "InvokeGateway", {"Protocol": "MCP"}),
            ("Runtime", runtime_arn, "InvokeAgentRuntime", {"Name": f"{RUNTIME_NAME}::DEFAULT"}),
        ):
            system_errors_alarm_metric = cloudwatch.Metric(
                namespace=AGENTCORE_NAMESPACE,
                metric_name="SystemErrors",
                dimensions_map={"Resource": resource_arn, "Operation": operation, **rollup_dims},
                statistic="Sum",
                period=ERROR_ALARM_PERIOD,
            )
            cloudwatch.Alarm(
                self,
                f"{name}SystemErrorsAlarm",
                alarm_name=f"travel-agent-{name.lower()}-system-errors",
                alarm_description=f"Any AgentCore {name} server-side (5xx) error in a 5-minute window.",
                metric=system_errors_alarm_metric,
                threshold=ERROR_ALARM_THRESHOLD,
                evaluation_periods=ERROR_ALARM_EVALUATION_PERIODS,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(self._alarm_action())

            system_errors_widget_metric = self._agentcore_search_metric(
                resource_arn, operation, "SystemErrors", "Sum"
            )
            invocations = self._agentcore_search_metric(resource_arn, operation, "Invocations", "Sum")
            latency = self._agentcore_search_metric(resource_arn, operation, "Latency", "Average")
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"AgentCore {name} — Invocations, Errors & Latency",
                    left=[invocations, system_errors_widget_metric],
                    right=[latency],
                    width=12,
                )
            )
        return widgets

    def _agentcore_search_metric(
        self, resource_arn: str, operation: str, metric_name: str, statistic: str
    ) -> cloudwatch.MathExpression:
        """One SEARCH-based aggregate metric for a specific AgentCore
        Resource+Operation+MetricName, summed/averaged across whatever
        other dimensions (Method, Protocol, Name, ComputeType) the real
        published series also carry. Dashboard-widget-only — CloudWatch
        rejects SEARCH expressions on Alarms (see
        _create_agentcore_alarms_and_widgets's docstring).

        Uses the plain `Namespace="..." MetricName="..." Dim="..."`
        filter-only SEARCH syntax, not the `{Namespace,Dim,Dim}`
        schema-braces form. Confirmed live (2026-09-12) via
        `cloudwatch:GetMetricData` that the schema-braces form silently
        returns zero matched series for these particular metrics — this
        Gateway/Runtime data has a *variable* dimension count across its
        real published series (some carry Method/Name, some don't; see
        this class's docstring), and the schema-braces form appears to
        require a fixed, exact dimension-name set to match against,
        unlike the plain filter-only form, which matches any series
        containing the given key=value filters regardless of what other
        dimension keys are also present. Re-verify with a real
        GetMetricData call before changing this syntax again — this was
        found by trial, not documented anywhere consulted for this fix.

        Search-expression quoting note: resource_arn/operation are this
        stack's own CDK-resolved constants (a real ARN, a fixed literal),
        never end-user input, so no injection concern — the same trust
        level as every other CDK-constructed metric expression in this
        method."""
        aggregator = "AVG" if statistic == "Average" else "SUM"
        query = (
            f'Namespace="{AGENTCORE_NAMESPACE}" MetricName="{metric_name}" '
            f'Resource="{resource_arn}" Operation="{operation}"'
        )
        expression = f"{aggregator}(SEARCH('{query}', '{statistic}', {ERROR_ALARM_PERIOD.to_seconds()}))"
        return cloudwatch.MathExpression(
            expression=expression,
            label=f"{metric_name}",
            period=ERROR_ALARM_PERIOD,
        )



    def _create_web_alarms_and_widgets(
        self,
        load_balancer: elbv2.IApplicationLoadBalancer,
        target_group: elbv2.IApplicationTargetGroup,
        cluster: ecs.ICluster,
        fargate_service: ecs.IBaseService,
    ) -> list[cloudwatch.IWidget]:
        """Alarms #3-7 (ALB 5xx/unhealthy hosts, ECS task count/CPU/
        Memory) + volume/latency widgets. Only constructed when
        WebStack's resources are actually passed in (see class docstring
        — WebStack itself is optional)."""
        widgets: list[cloudwatch.IWidget] = []

        five_xx = target_group.metrics.http_code_target(
            elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=ERROR_ALARM_PERIOD, statistic="Sum"
        )
        cloudwatch.Alarm(
            self,
            "AlbTarget5xxAlarm",
            alarm_name="travel-agent-web-alb-5xx",
            alarm_description="Any HTTP 5xx from the web target group in a 5-minute window.",
            metric=five_xx,
            threshold=ERROR_ALARM_THRESHOLD,
            evaluation_periods=ERROR_ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(self._alarm_action())

        unhealthy_hosts = target_group.metrics.unhealthy_host_count(period=ERROR_ALARM_PERIOD)
        cloudwatch.Alarm(
            self,
            "AlbUnhealthyHostsAlarm",
            alarm_name="travel-agent-web-alb-unhealthy-hosts",
            alarm_description="Any unhealthy target behind the web ALB in a 5-minute window.",
            metric=unhealthy_hosts,
            threshold=ERROR_ALARM_THRESHOLD,
            evaluation_periods=ERROR_ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(self._alarm_action())

        # RunningTaskCount has no CDK L2 metric_* helper on FargateService
        # (confirmed by inspecting the installed aws_ecs module directly)
        # — built as a raw cloudwatch.Metric against AWS/ECS, matching the
        # dimension shape confirmed live via `aws cloudwatch
        # describe-alarms` against this account's existing ECS
        # autoscaling alarms before writing this.
        running_task_count = cloudwatch.Metric(
            namespace="AWS/ECS",
            metric_name="RunningTaskCount",
            dimensions_map={
                "ClusterName": cluster.cluster_name,
                "ServiceName": fargate_service.service_name,
            },
            statistic="Minimum",
            period=ERROR_ALARM_PERIOD,
        )
        cloudwatch.Alarm(
            self,
            "EcsRunningTaskCountAlarm",
            alarm_name="travel-agent-web-ecs-running-task-count",
            alarm_description="Fewer than 1 running task for the web service in a 5-minute window.",
            metric=running_task_count,
            threshold=1,
            evaluation_periods=ERROR_ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            # Originally BREACHING, on the theory that no datapoints at
            # all is itself a sign of an outage. Confirmed live (2026-09-
            # 06) that's wrong in practice: this metric has genuine
            # reporting gaps independent of service health — the alarm's
            # own history shows a single ALARM transition caused entirely
            # by "no datapoints were received for 1 period," while ECS's
            # own DescribeServices (the ground truth for actual running
            # tasks) reported runningCount=1 continuously through that
            # exact window. Treating a metric-emission gap as a breach
            # produces false alarms on a healthy service — matching the
            # other alarms in this stack (see EcsCpuUtilizationAlarm
            # below, and the ALB alarms above), missing data here means
            # "no evidence of a problem," not "assume the worst."
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(self._alarm_action())

        cpu_utilization = fargate_service.metric_cpu_utilization(period=RESOURCE_ALARM_PERIOD)
        cloudwatch.Alarm(
            self,
            "EcsCpuUtilizationAlarm",
            alarm_name="travel-agent-web-ecs-cpu-high",
            alarm_description="ECS web service CPU utilization above 80% for 3 consecutive 5-minute periods.",
            metric=cpu_utilization,
            threshold=RESOURCE_ALARM_THRESHOLD_PERCENT,
            evaluation_periods=RESOURCE_ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(self._alarm_action())

        memory_utilization = fargate_service.metric_memory_utilization(period=RESOURCE_ALARM_PERIOD)
        cloudwatch.Alarm(
            self,
            "EcsMemoryUtilizationAlarm",
            alarm_name="travel-agent-web-ecs-memory-high",
            alarm_description="ECS web service memory utilization above 80% for 3 consecutive 5-minute periods.",
            metric=memory_utilization,
            threshold=RESOURCE_ALARM_THRESHOLD_PERCENT,
            evaluation_periods=RESOURCE_ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(self._alarm_action())

        widgets.append(
            cloudwatch.GraphWidget(
                title="Web ALB — Requests, 5xx & Response Time",
                left=[target_group.metrics.request_count(statistic="Sum"), five_xx],
                right=[target_group.metrics.target_response_time()],
                width=12,
            )
        )
        widgets.append(
            cloudwatch.GraphWidget(
                title="Web ECS — CPU, Memory & Running Tasks",
                left=[cpu_utilization, memory_utilization],
                right=[running_task_count],
                width=12,
            )
        )
        return widgets
