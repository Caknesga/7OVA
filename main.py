
import json
from api_client import get_scenarios

scenarios = get_scenarios()
print(json.dumps(scenarios, indent=4))