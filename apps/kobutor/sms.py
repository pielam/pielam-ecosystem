# apps/kobutor/sms.py

def send_welcome_sms(to_phone):
    # Replace this with your actual SMS provider API call
    # For now, just simulate sending SMS with a print statement
    print(f"Sending welcome SMS to: {to_phone}")

    # Example if you use a real provider like Twilio:
    # from twilio.rest import Client
    #
    # client = Client(account_sid, auth_token)
    # message = client.messages.create(
    #     body="Welcome to Pielam! Your account has been created successfully.",
    #     from_="+1234567890",  # Your Twilio number
    #     to=to_phone
    # )
