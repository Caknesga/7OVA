
import asyncio
import uuid

from communicate.client import TargetAPIClient
from communicate.agent import CommunicationAgent

import json
from pathlib import Path
from datetime import datetime, timezone


async def main():
    async with TargetAPIClient() as client:

        # 1. Create practice session
        session = await client.create_session("practice")
        session_id = "s_79af691e30"

        print("Session created:", session_id)

        # 2. Simulated output from Planning Agent
        planned_action = {
            "action_type": "send_message",
            "target_id": "robert",
            "channel": "email",
            "subject": "Communication system test",
            "body": (
                "Hello, this is a test of our "
                "communication system. "
                "Could you confirm receipt?"
            ),
        }

        # 3. Simulated input from Orchestrator
        execution_context = {
            "session_id": session_id,
            "request_id": str(uuid.uuid4()),
            "remaining_touches": (
                session["touch_budget"]
                - session["touches_used"]
            ),
        }

        # 4. Execute Communication Agent
        agent = CommunicationAgent(client)

        result = await agent.execute(
            planned_action=planned_action,
            execution_context=execution_context,
        )
        # Save the action and its observable result
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "planned_action": planned_action,
            "execution_context": {
                "session_id": session_id,
                "request_id": execution_context["request_id"]
            },
            "communication_result": result.model_dump(mode="json")
        }

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        log_file = log_dir / f"{session_id}.jsonl"

        with log_file.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(log_entry, ensure_ascii=False) + "\n"
            )

        print("Interaction saved:", log_file)

        # 5. Display structured output
        print("\nCOMMUNICATION RESULT:")
        print(result.model_dump_json(indent=2))

        # 6. Verify updated simulator state
        updated_session = await client.get_session(session_id)
        print("\nTouches used:", updated_session["touches_used"])


if __name__ == "__main__":
    asyncio.run(main())
