"""WebStack: hosts the Travel Planning Agent's web UI on ECS Fargate,
behind an internet-facing Application Load Balancer.

Replaces the previous "run web/server.py on your own machine" model
(DESIGN.md decisions #19/#28/#30) for anyone who wants a shared, always-on,
remotely-reachable instance instead of a local dev tool. See DESIGN.md's
Phase 1 auth rearchitecture decision (supersedes decisions #37/#38's
ALB-OIDC framing) for the full rationale behind the auth model this stack
now sets up:

  - The ALB is a plain TLS-terminating load balancer with no identity
    logic of its own — it forwards every request straight to the Fargate
    target group. There is no `authenticate-oidc` listener action.
  - `web/server.py` runs the entire OAuth 2.0 Authorization Code + PKCE
    flow against Okta itself (see `web/auth.py`), storing the resulting
    tokens in a single, KMS-envelope-encrypted cookie. This keeps the
    Fargate tasks stateless — any task can serve any request, since
    nothing about a session lives server-side.
  - The AgentCore Runtime's own authorizer is switched to JWT Bearer
    Token auth (RuntimeStack, DESIGN.md's Phase 2 auth rearchitecture
    decision) whenever this stack's WEB_RUNTIME_OIDC_* config is present
    — this stack's Fargate task exchanges each user's Okta access token
    for a Runtime-audienced JWT (RFC 8693, web/auth.py) and presents
    that instead of calling InvokeAgentRuntime with its own IAM role.

Networking: a dedicated VPC (2 AZs, public + private-with-egress subnets,
one NAT Gateway per AZ) — self-contained rather than depending on any
pre-existing VPC, matching how every other resource in this project is
provisioned by its own CDK stack. The ALB sits in the public subnets; the
Fargate task sits in the private subnets (no public IP), reachable only
from the ALB's security group.

TLS: the caller supplies an existing ACM certificate ARN (see
`certificate_arn` below) — this stack does not provision or import a
certificate itself, and does not configure Route 53/DNS; a real domain
pointed at the ALB is left entirely to the caller (DESIGN.md decision
#40). `web_hostname` (see below) must be that same domain — it is used
to build the OIDC `redirect_uri` (DESIGN.md decision #62), so it must
exactly match both what the caller has pointed at this ALB and what is
registered as the redirect URI on the Okta app (decision #50). This
stack cannot verify that DNS record exists or resolves correctly — a
wrong or unset `web_hostname` fails at login time (a `redirect_uri`
mismatch from Okta), not at deploy time.
"""
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]

# ALB idle timeout: raised from the 60s default (DESIGN.md decision #37 /
# PLAN.md Phase 10) — real itinerary-generation turns have taken up to
# ~56s in practice (see PLAN.md Phase 7's MaxTokensReachedException fix
# notes), close enough to the default that a long, tool-heavy turn could
# get its connection killed by the ALB mid-stream.
ALB_IDLE_TIMEOUT = Duration.seconds(180)

# Small and conservative (DESIGN.md decision #37 / PLAN.md Phase 10): this
# is a small-group internal tool, not a high-traffic public service — easy
# to raise later if real usage calls for it.
FARGATE_CPU = 512  # 0.5 vCPU
FARGATE_MEMORY_MIB = 1024  # 1 GB
MIN_TASK_COUNT = 1
MAX_TASK_COUNT = 3
CPU_TARGET_UTILIZATION_PERCENT = 60

CONTAINER_PORT = 8420

