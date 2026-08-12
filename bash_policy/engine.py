"""Policy evaluation and deterministic decision resolution."""

from collections.abc import Iterable

from .models import Decision, Policy, Request, Resolution

DISPOSITION_PRECEDENCE = ("deny", "ask", "allow")


def evaluate_policies(request: Request, policies: Iterable[Policy]) -> Resolution:
    """Evaluate every policy and resolve actions independently from advice."""
    results: list[tuple[Policy, Decision]] = []
    for policy in policies:
        decision = policy.evaluate(request)
        if decision is not None:
            results.append((policy, decision))

    advice = tuple(
        dict.fromkeys(
            decision.reason
            for policy, decision in sorted(
                results, key=lambda item: item[0].priority, reverse=True
            )
            if decision.disposition == "advice" and decision.reason
        )
    )

    for disposition in DISPOSITION_PRECEDENCE:
        candidates = [
            (policy, decision)
            for policy, decision in results
            if decision.disposition == disposition
        ]
        if candidates:
            policy, decision = max(candidates, key=lambda item: item[0].priority)
            return Resolution(disposition, decision.reason, advice, policy.name)

    return Resolution(None, advice=advice)
