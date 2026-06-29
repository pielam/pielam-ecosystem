
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError

from apps.customer.validators import email_or_phone_validator
from django.contrib.auth import get_user_model

User = get_user_model()
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.account import User

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.account import User
from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product
from apps.notify.models.welcome_notify import WelcomeNotification

@login_required(login_url='/customer/signin/')
def NotificationView(request):
    user = request.user
    
    # User profile
    profile_info = get_object_or_404(ProfileInfo, user=user)
    followings_count = profile_info.following.count()
    followers_count = profile_info.followers.count()

    # Product counts for dealer role users
    products_listed_count = Product.objects.filter(dealer=user).count()
    active_products_count = Product.objects.filter(dealer=user, is_active=True).count()

    # Global stats
    brands_count = Brand.objects.count()
    categories_count = Category.objects.count()
    brands = Brand.objects.all()
    categories = Category.objects.all()

    # ---------------------------------------------------
    #  🔔 WELCOME NOTIFICATION (Display only once)
    # ---------------------------------------------------
    welcome = None
    if request.user.is_authenticated:
        try:
            welcome = request.user.welcome
        except WelcomeNotification.DoesNotExist:
            pass

    if welcome and not welcome.is_shown:
        welcome.is_shown = False
        welcome.save()
    # ---------------------------------------------------
    # In future: Add more notifications here
    # Example:
    # notifications.append({ "title": "New Order", "message": "You received an order.", "type": "info" })
    # ---------------------------------------------------

    context = {
        "user": user,
        "profile_info": profile_info,
        "followings_count": followings_count,
        "followers_count": followers_count,
        "products_listed_count": products_listed_count,
        "active_products_count": active_products_count,
        "brands_count": brands_count,
        "categories_count": categories_count,
        "brands": brands,
        "categories": categories,

        # NEW: Pass notifications to template
        "welcome": welcome,
    }

    return render(request, "notify/notification.html", context)
