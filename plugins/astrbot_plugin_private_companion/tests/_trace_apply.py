import sys, os
sys.argv = ["x"]
from astrbot_host.legacy_api import LegacyMigrationWebAPI
orig = LegacyMigrationWebAPI._apply_intent
def traced(self, intent, session, baseline_snapshot):
    try:
        service = session.application.configuration
        defs = service.registry.group(intent.group_id)
        target = self._scope_target(session, intent)
        owner = getattr(service, "_owners", {}).get(defs[0].owner_id)
        print(f"[T] {intent.group_id} layer={getattr(target,'layer',None)} owner_id={defs[0].owner_id} "
              f"cls={type(owner).__name__} mro={[c.__name__ for c in type(owner).__mro__[:4]]} "
              f"names={list(intent.changes)[:8]}", file=sys.stderr, flush=True)
    except Exception as error:
        print("[T] fail", error, file=sys.stderr, flush=True)
    return orig(self, intent, session, baseline_snapshot)
LegacyMigrationWebAPI._apply_intent = traced
import pathlib, asyncio
from tests.legacy_configuration_acceptance_process import run
try:
    asyncio.run(run(pathlib.Path(r"C:\Users\99505\.astrbot\data\plugin_data\astrbot_plugin_private_companion"),
                    pathlib.Path(r"C:\Users\99505\.astrbot\data\config\astrbot_plugin_private_companion_config.json")))
    print("RUN OK", file=sys.stderr, flush=True)
except Exception as error:
    print("RUN FAILED:", type(error).__name__, error, file=sys.stderr, flush=True)
