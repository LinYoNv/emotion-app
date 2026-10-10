from __future__ import annotations

import pytest

from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin


class _SplitGenerationHarness(ProactiveMessageMixin):
    def __init__(self) -> None:
        self.data = {
            "recent_photo_generations": [
                {
                    "session": "tool_photo_default:GroupMessage:1097283005",
                    "reference_id": "stale-reference",
                    "reference_kind": "source",
                    "reference_roles": ["source"],
                    "reference_used": True,
                }
            ]
        }
        self._image_companion_generation_metadata = {
            "reference_id": "older-bridge-reference",
            "reference_used": True,
        }

    async def _generate_photo_image(self, **_kwargs):
        return "独立生图服务", "", "generation failed"

    def _image_companion_last_metadata(self):
        return dict(self._image_companion_generation_metadata)


@pytest.mark.asyncio
async def test_split_generation_failure_does_not_reuse_stale_reference_metadata() -> None:
    harness = _SplitGenerationHarness()

    result = await harness._generate_photo_image_result(
        workflow_kind="selfie",
        prompt_text="test",
        session_key="tool_photo_default:GroupMessage:1097283005",
    )

    assert result.success is False
    assert result.reference_used is False
    assert result.reference_id == ""
    assert result.reference_kind == ""
    assert result.reference_roles == ()