# ADOT collector sidecar (observability pass, see DESIGN.md): collects
# OTLP/gRPC traces from the WebContainer (server.py's FastAPI
# instrumentation) and forwards them to AWS X-Ray.
#
# Config file: /etc/ecs/ecs-default-config.yaml, NOT
# /etc/ecs/otel-instance-metrics-config.yaml (the config AWS's generic
# "trace collection" ECS doc example uses, and what this project
# originally deployed with — confirmed live to crash-loop on Fargate:
# "Error: cannot start pipelines: failed to initialize NodeCapacity:
# lstat /rootfs/proc: no such file or directory"). That config's bundled
# receivers expect a real EC2 host filesystem for container-insights
# host metrics, which Fargate does not expose. AWS's own ADOT-on-ECS
# docs (aws-otel.github.io/docs/setup/ecs/task-definition-for-ecs-fargate)
# are explicit that Fargate tasks must use one of two different,
# Fargate-safe bundled configs instead — ecs-default-config.yaml (OTLP/
# StatsD/X-Ray SDK traces, no host metrics) is the correct one here,
# since this project only needs trace forwarding, not container-insights
# resource-utilization metrics.
ADOT_COLLECTOR_IMAGE = "public.ecr.aws/aws-observability/aws-otel-collector:v0.30.0"
ADOT_COLLECTOR_CONFIG = "/etc/ecs/ecs-default-config.yaml"
OTLP_GRPC_PORT = 4317


