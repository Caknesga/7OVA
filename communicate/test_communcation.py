import asyncio
from client import TargetAPIClient


async def main():
    async with TargetAPIClient() as client:
        scenarios = await client.get_scenarios()
        channels = await client.get_channels()

        print("Available scenarios:", scenarios)
        print("Available channels:", channels)


if __name__ == "__main__":
    asyncio.run(main())