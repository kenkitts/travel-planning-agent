"""RuntimeStack: AgentCore Runtime hosting the Strands travel planning agent.

Deploys the agent/ directory as a code asset (zip-based, no Docker) to
AgentCore Runtime. Bundling pip-installs agent/requirements.txt into the
asset before upload, matching the standard Lambda-style dependency bundling
pattern since AgentRuntimeArtifact.from_code_asset does not do this itself.

Auth is IAM-only (design decision #15) via RuntimeAuthorizerConfiguration.using_iam().

The agent process reads its Gateway URL and Memory ID from environment
variables (GATEWAY_URL, MEMORY_ID) — this stack wires those from the
GatewayStack and MemoryStack outputs, and grants the Runtime's execution
role the permissions it needs to invoke the Bedrock model and call the
Gateway.
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
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class RuntimeStack(Stack):
    """AgentCore Runtime hosting the travel planning agent, IAM auth only."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        gateway: agentcore.IGateway,
        memory: agentcore.IMemory,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path=str(AGENT_DIR),
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            entrypoint=["agent.py"],
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
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            environment_variables={
                "GATEWAY_URL": gateway.gateway_url,
                "MEMORY_ID": memory.memory_id,
                "AWS_REGION": self.region,
                "MODEL_ID": DEFAULT_MODEL_ID,
            },
        )

        # Claude Sonnet invocation for the agent's own reasoning. The "us."
        # cross-region inference profile for Claude Sonnet 4.5 routes actual
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