class WebStack(Stack):
    """ECS Fargate + plain ALB hosting for the web UI (app-level OIDC auth)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime: agentcore.Runtime,
        memory: agentcore.IMemory,
        certificate_arn: str,
        web_hostname: str,
        oidc_issuer: str,
        oidc_authorization_endpoint: str,
        oidc_token_endpoint: str,
        oidc_client_id: str,
        oidc_client_secret: str,
        runtime_oidc_issuer: str,
        runtime_oidc_token_endpoint: str,
        runtime_oidc_client_id: str,
        runtime_oidc_client_secret: str,
        runtime_oidc_audience: str,
        runtime_oidc_scope: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Self-contained VPC (DESIGN.md decision #37) — 2 AZs is the
        # minimum for a genuinely multi-AZ-resilient ALB/Fargate service;
        # one NAT Gateway per AZ avoids a cross-AZ single point of failure
        # for the private subnets' egress (to Bedrock/AgentCore APIs).
        self.vpc = ec2.Vpc(
            self,
            "WebVpc",
            max_azs=2,
            nat_gateways=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        cluster = ecs.Cluster(self, "WebCluster", vpc=self.vpc)

        # Task role: the container's own AWS credentials at runtime (ECS
        # injects these via the task metadata endpoint — no static keys in
        # the image or environment). Tightly scoped to the exact Memory
        # ARN this deployment uses (DESIGN.md decision #37) — no
        # wildcards, matching this project's existing least-privilege
        # convention (e.g. GatewayStack's _grant_web_search_invoke()).
        # No `runtime.grant_invoke()` here — this task role never calls
        # InvokeAgentRuntime itself; the web server presents a Runtime-
        # audienced JWT instead (RFC 8693 token exchange, DESIGN.md's
        # Phase 2 decision), and AWS's own docs confirm IAM/SigV4 and JWT
        # inbound auth are mutually exclusive per-Runtime, so an IAM grant
        # here would be dead permission once WEB_RUNTIME_OIDC_* (always
        # required for this stack) switches the Runtime to JWT-only auth.
        task_role = iam.Role(
            self,
            "WebTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        memory.grant_full_access(task_role)

        # Observability pass (see DESIGN.md): lets the ADOT collector
        # sidecar (added below) forward the traces it receives from
        # WebContainer to AWS X-Ray. AWS's managed policy for exactly
        # this purpose — same permission set the classic X-Ray daemon
        # itself would need (PutTraceSegments/PutTelemetryRecords/
        # GetSampling*), reused here since the ADOT collector plays the
        # same "forward segments to X-Ray" role.
        task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess")
        )

        # Envelope-encryption key for the session cookie (web/auth.py's
        # SessionCookieCodec) — replaces the ALB's own signing keypair now
        # that this container runs the OIDC flow and owns the session
        # cookie itself. A dedicated key, not shared with anything else in
        # this project (no other stack currently uses KMS at all),
        # matching this project's existing per-stack-owns-its-own-resources
        # convention. Automatic annual rotation is on by default for a
        # customer-managed key; old key material stays available for
        # Decrypt regardless, and this cookie's own envelope design (a
        # fresh data key per GenerateDataKey call, stored encrypted
        # alongside the ciphertext in the cookie) makes this doubly moot —
        # a cookie is self-contained and never depends on which specific
        # key version encrypted its data key.
        session_cookie_key = kms.Key(
            self,
            "SessionCookieKey",
            description="Encrypts/decrypts the web UI's session cookie (envelope encryption)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        session_cookie_key.grant_encrypt_decrypt(task_role)

        # The Okta app's client secret is a real bearer credential now
        # that this container (not the ALB) performs the token exchange —
        # stored in Secrets Manager rather than passed as a plain
        # environment variable, unlike the ALB's authenticate-oidc action
        # (which held it as a listener-rule property, not visible to the
        # container at all). SecretValue.unsafe_plain_text() here mirrors
        # this stack's pre-existing handling of the same value (previously
        # passed directly to authenticate_oidc's client_secret prop) — it
        # still ultimately comes from a real secret store (.env, loaded by
        # cdk/app.py), not a hardcoded literal.
        oidc_client_secret_resource = secretsmanager.Secret(
            self,
            "OidcClientSecret",
            secret_string_value=SecretValue.unsafe_plain_text(oidc_client_secret),
        )
        oidc_client_secret_resource.grant_read(task_role)

        # Phase 2 auth rearchitecture: a second, independent Okta app's
        # client secret — the dedicated "API Services" app configured for
        # the Token Exchange grant type (DESIGN.md's Phase 2 decision),
        # used to exchange a user's Okta access token for a Runtime-
        # audienced JWT (web/auth.py's exchange_token_for_runtime()).
        # Same secret-storage treatment as OidcClientSecret above, for the
        # same reason: a real bearer credential, not a plain env var.
        runtime_oidc_client_secret_resource = secretsmanager.Secret(
            self,
            "RuntimeOidcClientSecret",
            secret_string_value=SecretValue.unsafe_plain_text(runtime_oidc_client_secret),
        )
        runtime_oidc_client_secret_resource.grant_read(task_role)

        task_definition = ecs.FargateTaskDefinition(
            self,
            "WebTaskDefinition",
            cpu=FARGATE_CPU,
            memory_limit_mib=FARGATE_MEMORY_MIB,
            task_role=task_role,
            # ARM64 (Graviton) — matches the image build's native
            # architecture. Cross-building for X86_64 (Fargate's default
            # platform) from an Apple Silicon (arm64) machine requires
            # Docker to emulate the target architecture for both the build
            # and the push to ECR — this hung indefinitely during this
            # stack's own deployment (confirmed: the push step sat idle
            # past a 300s SDK request timeout with no progress). Keeping
            # both the image and the runtime platform ARM64 avoids
            # emulation entirely; Fargate has supported Graviton/ARM64
            # tasks since 2021, so this is not a compatibility compromise.
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Travel Agent Web ALB: allows inbound HTTPS from the internet",
            allow_all_outbound=True,
        )
        alb_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS from the internet"
        )

        task_security_group = ec2.SecurityGroup(
            self,
            "WebTaskSecurityGroup",
            vpc=self.vpc,
            description="Travel Agent Web Fargate task: allows inbound only from the ALB",
            allow_all_outbound=True,
        )
        task_security_group.add_ingress_rule(
            alb_security_group, ec2.Port.tcp(CONTAINER_PORT), "From the ALB only"
        )

        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "WebAlb",
            vpc=self.vpc,
            internet_facing=True,
            security_group=alb_security_group,
            idle_timeout=ALB_IDLE_TIMEOUT,
        )

        # ALB access logs -> CloudWatch Logs (observability pass, see
        # DESIGN.md): this is CloudWatch's newer, direct-to-CWL vended-
        # logs integration for ALB (`AWS::Logs::DeliverySource` with
        # `LogType: ALB_ACCESS_LOGS`), not the older `access_logs.s3.*`
        # load balancer attribute — chosen so ALB request-level logs land
        # in CloudWatch alongside every other component's logs in this
        # project, rather than introducing a new S3 bucket + lifecycle
        # policy just for this one log type. Mirrors the same
        # DeliverySource/DeliveryDestination/Delivery triple
        # `gateway_stack.py` already uses for the Gateway's own
        # application logs/traces — no new pattern introduced.
        alb_access_log_group = logs.LogGroup(
            self,
            "AlbAccessLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        alb_logs_source = logs.CfnDeliverySource(
            self,
            "AlbAccessLogsDeliverySource",
            name="travel-agent-web-alb-access-logs-source",
            log_type="ALB_ACCESS_LOGS",
            resource_arn=self.load_balancer.load_balancer_arn,
        )
        alb_logs_destination = logs.CfnDeliveryDestination(
            self,
            "AlbAccessLogsDeliveryDestination",
            name="travel-agent-web-alb-access-logs-destination",
            delivery_destination_type="CWL",
            destination_resource_arn=alb_access_log_group.log_group_arn,
        )
        alb_logs_delivery = logs.CfnDelivery(
            self,
            "AlbAccessLogsDelivery",
            delivery_source_name=alb_logs_source.name,
            delivery_destination_arn=alb_logs_destination.attr_arn,
        )
        alb_logs_delivery.node.add_dependency(alb_logs_source)
        alb_logs_delivery.node.add_dependency(alb_logs_destination)

        # The OIDC redirect_uri points at web_hostname (a caller-supplied
        # friendly domain the caller has separately pointed at this ALB —
        # DNS is explicitly out of scope for this stack, decision #40),
        # not the ALB's own raw DNS name. This must exactly match the
        # redirect URI registered on the Okta app — the caller registers
        # this domain, not the ALB's generated DNS name, since Okta must
        # redirect the user's browser back to whatever hostname they
        # actually logged in from.
        redirect_uri = f"https://{web_hostname}/oauth2/callback"

        container_image = ecs.ContainerImage.from_asset(
            str(REPO_ROOT / "web"),
            file="Dockerfile",
            # Built natively for the host architecture (no explicit
            # `platform=`) — WebTaskDefinition's runtime_platform is set
            # to match (ARM64) above, so the image and the Fargate runtime
            # always agree without needing cross-platform emulation. An
            # earlier attempt explicitly targeted linux/amd64 (Fargate's
            # default) to "fix" a real exec-format-error crash caused by
            # an architecture mismatch — that surfaced a second, worse
            # problem: cross-building for a different architecture than
            # the host requires Docker to emulate both the build and the
            # subsequent push to ECR, which hung indefinitely in this
            # environment (confirmed: stuck past a 300s SDK request
            # timeout with zero progress). Matching the runtime platform
            # to the image's native build architecture instead avoids
            # emulation on either side.
            # The build context is just web/ itself — the Dockerfile only
            # COPYs files from this directory (the CLI removal dropped the
            # earlier COPY of the sibling cli/agent_client.py, which is
            # what previously forced the build context to be the whole
            # repo root plus a long exclude list for cdk.out/.venv/.git).
        )
        # ADOT collector sidecar (observability pass, see DESIGN.md's
        # rationale for choosing OTel over the classic X-Ray SDK for this
        # FastAPI app): must start before WebContainer, which sends it
        # traces over OTLP/gRPC on OTLP_GRPC_PORT via the task's shared
        # `awsvpc` network namespace (both containers see "localhost" as
        # the same network interface).
        adot_log_group = logs.LogGroup(
            self,
            "AdotCollectorLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        adot_container = task_definition.add_container(
            "AdotCollector",
            image=ecs.ContainerImage.from_registry(ADOT_COLLECTOR_IMAGE),
            essential=True,
            command=[f"--config={ADOT_COLLECTOR_CONFIG}"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="adot-collector", log_group=adot_log_group
            ),
        )

        web_log_group = logs.LogGroup(
            self,
            "WebContainerLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        web_container = task_definition.add_container(
            "WebContainer",
            image=container_image,
            port_mappings=[ecs.PortMapping(container_port=CONTAINER_PORT)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="travel-agent-web", log_group=web_log_group
            ),
            environment={
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{OTLP_GRPC_PORT}",
            },
            command=[
                "--agent-runtime-arn",
                runtime.agent_runtime_arn,
                "--region",
                self.region,
                "--memory-id",
                memory.memory_id,
                "--host",
                "0.0.0.0",
                "--port",
                str(CONTAINER_PORT),
                "--oidc-issuer",
                oidc_issuer,
                "--oidc-authorization-endpoint",
                oidc_authorization_endpoint,
                "--oidc-token-endpoint",
                oidc_token_endpoint,
                "--oidc-client-id",
                oidc_client_id,
                "--oidc-client-secret-arn",
                oidc_client_secret_resource.secret_arn,
                "--oidc-redirect-uri",
                redirect_uri,
                "--session-cookie-kms-key-id",
                session_cookie_key.key_id,
                "--runtime-oidc-issuer",
                runtime_oidc_issuer,
                "--runtime-oidc-token-endpoint",
                runtime_oidc_token_endpoint,
                "--runtime-oidc-client-id",
                runtime_oidc_client_id,
                "--runtime-oidc-client-secret-arn",
                runtime_oidc_client_secret_resource.secret_arn,
                "--runtime-oidc-audience",
                runtime_oidc_audience,
                "--runtime-oidc-scope",
                runtime_oidc_scope,
            ],
        )
        web_container.add_container_dependencies(
            ecs.ContainerDependency(
                container=adot_container,
                condition=ecs.ContainerDependencyCondition.START,
            )
        )

        # Explicit, not implicit: CDK defaults the load-balanced container
        # to "the first essential container added to the task" — since
        # AdotCollector (also essential=True, for the START dependency
        # above to be meaningful) is added first, it would otherwise
        # become the ALB's target instead of WebContainer, and fail synth
        # entirely (AdotCollector defines no port mappings for the ALB to
        # attach to). Set explicitly so this doesn't depend on container
        # add-order going forward.
        task_definition.default_container = web_container

        fargate_service = ecs.FargateService(
            self,
            "WebService",
            cluster=cluster,
            task_definition=task_definition,
            security_groups=[task_security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            desired_count=MIN_TASK_COUNT,
            # Matches ALB_IDLE_TIMEOUT's rationale — a task mid-deployment
            # shouldn't be marked unhealthy while a long turn is still
            # streaming to a client from the old task.
            health_check_grace_period=ALB_IDLE_TIMEOUT,
            # Fail fast on a bad deployment (default: up to 3h to notice)
            # rather than leaving tasks stuck in an unhealthy rollout.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Keep the desired_count worth of tasks running throughout a
            # deployment (min_healthy_percent's default of 50% would let
            # a single-task deployment (MIN_TASK_COUNT=1) drop to zero
            # running tasks mid-rollout).
            min_healthy_percent=100,
        )

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "WebTargetGroup",
            vpc=self.vpc,
            port=CONTAINER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[fargate_service],
            health_check=elbv2.HealthCheck(
                path="/api/config",
                healthy_http_codes="200",
            ),
        )

        certificate = acm.Certificate.from_certificate_arn(
            self, "WebCertificate", certificate_arn
        )

        listener = self.load_balancer.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            default_action=elbv2.ListenerAction.forward([target_group]),
        )

        scaling = fargate_service.auto_scale_task_count(
            min_capacity=MIN_TASK_COUNT, max_capacity=MAX_TASK_COUNT
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling", target_utilization_percent=CPU_TARGET_UTILIZATION_PERCENT
        )

        CfnOutput(self, "AlbDnsName", value=self.load_balancer.load_balancer_dns_name)
        CfnOutput(self, "AlbArn", value=self.load_balancer.load_balancer_arn)
