"""Shared control flow; never render these stops as recoverable tool results."""


class ExecutionStopped(Exception):
    pass


class LeaseLost(ExecutionStopped):
    pass


class CancelRequested(ExecutionStopped):
    pass


class SafetyStop(ExecutionStopped):
    reason = "execution_safety_stop"

    def __init__(self, tool=None):
        super().__init__(self.reason)
        # Tool names come from the server's specs, never exception/input text.
        self.recovery = {"reason": self.reason}
        if tool:
            self.recovery["tool"] = tool


class ToolOutcomeUncertain(SafetyStop):
    reason = "tool_outcome_uncertain"


class OutputLimitExceeded(SafetyStop):
    reason = "output_limit_exceeded"
