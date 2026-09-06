"""DevOpsStack: AWS DevOps Agent integration for on-demand monitoring.

Provisions an AWS DevOps Agent Space scoped to this single AWS account
(`us-east-1` only — matches decision #14's region choice), for on-demand
investigation of the travel planning agent's infrastructure via the
DevOps Agent web app or CLI. Deliberately does NOT wire any CloudWatch
Alarm to auto-trigger investigations — usage is on-demand only for now,
revisited once the on-demand flow has been used enough to trust it (see
DESIGN.md §2j).

Two IAM roles, matching AWS's own documented CloudFormation reference
pattern exactly (see "Getting started with AWS DevOps Agent using AWS
CloudFormation"):
  - AgentSpaceRole: assumed by the DevOps Agent service principal
    (`aidevops.amazonaws.com`) to discover/describe resources and read
    observability data during investigations. Attached with the
    AWS-managed `AIDevOpsAgentAccessPolicy` (read-only by design — AWS's
    own docs describe it as "the default set of read-only permissions
    the agent uses for investigations") plus an inline grant for
    `iam:CreateServiceLinkedRole` scoped to the Resource Explorer
    service-linked role (needed for the agent's own resource-discovery
    bootstrap; AWS's reference template requires this explicitly). AWS
    DevOps Agent additionally enforces its own "permission guardrail" — a
    session policy applied at assume-role time that acts as a hard
    ceiling independent of whatever this role's own policy allows —
    confirmed via AWS's own security docs that mutating actions
    (`s3:PutObject`, `ec2:TerminateInstances`, `dynamodb:DeleteItem`,
    etc.) are structurally blocked by this guardrail even if a role's
    policy would otherwise permit them. This is genuinely different from
    every other IAM role in this project's CDK stacks so far: every role
    up to now is assumed by *this project's own* resources (the Runtime's
    execution role, the Gateway's service role, the ECS task role) — this
    is the first role a third-party AWS-managed control plane assumes
    into this account.
  - OperatorAppRole: assumed by the DevOps Agent's operator app (the web
    app you interact with to browse investigations/topology) so it can
    act as a caller with access to agent-space features. Attached with
    the AWS-managed `AIDevOpsOperatorAppAccessPolicy`. Configured for IAM
    auth (`CfnAgentSpace.operator_app.iam`), not IAM Identity Center —
    this is a personal AWS account with no IdC set up anywhere in this
    project's history, so IAM matches every other console/service access
    pattern already in use here.

Both trust policies use the exact confused-deputy conditions from AWS's
reference template (`aws:SourceAccount` + `aws:SourceArn` `ArnLike`
scoped to this account's own `agentspace/*` ARN pattern) — this prevents
a different AWS account's Agent Space from assuming either role.

Cost guardrail: a single AWS Budget scoped to the "AWS DevOps Agent"
Cost Explorer service dimension, $100/month, with two notification
thresholds (80% FORECASTED, 100% ACTUAL) emailing kenkitts@amazon.com
directly (Budgets supports EMAIL subscribers natively — no separate SNS
topic/subscription needed). This is the project's first real per-second
billed AWS service (confirmed pricing: $0.0083/agent-second, ~$30/hour of
active investigation time) — every other billed component in this stack
(Bedrock, Lambda, Gateway) costs fractions of a cent per call, a
materially different cost profile that warrants a budget alert before
first use rather than only checking Cost Explorer after the fact.

Deliberately independent of the other 5 stacks: no CDK cross-stack
dependency, no imported resource ARNs. The Agent Space's own read access
is inherently account-wide (CloudWatch/X-Ray/Logs/CloudTrail plus
resource-inspection actions across whatever services exist in this
account) rather than scoped to this app's specific resources — scoping it
down to only the 5 known stacks' ARNs would undercut the service's own
value proposition of learning the account's full topology, and would
require redeploying this stack every time a new resource is added
elsewhere. This also means the stack can be deployed, updated, or torn
down independently of the other 5 without any ordering constraint.
"""
from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_devopsagent as devopsagent
from aws_cdk import aws_iam as iam
from constructs import Construct

AGENT_SPACE_NAME = "travel-planning-agent"
MONTHLY_BUDGET_USD = 100
BUDGET_NOTIFICATION_EMAIL = "kenkitts@amazon.com"


