"""Performance instrumentation decorator for Chord execution paths.

Logs timestamp, file, function/subroutine, duration (ms), and success/failure.
Measurements collected to inform future Rust rewrite decisions.
No histograms or dashboards — structured log lines only.
"""

import asyncio
import functools
import inspect
import time
from pathlib import Path

from chord import logging


def _log(func, start: float, success: bool) -> None:
    duration_ms = (time.perf_counter() - start) * 1000
    code = getattr(func, "__code__", None)
    where = Path(code.co_filename).name if code is not None else "?"
    module = getattr(func, "__module__", None) or "?"
    name = (
        getattr(func, "__qualname__", None)
        or getattr(func, "__name__", None)
        or type(func).__name__
    )
    logging.info(
        f"PERF file={where} func={module}.{name} "
        f"duration_ms={duration_ms:.3f} success={'true' if success else 'false'}"
    )


def instrumented(func):
    """Decorator that logs execution time and outcome for a function.

    Outcome is "did the call raise". That is not the whole truth for a
    function that absorbs its own failures, so a decorated function may also
    say so itself: if it returns a `bool`, that value is the outcome. `poll`
    and the webhook handler use this — they answer failures rather than raise
    them, and reporting those as successes is worse than not measuring at all.

    `CancelledError` is treated as a clean stop rather than a failure, because
    that is what `chord stop` does to `Watcher.run`. The exception still
    travels; only the log line changes.
    """

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            success = True
            try:
                result = await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except BaseException:
                success = False
                raise
            else:
                if isinstance(result, bool):
                    success = result
                return result
            finally:
                _log(func, start, success)

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        success = True
        try:
            result = func(*args, **kwargs)
        except BaseException:
            success = False
            raise
        else:
            if isinstance(result, bool):
                success = result
            return result
        finally:
            _log(func, start, success)

    return wrapper
