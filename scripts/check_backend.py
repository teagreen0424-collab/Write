"""在临时数据库与离线环境中运行后端测试，不读取本地 .env。"""
from contextlib import ExitStack
import gc
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


def main() -> int:
    backend = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend))

    from app import config

    with TemporaryDirectory(prefix="lingmo-check-", ignore_cleanup_errors=True) as directory:
        settings = config.Settings(
            _env_file=None,
            database_url=f"sqlite:///{(Path(directory) / 'tests.db').as_posix()}",
            openai_api_key="",
            openai_base_url="http://127.0.0.1",
            embedding_service="local",
            embedding_api_key="",
            embedding_base_url="",
            anthropic_api_key="",
            ark_api_key="",
            gemini_api_key="",
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(config, "get_settings", return_value=settings))
            # 测试自有的 MockTransport 与嵌入 mock 可继续工作，默认路径禁止外呼。
            stack.enter_context(patch(
                "httpx.HTTPTransport.handle_request",
                side_effect=AssertionError("External HTTP is disabled during local checks"),
            ))
            stack.enter_context(patch(
                "httpx.AsyncHTTPTransport.handle_async_request",
                side_effect=AssertionError("External HTTP is disabled during local checks"),
            ))
            stack.enter_context(patch(
                "app.infrastructure.ai.embedding.EmbeddingService.embed",
                side_effect=RuntimeError("Embedding is offline during local checks"),
            ))
            from app.infrastructure.db import engine

            try:
                suite = unittest.defaultTestLoader.discover(
                    start_dir=str(backend / "tests"), pattern="test_*.py"
                )
                result = unittest.TextTestRunner(verbosity=1).run(suite)
                if result.testsRun == 0:
                    print("No backend tests were discovered", file=sys.stderr)
                    return 1
                return 0 if result.wasSuccessful() else 1
            finally:
                engine.dispose()
                gc.collect()


if __name__ == "__main__":
    raise SystemExit(main())
