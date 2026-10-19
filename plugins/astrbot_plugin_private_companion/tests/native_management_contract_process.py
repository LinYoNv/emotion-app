"""Export synthetic real-handler receipts for the Dashboard bridge regression."""

import asyncio
import json
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch


async def receipts():
    from tests.test_native_extension_api import NativeExtensionAPITests, PREFIX

    case = NativeExtensionAPITests()
    await case.asyncSetUp()
    try:
        service = await case.enable("reality")
        service.authorized_user_ids.add("200")

        async def action(name, payload):
            return await case.call(PREFIX + "/action", body={"extension_id": "reality",
                "action": name, "payload": payload})

        result = {"initial": await case.call()}
        result["missing_user"] = await action("update_camera_policy", {"user_id": "200",
            "camera_enabled": True})
        result["consent"] = await action("consent_camera", {"user_id": "200", "confirmed": True})
        result["policy"] = await action("update_camera_policy", {"user_id": "200",
            "camera_enabled": True, "proactive_mode": "auto", "proactive_max_daily": -1})
        service.data = service._load_data()
        result["refreshed"] = await case.call()
        result["revoke"] = await action("consent_camera", {"user_id": "200", "confirmed": False})
        result["ineligible"] = await action("consent_camera", {"user_id": "999", "confirmed": True})
        with patch.object(service, "_mihome_api", return_value=None):
            result["no_mihome"] = await case.call()
            result["no_mihome_action"] = await action("mihome_start_login", {})
        return result
    finally:
        await case.asyncTearDown()


if __name__ == "__main__":
    # AstrBot reads its root at SDK import time; never use the real user store.
    with TemporaryDirectory(prefix="v2-native-bridge-import-") as temporary:
        os.environ["ASTRBOT_ROOT"] = temporary
        print("NATIVE_MANAGEMENT_CONTRACT=" + json.dumps(asyncio.run(receipts()), ensure_ascii=False))
