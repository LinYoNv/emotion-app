"""V2 reality activity services."""
from __future__ import annotations
from .common import *

class RealityActivityMethods:

    def mihome_unavailable_reason(self) -> str:
        if _MIHOME_IMPORT_ERROR:
            return '现实触及内置米家运行时不可用，请安装米家依赖后重载插件'
        return '现实触及内置米家运行时尚未初始化'

    def _init_embedded_mihome(self) -> None:
        """Initialize the embedded MiHome runtime without loading the old plugin."""
        if MiHomeDataManager is None or MiHomeClient is None or MiHomeWebAPI is None:
            logger.warning('[RealityCompanion] 米家内置运行时不可用: %s', _single_line(_MIHOME_IMPORT_ERROR, 180))
            return
        try:
            self.mihome_data_manager = MiHomeDataManager(PLUGIN_NAME, data_root=self.data_dir / 'mihome')
            self.mihome_client = MiHomeClient(self.mihome_data_manager)
            self.client = self.mihome_client
            self.data_manager = self.mihome_data_manager
            self.mihome_web_api = MiHomeWebAPI(self)
        except Exception as exc:
            self.mihome_data_manager = None
            self.mihome_client = None
            self.mihome_web_api = None
            logger.warning('[RealityCompanion] 初始化内置米家运行时失败: %s', _single_line(exc, 180))

    def _migrate_embedded_mihome_state(self) -> None:
        return False

    def _load_data(self) -> dict[str, Any]:
        if not self.data_file.is_file():
            return {'version': 1, 'users': {}, 'reality_touch': {}, 'reality_touch_outputs': {}}
        try:
            loaded = json.loads(self.data_file.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                loaded.setdefault('version', 1)
                loaded.setdefault('users', {})
                loaded.setdefault('reality_touch', {})
                loaded.setdefault('reality_touch_outputs', {})
                return loaded
        except Exception as exc:
            logger.warning('[RealityCompanion] 读取数据失败，将使用空数据: %s', _single_line(exc, 160))
        return {'version': 1, 'users': {}, 'reality_touch': {}, 'reality_touch_outputs': {}}

    def _save_data_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.data_file.with_name(f'.{self.data_file.name}.{uuid.uuid4().hex}.tmp')
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + '\n'
        try:
            with open(temp, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.data_file)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _schedule_data_save(self, delay: float=0.2) -> None:
        if isinstance(self._save_task, asyncio.Task) and (not self._save_task.done()):
            return

        async def delayed_save() -> None:
            await asyncio.sleep(max(0.0, delay))
            await asyncio.to_thread(self._save_data_sync)
        self._save_task = self._create_lifecycle_background_task(delayed_save(), label='save_data')

    def _create_lifecycle_background_task(self, awaitable: Any, *, label: str='background') -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=f'companion_v2.reality:{label}')
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _user(self, user_id: Any, *, create: bool=True) -> dict[str, Any] | None:
        normalized = _single_line(user_id, 120)
        if not normalized:
            return None
        users = self.data.setdefault('users', {})
        if not isinstance(users, dict):
            users = {}
            self.data['users'] = users
        user = users.get(normalized)
        if not isinstance(user, dict):
            if not create:
                return None
            user = {'user_id': normalized}
            users[normalized] = user
        user.setdefault('user_id', normalized)
        return user

    async def _migration_loop(self) -> None:
        """Retry once after AstrBot finishes loading the companion host plugin."""
        for _ in range(24):
            if self.data.get('legacy_migration_completed'):
                return
            if await self._try_legacy_migration():
                return
            await asyncio.sleep(5)

    async def _alarm_loop(self) -> None:
        while True:
            try:
                await self._run_wakeup_alarm_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[RealityCompanion] 起床提醒轮询失败: %s', _single_line(exc, 160))
            await asyncio.sleep(30)

    def _mihome_api(self) -> Any | None:
        """Return the embedded MiHome API owned by Reality Companion."""
        api = getattr(self, 'mihome_extension_api', None)
        return api if getattr(self, 'mihome_web_api', None) is not None else None

    async def _try_legacy_migration(self) -> bool:
        """Import the V1 runtime records the config migration never touched.

        V1 把音响输出、摄像头设备目录和按用户的摄像头授权写在运行库
        （``plugin_data/astrbot_plugin_reality_companion/reality_companion.json``），
        配置迁移只搬可移植偏好，这些记录一直留在旧目录里。这里只补新版还没有的项，
        已有值一律保留。
        """
        path = self._legacy_reality_state_path()
        if path is None:
            await self._finish_legacy_migration('legacy_reality_state_missing')
            return True
        try:
            payload = json.loads(await asyncio.to_thread(path.read_text, encoding='utf-8'))
        except FileNotFoundError:
            await self._finish_legacy_migration('legacy_reality_state_missing')
            return True
        except (OSError, ValueError) as exc:
            logger.warning('[RealityCompanion] 读取旧版运行记录失败: %s', _single_line(exc, 160))
            return False
        from companion_v2.legacy.reality_migration import merge_legacy_runtime_state
        merged, report = merge_legacy_runtime_state(self.data, payload)
        if isinstance(merged, dict) and merged is not self.data:
            self.data.clear()
            self.data.update(merged)
        copied = [item for item in report if item.get('kind') == 'copied']
        kept = [item for item in report if item.get('kind') == 'kept_current']
        index_report = await self._migrate_legacy_camera_index()
        if index_report is not None:
            (copied if index_report.get('kind') == 'copied' else kept).append(index_report)
        await self._finish_legacy_migration(
            'legacy_reality_state_migrated' if copied else 'legacy_reality_state_kept')
        if copied or kept:
            logger.info('[RealityCompanion] 旧版现实触及运行记录已核对 copied=%d kept=%d',
                        len(copied), len(kept))
        return True

    async def _migrate_legacy_camera_index(self):
        """Carry the old camera choice only when the old catalog proves the device.

        V1 的 ``camera.index`` 只有在设备目录里真实存在时才继续可用；新版目录刚由
        运行记录补进来，所以这里能做一次有据可依的核对，而不是照抄一个可能已经指向
        别的设备的编号。新版已经有非默认选择时一律保留。
        """
        config_path = self._legacy_reality_config_path()
        catalog = (self.data.get('reality_touch') or {}).get('camera_device_catalog')
        devices = catalog.get('devices') if isinstance(catalog, dict) else None
        known = {item.get('index') for item in devices or [] if isinstance(item, dict)}
        camera = self.config.get('camera') if isinstance(self.config, dict) else None
        if not isinstance(camera, dict):
            return None
        if camera.get('index') not in (None, 0, '0'):
            return {'field': 'reality.camera.index', 'kind': 'kept_current',
                    'explain': '新版已经选过摄像头，保留当前值。'}
        if config_path is None:
            return None
        try:
            legacy_config = json.loads(await asyncio.to_thread(config_path.read_text, encoding='utf-8'))
        except (OSError, ValueError):
            return None
        legacy_camera = legacy_config.get('camera') if isinstance(legacy_config, dict) else None
        index = legacy_camera.get('index') if isinstance(legacy_camera, dict) else None
        if type(index) is not int or index <= 0 or (known and index not in known):
            return None
        camera['index'] = index
        saver = getattr(self.config, 'save_config', None)
        try:
            if callable(saver):
                saver()
        except Exception as exc:
            logger.warning('[RealityCompanion] 保存迁移的摄像头编号失败: %s', _single_line(exc, 160))
            return None
        return {'field': 'reality.camera.index', 'kind': 'copied',
                'explain': '旧版摄像头在设备目录中存在，继续沿用这个编号。'}

    def _legacy_reality_config_path(self):
        base = Path(self.data_dir)
        for parent in base.parents:
            if parent.name.casefold() == 'plugin_data':
                return parent.parent / 'config' / 'astrbot_plugin_reality_companion_config.json'
        return None

    async def _finish_legacy_migration(self, outcome: str) -> None:
        self.data['legacy_migration_completed'] = True
        self.data['legacy_migration_at'] = _now_ts()
        self.data['legacy_migration_outcome'] = outcome
        await asyncio.to_thread(self._save_data_sync)

    def _legacy_reality_state_path(self):
        base = Path(self.data_dir)
        for parent in base.parents:
            if parent.name.casefold() == 'plugin_data':
                return parent / 'astrbot_plugin_reality_companion' / 'reality_companion.json'
        return None

    def _relationship_owner_user_ids(self) -> set[str]:
        owners = set(self.authorized_user_ids)
        api = self.v2_host_api()
        getter = getattr(api, 'mobile_pairing_candidates', None) if api is not None else None
        if callable(getter):
            try:
                owners.update(_single_line(item, 120) for item in getter()
                              if _single_line(item, 120) and self._host_identity(item).get('is_admin'))
            except Exception:
                pass
        return owners

    def _proactive_quota_policy(self, user: dict[str, Any]) -> dict[str, Any]:
        context = self._host_identity(user.get('user_id'))
        tier = _safe_int(context.get('proactive_tier'), 1, 1, 5)
        return {'tier': tier, 'label': f'L{tier}'}

    def _environment_today_key(self) -> str:
        return self._wakeup_now().strftime('%Y-%m-%d')

    @staticmethod
    def _safe_event_is_private(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    @staticmethod
    def _private_user_id_for_event(event: AstrMessageEvent) -> str:
        return _single_line(event.get_sender_id(), 120)

    def _format_proactive_relationship_fact(self, user: dict[str, Any]) -> str:
        return _single_line(self._host_identity(user.get('user_id')).get('relationship'), 500)

    async def _recent_private_conversation_for_proactive_review(self, user: dict[str, Any], limit: int=8) -> str:
        return _single_line(self._host_identity(user.get('user_id')).get('recent_dialogue'), 2400)

    def _official_cron_manager(self) -> Any | None:
        api = self.v2_host_api()
        getter = getattr(api, 'get_reality_touch_cron_manager', None) if api is not None else None
        return getter() if callable(getter) else None

    def _environment_fromtimestamp(self, value: float) -> datetime:
        try:
            zone = zoneinfo.ZoneInfo(self.environment_perception_timezone)
        except Exception:
            zone = datetime.now().astimezone().tzinfo
        return datetime.fromtimestamp(float(value), tz=zone)

    async def _send_chain_components(self, umo: str, components: list[Any], *, user_id: str,
                                     operation_key: str) -> bool:
        api = self.v2_host_api()
        sender = getattr(api, 'send_reality_touch_chat', None) if api is not None else None
        if not callable(sender):
            return False
        text = ''.join((str(getattr(item, 'text', '') or '') for item in components))
        return bool(await sender(umo, text, user_id=user_id, operation_key=operation_key))

    async def _record_reality_touch_delivery(self, user: dict[str, Any], text: str, *, source: str) -> bool:
        user_id = _single_line(user.get('user_id'), 120) if isinstance(user, dict) else ''
        api = self.v2_host_api()
        recorder = getattr(api, 'record_reality_touch_output', None) if api is not None else None
        if not user_id or not callable(recorder):
            return False
        try:
            result = await recorder(user_id, text, source=source, delivered_at=_now_ts())
        except Exception as exc:
            logger.warning('[RealityCompanion] 现实触及输出回写失败: %s', _single_line(exc, 160))
            return False
        return bool(isinstance(result, dict) and result.get('recorded'))

    def integration_status(self) -> dict[str, Any]:
        cleanup = getattr(self, '_mobile_cleanup_sessions', None)
        if callable(cleanup):
            cleanup()
        managed = self.v2_host_api() is not None
        return {'available': True, 'enabled': bool(self.enable_experimental_bluetooth_wakeup), 'camera_enabled': bool(self.enable_reality_touch_camera), 'home_enabled': bool(self.home_enabled), 'home_available': bool(self.home_enabled and self.home_base_url and self._cfg_str('home.access_token', '')), 'health_enabled': bool(self.health_enabled), 'owner': 'companion_v2', 'execution_path': 'v2_native', 'managed_by_v2': managed, 'users': len(self.data.get('users', {})) if isinstance(self.data.get('users'), dict) else 0, 'audio': self._reality_touch_audio_snapshot(), 'camera': self._reality_touch_camera_page_snapshot(), 'mobile': {'enabled': self._mobile_enabled(), 'running': self._mobile_server_runner is not None, 'host': self._mobile_host(), 'port': self._mobile_server_bound_port or self._mobile_port(), 'pairing_configured': bool(self._mobile_pairing_token()), 'session_ttl_hours': self._mobile_session_ttl() // 3600, 'location_ttl_seconds': self._mobile_location_ttl(), 'amap_reverse_geocode_enabled': self._mobile_amap_enabled(), 'amap_api_key_configured': bool(self._mobile_amap_api_key()), 'amap_cache_ttl_seconds': self._mobile_amap_cache_ttl(), 'amap_request_timeout_seconds': self._mobile_amap_timeout_seconds(), 'telemetry_enabled': self._mobile_telemetry_enabled(), 'telemetry_ttl_seconds': self._mobile_telemetry_ttl(), 'activity_enabled': self._mobile_activity_enabled(), 'activity_ttl_seconds': self._mobile_activity_ttl(), 'active_sessions': len(self._mobile_sessions), 'screen_upload_enabled': self._mobile_screen_upload_enabled()}}

    async def _mobile_pairing_token_command(self, *, rotate: bool) -> dict[str, Any]:
        token = self._mobile_pairing_token()
        generated = rotate or not token
        if generated and self._mobile_server_runner is not None:
            await self._stop_mobile_server()
        if generated:
            token = secrets.token_urlsafe(32)
            self._set_group_config_field('mobile', 'pairing_token', token)
        self._set_group_config_field('mobile', 'enabled', True)
        saver = getattr(self.config, 'save_config', None)
        if callable(saver) and saver() is False:
            raise RuntimeError('保存手机网关配置失败')
        if self._mobile_server_runner is None:
            await self._start_mobile_server()
        return {
            'pairing_token': token if generated else '',
            'generated': generated,
            'enabled': self._mobile_enabled(),
            'running': self._mobile_server_runner is not None,
            'host': self._mobile_host(),
            'port': self._mobile_server_bound_port or self._mobile_port(),
            'public_base_url': _single_line(self._cfg_str('mobile.public_base_url', ''), 300).rstrip('/'),
        }

    async def _home_request(self, request_text: str) -> dict[str, Any]:
        """Send a small, explicit Home Assistant service request.

        The page intentionally keeps this surface narrow: configuration is
        local, and device operations are only attempted for obvious on/off
        requests after the user submits the form.
        """
        if not self.home_enabled:
            return {'ok': False, 'reason': 'home_disabled'}
        token = self._cfg_str('home.access_token', '')
        if not self.home_base_url or not token:
            return {'ok': False, 'reason': 'home_not_configured'}
        text = _single_line(request_text, 500)
        lowered = text.casefold()
        service = 'turn_on' if any((word in lowered for word in ('打开', '开启', '开灯', '启动'))) else 'turn_off' if any((word in lowered for word in ('关闭', '关掉', '关灯', '停止'))) else ''
        if not service:
            return {'ok': False, 'reason': 'home_request_failed', 'message': '目前只支持明确的打开或关闭请求'}
        area = self.home_default_area or '未指定区域'
        url = f'{self.home_base_url}/api/services/homeassistant/{service}'
        body = json.dumps({'area_id': area}).encode('utf-8')

        def send() -> dict[str, Any]:
            request_obj = urllib.request.Request(url, data=body, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(request_obj, timeout=self.home_request_timeout_seconds) as response:
                    return {'ok': 200 <= int(response.status) < 300, 'status': int(response.status)}
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                return {'ok': False, 'reason': 'home_request_failed', 'message': _single_line(exc, 180)}
        result = await asyncio.to_thread(send)
        if result.get('ok'):
            result.update({'handled': True, 'summary': f"已向家居服务发送{('打开' if service == 'turn_on' else '关闭')}请求：{area}"})
        return result
