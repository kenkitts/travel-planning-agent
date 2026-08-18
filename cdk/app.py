#!/usr/bin/env python3
"""CDK app entrypoint for the Travel Planning Agent.

Stacks are added incrementally as later build phases land:
  - ToolsStack (Phase 2): weather + places Lambda functions
  - GatewayStack (Phase 2): AgentCore Gateway + targets
  - MemoryStack (Phase 2): AgentCore Memory (short + long term)
  - RuntimeStack (Phase 3): AgentCore Runtime hosting the Strands agent
"""
import aws_cdk as cdk

app = cdk.App()

app.synth()
