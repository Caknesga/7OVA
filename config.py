import os
from dotenv import load_dotenv

load_dotenv()

TEAM_KEY = os.getenv("TEAM_KEY")
BASE_URL = os.getenv("BASE_URL")