"""
Test: Verify 'User UPN detected' log in app.py during message handling.

Approach:
- Stub microsoft_teams SDK modules/classes to safely import src/app.py.
- Prime in-memory user profile with a known email (UPN) via app.remember_user_details.
- Create a minimal ActivityContext stub and run handle_stateful_conversation().
- Capture logger output and assert the expected UPN log appears.

Run:
  python test_user_upn.py
"""

import asyncio
import io
import logging
import os
import sys
from types import ModuleType


def install_teams_stubs():
    """Install minimal stubs for microsoft_teams* modules to allow importing app.py."""
    # Root package
    mt = ModuleType("microsoft_teams")
    sys.modules["microsoft_teams"] = mt

    # microsoft_teams.ai
    mt_ai = ModuleType("microsoft_teams.ai")

    class ListMemory:
        def __init__(self):
            self.items = []

    class ChatPrompt:
        def __init__(self, model):
            self.model = model

        async def send(self, input: str, instructions=None, memory=None, on_chunk=None):
            # Router calls expect a JSON string; return a minimal respond_direct decision
            # Chat calls don't need to stream; if on_chunk is provided, we can emit one tiny chunk.
            if instructions and memory is None:
                # Likely router call
                class R:
                    text = '{"action":"respond_direct","should_search":false,"search_query":"","scope":"graph"}'
                return R()
            # Chat call
            if on_chunk:
                await on_chunk("ok")
            class R2:
                text = "ok"
            return R2()

    mt_ai.ChatPrompt = ChatPrompt
    mt_ai.ListMemory = ListMemory
    sys.modules["microsoft_teams.ai"] = mt_ai

    # microsoft_teams.ai.ai_model
    mt_ai_model = ModuleType("microsoft_teams.ai.ai_model")
    class AIModel:  # placeholder
        pass
    mt_ai_model.AIModel = AIModel
    sys.modules["microsoft_teams.ai.ai_model"] = mt_ai_model

    # microsoft_teams.apps
    mt_apps = ModuleType("microsoft_teams.apps")

    class App:
        def __init__(self, token=None):
            self._handlers = {}

        def on_message(self, fn=None):
            def deco(f):
                self._handlers["message"] = f
                return f
            return deco if fn is None else deco(fn)

        def on_message_submit_feedback(self, fn=None):
            def deco(f):
                self._handlers["feedback"] = f
                return f
            return deco if fn is None else deco(fn)

        async def start(self):
            # no-op in tests
            return

    class ActivityContext:
        # Make it usable in type annotations like ActivityContext[MessageActivity]
        def __class_getitem__(cls, item):
            return cls
        def __init__(self, activity):
            self.activity = activity
            class _Stream:
                def __init__(self):
                    self.data = []
                def emit(self, chunk: str):
                    self.data.append(chunk)
            self.stream = _Stream()

        async def send(self, activity_input):
            # no-op; test doesn't rely on send output
            return

    mt_apps.App = App
    mt_apps.ActivityContext = ActivityContext
    sys.modules["microsoft_teams.apps"] = mt_apps

    # microsoft_teams.openai
    mt_openai = ModuleType("microsoft_teams.openai")
    class OpenAICompletionsAIModel:
        def __init__(self, *args, **kwargs):
            pass
    mt_openai.OpenAICompletionsAIModel = OpenAICompletionsAIModel
    sys.modules["microsoft_teams.openai"] = mt_openai

    # microsoft_teams.api (types used in annotations and simple instantiation)
    mt_api = ModuleType("microsoft_teams.api")
    class MessageActivity: pass
    class MessageActivityInput:
        def __init__(self, text=""):
            self.text = text
        def add_ai_generated(self):
            return self
    class MessageSubmitActionInvokeActivity: pass
    mt_api.MessageActivity = MessageActivity
    mt_api.MessageActivityInput = MessageActivityInput
    mt_api.MessageSubmitActionInvokeActivity = MessageSubmitActionInvokeActivity
    sys.modules["microsoft_teams.api"] = mt_api


def build_context(aad_object_id: str, text: str="hello"):
    class _From:
        def __init__(self, aad):
            self.aadObjectId = aad
            self.id = aad
    class _Conversation:
        def __init__(self, cid):
            self.id = cid
    class _Activity:
        def __init__(self):
            self.text = text
            self.attachments = []
            self.from_property = _From(aad_object_id)
            self.channel_data = {}
            self.conversation = _Conversation("conv-1")

    # Use the stubbed ActivityContext from our fake SDK
    from microsoft_teams.apps import ActivityContext
    return ActivityContext(_Activity())


def main():
    # Ensure src is on path
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

    # Install SDK stubs before importing app.py
    install_teams_stubs()

    # Import app after stubbing
    import app as appmod

    # Capture logs from app logger
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.INFO)
    appmod.logger.setLevel(logging.INFO)
    appmod.logger.addHandler(handler)

    # Prime remembered user details with a known UPN
    aad_id = "123e4567-e89b-12d3-a456-426614174000"
    upn = "user@example.com"
    appmod.remember_user_details(aad_id, {
        "displayName": "Test User",
        "mail": upn,
        "aadObjectId": aad_id,
    })

    # Build context and run handler
    ctx = build_context(aad_id, text="hi")

    async def run_test():
        await appmod.handle_stateful_conversation(appmod.model, ctx)

    try:
        asyncio.run(run_test())
    except Exception as e:
        print(f"✗ Test execution error: {e}")
        sys.exit(1)

    # Evaluate logs
    logs = log_stream.getvalue()
    appmod.logger.removeHandler(handler)
    handler.close()

    if f"User UPN detected: {upn}" in logs:
        print("\n✓ UPN log detected:", f"User UPN detected: {upn}")
        print("✓ Test PASSED")
        sys.exit(0)
    else:
        print("\n✗ Expected UPN log not found. Logs were:\n")
        print(logs)
        sys.exit(2)


if __name__ == "__main__":
    main()
