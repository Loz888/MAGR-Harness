"""Built-in registrations for the general planning catalog."""

from mgar_v3.general_agent.capabilities import CapabilityDescriptor, CapabilityRegistry, Resources
from mgar_v3.general_agent.contracts import TaskDomain

CODING_NODE_LIMITS = {
    "coding.execute.inspect": (Resources(model_calls=2, tool_calls=1), 60),
    "coding.execute": (Resources(model_calls=8, tool_calls=24), 180),
}


def default_registry() -> CapabilityRegistry:
    declarations: tuple[tuple[TaskDomain, str, str], ...] = (
        ("coding", "coding.harness", "Use the existing CodingLoop policy and project admission."),
        ("coding", "coding.inspect", "Inspect the repository through the existing CodingLoop."),
        ("coding", "coding.execute.inspect", "Run a scoped CodingLoop inspection with recovery."),
        ("coding", "coding.execute", "Run and recover an explicitly scoped CodingLoop task."),
        ("research", "research.sources", "Collect source references for the declared goal."),
        ("research", "research.report", "Assemble checked findings into a report."),
        ("data", "data.analyze", "Analyze the declared inputs and produce results."),
        ("office", "office.draft", "Prepare a draft for review."),
        ("office", "office.read", "Read a specifically granted Office resource."),
    )
    return CapabilityRegistry(
        [
            CapabilityDescriptor(
                id=key,
                version="v1",
                domain=domain,
                description=description,
                provider="executor",
                provider_id=key,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                permissions=(key + ".use",),
                idempotent=key not in {"coding.harness", "coding.execute"},
                effect=(
                    "workspace_write"
                    if key in {"coding.harness", "coding.execute"}
                    else "read_only"
                ),
                upper_bound=CODING_NODE_LIMITS.get(key, (Resources(tool_calls=1), 60))[0],
                timeout_seconds=CODING_NODE_LIMITS.get(key, (Resources(tool_calls=1), 60))[1],
                evidence_contract="general-node-receipt-v1",
            )
            for domain, key, description in declarations
        ],
        unavailable=[key for domain, key, _ in declarations if domain == "office"],
    )
