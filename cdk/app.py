#!/usr/bin/env python3
"""CDK app entrypoint for the Travel Planning Agent.

Stack wiring:
  ToolsStack   -> weather + places Lambda functions
  GatewayStack -> AgentCore Gateway (Web Search + the two Lambda targets),
                  depends on ToolsStack for the Lambda function references
  MemoryStack  -> AgentCore Memory (short-term + long-term strategies),
                  independent of the other two stacks

RuntimeStack (Phase 3) will host the Strands agent on AgentCore Runtime and
depend on both GatewayStack (for the Gateway URL) and MemoryStack (for the
Memory ID).
"""
import aws_cdk as cdk

from stacks.gateway_stack import GatewayStack
from stacks.memory_stack import MemoryStack
from stacks.runtime_stack import RuntimeStack
from stacks.tools_stack import ToolsStack

app = cdk.App()

env = cdk.Environment(region="us-east-1")

tools_stack = ToolsStack(app, "TravelAgentToolsStack", env=env)

gateway_stack = GatewayStack(
    app,
    "TravelAgentGatewayStack",
    env=env,
    weather_function=tools_stack.weather_function,
    places_function=tools_stack.places_function,
)
gateway_stack.add_dependency(tools_stack)

memory_stack = MemoryStack(app, "TravelAgentMemoryStack", env=env)

runtime_stack = RuntimeStack(
    app,
    "TravelAgentRuntimeStack",
    env=env,
    gateway=gateway_stack.gateway,
    memory=memory_stack.memory,
)
runtime_stack.add_dependency(gateway_stack)
runtime_stack.add_dependency(memory_stack)

# Applied to every taggable resource across all four stacks.
cdk.Tags.of(app).add("auto-delete", "no")
cdk.Tags.of(app).add("project", "travel-planning-agent")

app.synth()
