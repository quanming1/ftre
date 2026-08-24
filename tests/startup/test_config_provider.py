import pytest

from ftre.services.config import plugin


class _FakeHttp:
    def register_router(self, _router, *, owner):
        assert owner == "config"
        return lambda: None


class _FakeContext:
    http = _FakeHttp()

    def __init__(self):
        self.provided = {}

    def get(self, _key, *, strict=False):
        assert strict is False

    def provide(self, key, value):
        self.provided[key] = value

    def effect(self, _effect, *, label):
        assert label == "http:config"


@pytest.mark.asyncio
async def test_config_plugin_reads_file_owner_instead_of_empty_manifest_config(monkeypatch):
    calls = []

    class SpyConfigService:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(plugin, "ConfigService", SpyConfigService)

    context = _FakeContext()
    await plugin.apply(context, config={})

    assert calls == [((), {})]
    assert isinstance(context.provided["config"], SpyConfigService)
