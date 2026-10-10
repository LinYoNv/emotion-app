from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


def test_ntqq_event_checker_send_rejection_is_identified():
    error = (
        "ActionFailed: <ActionFailed status='failed', retcode=1200, data=None, "
        "message='EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg'>"
    )

    assert ProactiveMessageMixin._is_onebot_event_checker_send_rejection(error) is True


def test_unrelated_onebot_failure_is_not_identified_as_event_checker_rejection():
    assert ProactiveMessageMixin._is_onebot_event_checker_send_rejection("ActionFailed: retcode=1404") is False
    assert ProactiveMessageMixin._is_onebot_event_checker_send_rejection("TimeoutError: sendMsg") is False


def test_google_policy_refusal_with_mixed_punctuation_is_internal_provider_error():
    refusal = (
        "The。 prompt。 could。not。be。submitted.。The。prompt。contains。sensitive。words。"
        "that。violate。Google's。[Generative。AI。Prohibited。Use。policy]"
        "(https://policies.google.com/terms/generative-ai/use-policy).，Tryrephrasingtheprompt."
    )

    assert ProactiveMessageMixin._looks_like_internal_provider_error_text(refusal) is True


def test_truncated_google_policy_refusal_is_dropped_before_proactive_send():
    harness = ProactiveMessageMixin()

    decision = harness._validate_proactive_outbound_candidate(
        "The。 prompt。 could。not。be。submitted.。",
        reason="evening_greeting",
        action="poke+message",
    )

    assert decision["decision"] == "drop"
    assert decision["hard"] is True
    assert decision["text"] == ""


def test_normal_rephrasing_message_is_not_misclassified_as_provider_error():
    text = "这句话听起来有点生硬，换个说法也没关系。"

    assert ProactiveMessageMixin._looks_like_internal_provider_error_text(text) is False
