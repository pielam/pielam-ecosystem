from django.urls import path
from apps.customer.views.signup import SignUpView
from apps.customer.views.signin import SignInView
from apps.customer.views.profile import ProfileView
from apps.customer.views.logout import LogoutView
from apps.customer.views.profile_edit import EditProfileView
from apps.customer.views.profile_managers import ProfileManagerView
from apps.customer.views.forgot_password import ForgotPasswordView
from apps.customer.views.public_profile import PublicProfileView, follow, unfollow, load_more_products, load_more_services
from apps.customer.views.public_profile import WishlistToggleView

from apps.customer.views.dashboard import (
       ProfessionalDashboardView,
       ProfessionalDashboardTemplateView,
   )

from apps.customer.views.cover_photo import UpdateCoverPhotoView
from apps.customer.views.profile_photo import UpdateProfilePhotoView


from apps.customer.views.campaign_profile import CampaignDetailView, CampaignProfileView

app_name = "customer"

urlpatterns = [
    path('signup/', SignUpView, name='signup'),

    path('signin/', SignInView, name='signin'),
    path("forgot-password/", ForgotPasswordView, name="forgot-password"),
    
     # Logout URL
    path('logout/', LogoutView, name='logout'),

    path('profile/', ProfileView, name='profile'),  # <-- Add this line
    path("update_cover_photo/", UpdateCoverPhotoView, name="update_cover_photo"),
    path("update_profile_photo/", UpdateProfilePhotoView, name="update_profile_photo"),
    path("edit_profile/", EditProfileView, name="edit_profile"),
    path("profile_manager/", ProfileManagerView, name="profile_manager"),

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