from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)

        email = data.get("email")

        # Map Google email to email_or_phone
        user.email = email
        user.email_or_phone = email
        user.role = "customer"

        return user
