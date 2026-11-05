"""P2.09: a job runs on the config version it was created under (D-048)."""

import asyncio

from scc_core import config
from scc_core.pipeline.run import Runtime


def runtime(loader) -> Runtime:
    return Runtime(
        pool=None,
        gateway=None,
        config=config.load("pipeline-v4"),
        embedder="model",
        load_config=loader,
    )


def test_a_job_on_another_version_gets_that_version_loaded_once():
    calls = []
    other = config.load("pipeline-v3") | {"version": "pipeline-v4-test"}

    async def loader(version):
        calls.append(version)
        return other

    rt = runtime(loader)

    async def scenario():
        same = await rt.for_job("pipeline-v4")
        first = await rt.for_job("pipeline-v4-test")
        second = await rt.for_job("pipeline-v4-test")
        return same, first, second

    same, first, second = asyncio.run(scenario())
    assert same is rt
    assert (
        first.config["version"] == "pipeline-v4-test" and second.config is first.config
    )
    assert calls == ["pipeline-v4-test"]
    assert (
        rt.config["version"] == "pipeline-v4"
    )  # the worker's own runtime is untouched
    assert first.embedder == "model"  # same embedding model: the loaded model is reused


def test_a_version_with_another_embedding_model_loads_its_own():
    async def loader(version):
        body = config.load() | {"version": version}
        body["retrieval"] = body["retrieval"] | {"embedding_model": "another/model"}
        return body

    switched = asyncio.run(runtime(loader).for_job("pipeline-v4-other-model"))
    assert switched.embedder is None
