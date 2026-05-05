import os
import logging
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from azure.ai.projects import AIProjectClient
from azure.identity import ManagedIdentityCredential

logger = logging.getLogger(__name__)


class FabricOptimizerBot(ActivityHandler):
    def __init__(self):
        self._conversations = {}  # user_id -> previous_response_id
        self._project_endpoint = os.environ.get(
            "AZURE_AI_PROJECT_ENDPOINT",
            "https://ai-account-hungbvr6ykz3u.services.ai.azure.com/api/projects/ai-project-fabric-mcp-foundry",
        )
        self._agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "fabric-optimizer")
        self._credential = ManagedIdentityCredential(
            client_id=os.environ.get("AZURE_CLIENT_ID")
        )

    async def on_message_activity(self, turn_context: TurnContext):
        user_id = turn_context.activity.from_property.id
        user_text = turn_context.activity.text or ""

        try:
            client = AIProjectClient(
                endpoint=self._project_endpoint,
                credential=self._credential,
            )
            openai_client = client.get_openai_client()

            # Build request with conversation state
            extra_body = {
                "agent_reference": {
                    "name": self._agent_name,
                    "type": "agent_reference",
                }
            }

            # Include previous response ID for multi-turn
            prev_id = self._conversations.get(user_id)
            if prev_id:
                extra_body["previous_response_id"] = prev_id

            response = openai_client.responses.create(
                model="gpt-4o",
                input=user_text,
                extra_body=extra_body,
            )

            # Store response ID for multi-turn
            self._conversations[user_id] = response.id

            reply_text = response.output_text or "No response from agent."
            await turn_context.send_activity(
                Activity(type=ActivityTypes.message, text=reply_text)
            )

        except Exception as e:
            logger.error(f"Bot error: {e}")
            await turn_context.send_activity(f"Error: {str(e)}")

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        if turn_context.activity.members_added:
            for member in turn_context.activity.members_added:
                if member.id != turn_context.activity.recipient.id:
                    await turn_context.send_activity(
                        "Hi! I'm the Fabric Optimizer Bot. I scan your workspaces "
                        "for optimization issues and can apply fixes with your approval."
                    )
