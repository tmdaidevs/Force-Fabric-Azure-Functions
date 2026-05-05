import os
import logging
import requests
from auth.fabric_auth import get_token_for_scope

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

logger = logging.getLogger(__name__)


def _get_graph_token() -> str:
    return get_token_for_scope(GRAPH_SCOPE)


def get_user_by_id(user_id: str) -> dict:
    """Get user details from Graph API."""
    token = _get_graph_token()
    resp = requests.get(
        f"{GRAPH_BASE}/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_proactive_message(
    user_id: str, message: str, bot_app_id: str = None
) -> dict:
    """Send a proactive Teams message to a user via Graph API.
    Creates a 1:1 chat and sends the message.
    """
    token = _get_graph_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    bot_id = bot_app_id or os.environ.get("MicrosoftAppId", "")

    # Create or get 1:1 chat
    chat_body = {
        "chatType": "oneOnOne",
        "members": [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{user_id}')",
            },
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{bot_id}')",
            },
        ],
    }

    chat_resp = requests.post(
        f"{GRAPH_BASE}/chats", headers=headers, json=chat_body, timeout=30
    )
    chat_resp.raise_for_status()
    chat_id = chat_resp.json()["id"]

    # Send message
    msg_body = {"body": {"contentType": "html", "content": message}}
    msg_resp = requests.post(
        f"{GRAPH_BASE}/chats/{chat_id}/messages",
        headers=headers,
        json=msg_body,
        timeout=30,
    )
    msg_resp.raise_for_status()
    return msg_resp.json()
