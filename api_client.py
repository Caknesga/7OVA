import requests
from config import TEAM_KEY, BASE_URL

def get_scenarios():
    response = requests.get(
        f"{BASE_URL}/scenarios",
        headers={"Authorization": f"Bearer {TEAM_KEY}"},
        timeout=30
    )

    response.raise_for_status()
    return response.json()
