"""MemoryStack: AgentCore Memory for the travel planning agent.

Short-term memory (raw turn-by-turn conversation history within a session)
is always-on in AgentCore Memory; `expiration_duration` controls how long
that raw event history is retained.

Long-term memory is enabled via managed strategies:
  - User preference: extracts durable traveler preferences (e.g. "prefers
    walking tours", "budget travel", "traveling with kids") so the agent can
    recall them in future sessions without the user repeating themselves.
    Namespaced per-actor (not per-session) so preferences persist across
    separate trips/conversations for the same traveler.
  - Summarization: extracts a running summary of each conversation, useful
    for the agent to recall the gist of a past trip-planning session (e.g.
    "we planned a 5-day Kyoto trip focused on temples and food") without
    replaying the full raw transcript.
"""
from aws_cdk import Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

# AgentCore Memory namespace placeholders: {actorId} and {sessionId} are
# resolved by the service at write/read time.
USER_PREFERENCE_NAMESPACE = "/travel-agent/actor/{actorId}/preferences"
SUMMARIZATION_NAMESPACE = "/travel-agent/actor/{actorId}/session/{sessionId}/summary"

# Retention window for short-term (raw event) memory.
SHORT_TERM_EXPIRATION = Duration.days(90)


class MemoryStack(Stack):
    """AgentCore Memory with short-term event storage + long-term strategies."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.memory = agentcore.Memory(
            self,
            "TravelAgentMemory",
            memory_name="travel_planning_agent_memory",
            description=(
                "Short-term conversation history and long-term traveler "
                "preferences/session summaries for the travel planning agent."
            ),
            expiration_duration=SHORT_TERM_EXPIRATION,
            memory_strategies=[
                agentcore.MemoryStrategy.using_user_preference(
                    strategy_name="TravelerPreferences",
                    description=(
                        "Durable traveler preferences (interests, budget "
                        "style, pace, travel party) recalled across trips."
                    ),
                    namespaces=[USER_PREFERENCE_NAMESPACE],
                ),
                agentcore.MemoryStrategy.using_summarization(
                    strategy_name="TripPlanningSessionSummary",
                    description=(
                        "Running summary of each trip-planning conversation."
                    ),
                    namespaces=[SUMMARIZATION_NAMESPACE],
                ),
            ],
        )
