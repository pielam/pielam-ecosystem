# apps/customer/web_urls.py

from django.urls import path
from apps.customer.web_views.signup import SignUpView, VerifyEmailView, VerifyPhoneView

from apps.customer.web_views.signin import SignInView
from apps.customer.web_views.profile import ProfileView
from apps.customer.web_views.logout import LogoutView
from apps.customer.web_views.profile_edit import EditProfileView
from apps.customer.web_views.profile_managers import ProfileManagerView, contact_oauth_callback, contact_oauth_start
from apps.customer.web_views.forgot_password import ForgotPasswordView, VerifyForgotPasswordOTPView, ResetPasswordView
from apps.customer.web_views.public_profile import PublicProfileView, follow, unfollow, load_more_products, load_more_services
from apps.customer.web_views.public_profile import WishlistToggleView

from apps.customer.web_views.dashboard import (
       ProfessionalDashboardView,
       ProfessionalDashboardTemplateView,
   )

from apps.customer.web_views.cover_photo import UpdateCoverPhotoView
from apps.customer.web_views.profile_photo import UpdateProfilePhotoView


from apps.customer.web_views.campaign_profile import CampaignDetailView, CampaignProfileView

app_name = "customer"

urlpatterns = [

    path('signup/', SignUpView, name='signup'), # user Registration with email or phone number
    # Signup verification (email link + phone OTP)
    path('verify-email/<str:uidb64>/<str:token>/', VerifyEmailView, name='verify-email'),
    path('verify-phone/', VerifyPhoneView, name='verify-phone'),

    path('signin/', SignInView, name='signin'), # user signin with email or phone number
    path("forgot-password/", ForgotPasswordView, name="forgot-password"),
    path("verify-forgot-password-otp/", VerifyForgotPasswordOTPView, name="verify-forgot-password-otp"),
    path("reset-password/", ResetPasswordView, name="reset-password"),

     # Logout URL
    path('logout/', LogoutView, name='logout'),

    path('profile/', ProfileView, name='profile'),  # user personal profile
    path("update_cover_photo/", UpdateCoverPhotoView, name="update_cover_photo"),
    path("update_profile_photo/", UpdateProfilePhotoView, name="update_profile_photo"),
    path("edit_profile/", EditProfileView, name="edit_profile"),
    path("profile_manager/", ProfileManagerView, name="profile_manager"),
    # urls.py additions
    path("user/contact-oauth/<str:channel>/start/", contact_oauth_start, name="contact_oauth_start"),
    path("user/contact-oauth/<str:channel>/callback/", contact_oauth_callback, name="contact_oauth_callback"),

    # user's profile visible to all user
    path('profile_view/<str:username>/', PublicProfileView, name='profile_view'),
    path('profile/<str:username>/products/load-more/', load_more_products, name='load_more_products'),
    path('profile/<str:username>/services/load-more/', load_more_services, name='load_more_services'),
    path('follow/<str:username>/', follow, name='follow'),
    path('unfollow/<str:username>/', unfollow, name='unfollow'),

    path('wishlist/toggle/<uuid:product_id>/', WishlistToggleView, name='wishlist_toggle'),


    path("api/dashboard/", ProfessionalDashboardView.as_view(), name="dashboard-api"),
    path("dashboard/", ProfessionalDashboardTemplateView.as_view(), name="dashboard"),

    # Campaigns
    path('campaign_profile/', CampaignProfileView, name='campaign_profile'),
    path('campaign/<slug:slug>/', CampaignDetailView, name='campaign_detail'),
]
