"""Current coding guidance shared by ordinary and compacted model contexts."""

CODING_GUIDANCE = (
    "Trace the observed failing value through producers, storage and consumers. "
    "Batch related reads; use read_symbol for Python. Make the smallest complete "
    "change that explains the failure, including imports/helpers. Test early, "
    "revise hypotheses when failures repeat, and check the full objective beyond "
    "visible examples. Reserve turns for verification and a concise final answer "
    "on the verified tree, without further tools once the objective is satisfied."
)
