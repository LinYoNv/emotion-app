"""Reject-only guards for unavailable P4-B1b authorities."""


HOST_OWNER_CAPABILITY_UNAVAILABLE_CODE = "p4_b1b_host_owner_capability_unavailable"
REVIEW_PRODUCER_UNAVAILABLE_CODE = "p4_b1b_review_producer_unavailable"


def reject_unverified_owner_capability(claim: object = None) -> dict[str, object]:
    """Deny every proposed host-owner capability without reading the claim."""

    del claim
    return {
        "accepted": False,
        "live_effect_permitted": False,
        "code": HOST_OWNER_CAPABILITY_UNAVAILABLE_CODE,
    }


def reject_unverified_review_producer(candidate: object = None) -> dict[str, object]:
    """Deny every proposed review producer without reading the candidate."""

    del candidate
    return {
        "accepted": False,
        "live_effect_permitted": False,
        "code": REVIEW_PRODUCER_UNAVAILABLE_CODE,
    }