class DevOpsStack(Stack):
    """AWS DevOps Agent Space monitoring this account, plus a cost budget."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agent_space_role = self._create_agent_space_role()
        operator_app_role = self._create_operator_app_role()
        self.agent_space = self._create_agent_space(operator_app_role)
        self._create_account_association(agent_space_role)
        self._create_cost_budget()

        CfnOutput(
            self,
            "AgentSpaceId",
            value=self.agent_space.attr_agent_space_id,
            description=(
                "DevOps Agent Space ID — use with `aws devops-agent "
                "list-associations --agent-space-id <this>` or the DevOps "
                "Agent web app to run an on-demand investigation."
            ),
        )

    def _create_agent_space_role(self) -> iam.Role:
        """Role the DevOps Agent service assumes to investigate this account.

        Trust policy and permissions match AWS's own CloudFormation
        reference template exactly — see this module's docstring.
        """
        role = iam.Role(
            self,
            "AgentSpaceRole",
            role_name="DevOpsAgentRole-AgentSpace",
            assumed_by=iam.ServicePrincipal(
                "aidevops.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:aidevops:{self.region}:{self.account}:"
                            "agentspace/*"
                        )
                    },
                },
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AIDevOpsAgentAccessPolicy"
                ),
            ],
            description=(
                "Assumed by the AWS DevOps Agent service to discover and "
                "investigate resources in this account. Read-only by "
                "policy (AIDevOpsAgentAccessPolicy) and additionally "
                "restricted by AWS DevOps Agent's own permission "
                "guardrail, which structurally blocks mutating actions "
                "regardless of this role's own policy."
            ),
        )
        # AWS's reference template requires this exact statement — the
        # agent's resource-discovery bootstrap needs to be able to create
        # Resource Explorer's service-linked role in this account if it
        # doesn't already exist.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowCreateServiceLinkedRoles",
                effect=iam.Effect.ALLOW,
                actions=["iam:CreateServiceLinkedRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/aws-service-role/"
                    "resource-explorer-2.amazonaws.com/"
                    "AWSServiceRoleForResourceExplorer"
                ],
            )
        )
        return role

    def _create_operator_app_role(self) -> iam.Role:
        """Role the DevOps Agent operator app (web app) assumes for IAM auth."""
        role = iam.Role(
            self,
            "OperatorAppRole",
            role_name="DevOpsAgentRole-WebappAdmin",
            assumed_by=iam.ServicePrincipal(
                "aidevops.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:aidevops:{self.region}:{self.account}:"
                            "agentspace/*"
                        )
                    },
                },
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AIDevOpsOperatorAppAccessPolicy"
                ),
            ],
            description=(
                "Assumed by the AWS DevOps Agent operator app (the web "
                "app used to browse investigations/topology) for IAM-"
                "based access to this Agent Space's features."
            ),
        )
        # AWS's reference trust policy also grants sts:TagSession for the
        # operator app role specifically (not the Agent Space role).
        role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("aidevops.amazonaws.com")],
                actions=["sts:TagSession"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:aidevops:{self.region}:{self.account}:"
                            "agentspace/*"
                        )
                    },
                },
            )
        )
        return role

    def _create_agent_space(
        self, operator_app_role: iam.Role
    ) -> devopsagent.CfnAgentSpace:
        return devopsagent.CfnAgentSpace(
            self,
            "AgentSpace",
            name=AGENT_SPACE_NAME,
            description=(
                "On-demand monitoring for the travel planning agent's "
                "infrastructure (5 stacks: Tools, Gateway, Memory, "
                "Runtime, Web) in this single account/region."
            ),
            operator_app=devopsagent.CfnAgentSpace.OperatorAppProperty(
                iam=devopsagent.CfnAgentSpace.IamAuthConfigurationProperty(
                    operator_app_role_arn=operator_app_role.role_arn,
                ),
            ),
        )

    def _create_account_association(self, agent_space_role: iam.Role) -> None:
        """Associate this account with the Agent Space (`accountType=monitor`).

        `monitor` (vs. `source`) marks this as the primary account where
        the Agent Space itself lives and is used for topology discovery
        — the only account type relevant here, since decision #Q2 scoped
        this to a single account with no secondary/source accounts.
        """
        association = devopsagent.CfnAssociation(
            self,
            "AccountAssociation",
            agent_space_id=self.agent_space.attr_agent_space_id,
            service_id="aws",
            configuration=devopsagent.CfnAssociation.ServiceConfigurationProperty(
                aws=devopsagent.CfnAssociation.AWSConfigurationProperty(
                    account_id=self.account,
                    account_type="monitor",
                    assumable_role_arn=agent_space_role.role_arn,
                ),
            ),
        )
        association.add_dependency(self.agent_space)

    def _create_cost_budget(self) -> budgets.CfnBudget:
        """$100/month budget scoped to AWS DevOps Agent spend specifically.

        Two notification thresholds — 80% FORECASTED (an early warning
        while trend-based spend is still climbing) and 100% ACTUAL (a
        firm "you've hit the cap" signal) — both emailed directly via
        Budgets' native EMAIL subscriber type, no SNS topic needed.
        """
        return budgets.CfnBudget(
            self,
            "DevOpsAgentCostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="travel-agent-devops-agent-spend",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=MONTHLY_BUDGET_USD,
                    unit="USD",
                ),
                filter_expression=budgets.CfnBudget.ExpressionProperty(
                    dimensions=budgets.CfnBudget.ExpressionDimensionValuesProperty(
                        key="SERVICE",
                        values=["AWS DevOps Agent"],
                    ),
                ),
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    use_amortized=True,
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="FORECASTED",
                        comparison_operator="GREATER_THAN",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL",
                            address=BUDGET_NOTIFICATION_EMAIL,
                        ),
                    ],
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL",
                            address=BUDGET_NOTIFICATION_EMAIL,
                        ),
                    ],
                ),
            ],
        )
