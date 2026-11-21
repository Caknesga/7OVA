from target_api import TargetAPI

api = TargetAPI()  # reads TEAM_KEY from env

session = api.new_session("practice")

result = api.send_message(
    session.session_id,
    to="julius",
    channel="email",
    from_name="Laura Fend",
    from_address="laura.fend@deeptechforum-events.de",
    subject="DeepTech Forum Munich — confirming your keynote slot",
    body="Hi Julius, ...",
)
print(result["events"])