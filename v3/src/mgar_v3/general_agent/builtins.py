"""Built-in registrations for the general planning catalog."""

from mgar_v3.general_agent.capabilities import CapabilityDescriptor, CapabilityRegistry, Resources
from mgar_v3.general_agent.contracts import TaskDomain


def default_registry() -> CapabilityRegistry:
    declarations: tuple[tuple[TaskDomain, str, str], ...] = (
        ("coding", "coding.harness", "Use the existing CodingLoop policy and project admission."),
        ("coding", "coding.inspect", "Inspect the repository through the existing CodingLoop."),
        ("research", "research.sources", "Collect source references for the declared goal."),
        ("research", "research.report", "Assemble checked findings into a report."),
        ("data", "data.analyze", "Analyze the declared inputs and produce results."),
        ("office", "office.draft", "Prepare a draft for review."),
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
                idempotent=key != "coding.harness",
                effect="workspace_write" if key == "coding.harness" else "read_only",
                upper_bound=Resources(tool_calls=1),
                timeout_seconds=60,
                evidence_contract="general-node-receipt-v1",
            )
            for domain, key, description in declarations
        ],
        unavailable=[key for domain, key, _ in declarations if domain != "coding"],
    )
