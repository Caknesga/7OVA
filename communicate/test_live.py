
import asyncio
import uuid
import json
from pathlib import Path

from communicate.client import TargetAPIClient
from communicate.agent import CommunicationAgent


async def main():
    async with TargetAPIClient() as client:

        # Reuse existing practice session
        session_id = "s_4028bb0392"

        session = await client.get_session(session_id)
        agent = CommunicationAgent(client)

        # STEP 1: Start live conversation
        start = await agent.execute(
            planned_action={
                "action_type": "start_conversation",
                "target_id": "robert",
                "channel": "live_chat",
                "sender": {"name": "QA Tester"}
            },
            execution_context={
                "session_id": session_id,
                "request_id": str(uuid.uuid4()),
                "remaining_touches": (
                    session["touch_budget"] -
                    session["touches_used"]
                )
            }
        )

        if not start.success:
            print("Error:", start.error)
            return

        conversation_id = start.conversation_id

        print("\nDIGITAL TWIN:", start.persona_says)

        # STEP 2: Send a message and receive a response
        message = (
            "Hello! I'm testing our communication interface. "
            "Could you confirm that you can see this message?"
        )

        turn = await agent.execute(
            planned_action={
                "action_type": "send_turn",
                "say": message
            },
            execution_context={
                "session_id": session_id,
                "conversation_id": conversation_id,
                "request_id": str(uuid.uuid4()),
                "remaining_conversation_turns": 10
            }
        )

        print("\nOUR AGENT:", message)

        if not turn.success:
            print("Error:", turn.error)
        else:
            print("\nDIGITAL TWIN:", turn.persona_says)

        # STEP 3: Save transcript for Evaluation Agent
        log = {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "channel": "live_chat",
            "initial_response": start.model_dump(mode="json"),
            "sent_message": message,
            "turn_response": turn.model_dump(mode="json")
        }

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        path = log_dir / f"{session_id}_live.json"
        path.write_text(
            json.dumps(log, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        # STEP 4: End conversation
        await agent.execute(
            planned_action={
                "action_type": "end_conversation"
            },
            execution_context={
                "session_id": session_id,
                "conversation_id": conversation_id
            }
        )

        print("\nTranscript saved:", path)


if __name__ == "__main__":
    asyncio.run(main())
