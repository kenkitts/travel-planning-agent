"""WebStack: hosts the Travel Planning Agent's web UI on ECS Fargate,
behind an internet-facing, OIDC-authenticated Application Load Balancer.

Replaces the previous "run web/server.py on your own machine" model
(DESIGN.md decisions #19/#28/#30) for anyone who wants a shared, always-on,
remotely-reachable instance instead of a local dev tool. See DESIGN.md
decision #37 and PLAN.md Phase 10 for the full rationale and the auth
model this stack depends on:

  - The ALB's `authenticate-oidc` listener rule is the entire human-facing
    login gate — every request is authenticated against Okta before it
    ever reaches the Fargate task. There is no login page inside the
    container itself.
  - The ALB forwards the verified identity to the container per-request as
    a signed `x-amzn-oidc-data` header; `web/server.py`'s
    `actor_id_from_oidc_header()` verifies that signature itself (ES256,
    against ALB's own public-keys endpoint) rather than trusting it
    blindly — see that function's docstring for why.
  - The AgentCore Runtime's own authorizer was reverted from JWT back to
    IAM (RuntimeStack, decision #37) — this stack's Fargate task calls
    InvokeAgentRuntime with its own IAM role (SigV4), the same way the CLI
    already does. The ALB is the identity boundary now, not the Runtime.

Networking: a dedicated VPC (2 AZs, public + private-with-egress subnets,
one NAT Gateway per AZ) — self-contained rather than depending on any
pre-existing VPC, matching how every other resource in this project is
provisioned by its own CDK stack. The ALB sits in the public subnets; the
Fargate task sits in the private subnets (no public IP), reachable only
from the ALB's security group.

TLS: the caller supplies an existing ACM certificate ARN (see
`certificate_arn` below) — this stack does not provision or import a
certificate itself, and does not configure Route 53/DNS; the ALB's own
generated DNS name is used as-is, and pointing a real domain at it is left
to the caller (DESIGN.md-equivalent decision, recorded in PLAN.md Phase 10).
"""
from pathlib import Path

from aws_cdk import CfnOutput, Duration, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
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


class WebStack(Stack):
    """ECS Fargate + OIDC ALB hosting for the web UI."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime: agentcore.Runtime,
        memory: agentcore.IMemory,
        certificate_arn: str,
        oidc_issuer: str,
        oidc_authorization_endpoint: str,
        oidc_token_endpoint: str,
        oidc_user_info_endpoint: str,
        oidc_client_id: str,
        oidc_client_secret: str,
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
        # and Runtime ARNs this deployment uses (DESIGN.md decision #37) —
        # no wildcards, matching this project's existing least-privilege
        # convention (e.g. GatewayStack's _grant_web_search_invoke()).
        task_role = iam.Role(
            self,
            "WebTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        memory.grant_full_access(task_role)
        runtime.grant_invoke(task_role)

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

        # Placeholder ARN for web/server.py's --alb-arn signature
        # verification (actor_id_from_oidc_header() checks the JWT
        # header's "signer" field against exactly this ARN) — passed into
        # the container's environment below.
        alb_arn = self.load_balancer.load_balancer_arn

        container_image = ecs.ContainerImage.from_asset(
            str(REPO_ROOT),
            file="web/Dockerfile",
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
            # The Dockerfile only COPYs web/ and cli/agent_client.py, but
            # from_asset's build context is the whole directory passed in
            # (needed so the Dockerfile's COPY paths can reach both
            # sibling directories at once) — exclude everything else,
            # especially cdk.out and .venv, which are large and (for
            # cdk.out) self-referential: without this exclude, `cdk synth`
            # recursively copies its own previous output into the new
            # asset staging directory, which both wastes time and can
            # produce paths long enough to fail outright (confirmed via a
            # real ENAMETOOLONG error during this stack's own development).
            exclude=[
                "cdk/cdk.out",
                "cdk/cdk.out/**",
                ".venv",
                ".venv/**",
                ".git",
                ".git/**",
                "**/__pycache__",
                "**/__pycache__/**",
                "**/*.pyc",
            ],
        )
        task_definition.add_container(
            "WebContainer",
            image=container_image,
            port_mappings=[ecs.PortMapping(container_port=CONTAINER_PORT)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="travel-agent-web"),
            command=[
                "--agent-runtime-arn",
                runtime.agent_runtime_arn,
                "--alb-arn",
                alb_arn,
                "--region",
                self.region,
                "--memory-id",
                memory.memory_id,
                "--host",
                "0.0.0.0",
                "--port",
                str(CONTAINER_PORT),
            ],
        )

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
            default_action=elbv2.ListenerAction.authenticate_oidc(
                authorization_endpoint=oidc_authorization_endpoint,
                token_endpoint=oidc_token_endpoint,
                user_info_endpoint=oidc_user_info_endpoint,
                issuer=oidc_issuer,
                client_id=oidc_client_id,
                client_secret=SecretValue.unsafe_plain_text(oidc_client_secret),
                scope="openid",
                on_unauthenticated_request=elbv2.UnauthenticatedAction.AUTHENTICATE,
                next=elbv2.ListenerAction.forward([target_group]),
            ),
        )

        scaling = fargate_service.auto_scale_task_count(
            min_capacity=MIN_TASK_COUNT, max_capacity=MAX_TASK_COUNT
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling", target_utilization_percent=CPU_TARGET_UTILIZATION_PERCENT
        )

        CfnOutput(self, "AlbDnsName", value=self.load_balancer.load_balancer_dns_name)
        CfnOutput(self, "AlbArn", value=alb_arn)
