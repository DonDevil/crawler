"""Domain reputation scoring utilities."""

from __future__ import annotations

from typing import Optional

from intelligence.piracy_domain_classifier import PiracyDomainClassifier


class DomainReputation:
    """Compute a simple reputation score for a domain."""

    def __init__(self, classifier: Optional[PiracyDomainClassifier] = None):
        self.classifier = classifier or PiracyDomainClassifier()

    def score(self, domain: str) -> float:
        """Return a score between 0 and 1 where 1 is most suspicious."""
        if self.classifier.is_piracy_domain(domain):
            return 1.0
        return 0.0
