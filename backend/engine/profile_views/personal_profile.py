"""
Fixed Profile View - Resolves all data inconsistency and display issues
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
import logging
from datetime import date
logger = logging.getLogger(__name__)
from django.shortcuts import render, get_object_or_404
from apps.customer.models.profile_info import ProfileInfo

@login_required
def ProfileView(request):
    """
    Display the current user's profile information.
    """

    # Retrieve the user's profile or 404 if not found
    profile_info = get_object_or_404(ProfileInfo, user=request.user)

    # Followers count (users who follow this profile)
    followers_count = profile_info.followers.count()

    # Followings count (profiles that the current user is following)
    followings_count = request.user.following_profiles.count()

    # Subscribers count (users subscribed to this profile)
    subscribers_count = profile_info.subscribers.count()

     # Age calculation
    age = None
    if profile_info.profile_dob:
        today = date.today()
        age = today.year - profile_info.profile_dob.year
        # Adjust if birthday hasn't occurred yet this year
        if (today.month, today.day) < (profile_info.profile_dob.month, profile_info.profile_dob.day):
            age -= 1

    context = {
        'profile_info': profile_info,
        'followers_count': followers_count,
        'followings_count': followings_count,
        'subscribers_count': subscribers_count,
        'age': age,
    }

    return render(request, 'personal/personal_profile.html', context)
