"""Code-interpreter sandbox package.

Provides a best-effort isolated Python executor used by the assistant to run
model-generated code for data analysis, calculations, charts and document
generation. Execution happens in a separate OS process so a crash, hang or
runaway loop cannot take down the bot.
"""

from .sandbox import ExecResult, run_python  # noqa: F401

__all__ = ["ExecResult", "run_python"]
