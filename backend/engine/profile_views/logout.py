"""
Enhanced Logout View
- Secure logout with session cleanup
- Direct logout without confirmation
- Proper security measures
- Activity logging
"""

from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.views.decorators.http import require_http_methods
from django.utils import timezone
import logging

from backend.megamind.models.engine_users import User

logger = logging.getLogger(__name__)


@require_http_methods(["GET"])
def LogoutView(request):
    """
    Direct logout view - works with GET request from link
    No confirmation needed for better UX
    """
    
    # If user is not authenticated, redirect to signin
    if not request.user.is_authenticated:
        messages.info(request, "You are not logged in.")
        return redirect('signin')
    
    # Store user info before logout for logging
    user_identifier = request.user.email_or_phone
    user_uuid = request.user.user_uuid
    
    try:
        # Update last activity before logout
        if isinstance(request.user, User):
            request.user.update_last_activity()
        
        # Log the logout action
        logger.info(f"User logged out: {user_uuid} ({user_identifier})")
        
        # Perform Django logout (clears session)
        auth_logout(request)
        
        # Success message
        messages.success(request, "You have been logged out successfully. See you soon!")
        
        # Redirect to signin page
        return redirect('signin')
        
    except Exception as e:
        logger.error(f"Error during logout for user {user_uuid}: {e}", exc_info=True)
        
        # Force logout even if there's an error
        auth_logout(request)
        messages.warning(request, "You have been logged out.")
        return redirect('signin')


# ========== ALTERNATIVE: DIRECT LOGOUT (NO CONFIRMATION) ==========

@require_http_methods(["GET", "POST"])
def LogoutViewDirect(request):
    """
    Direct logout without confirmation page
    Works with both GET and POST requests
    """
    
    if not request.user.is_authenticated:
        messages.info(request, "You are not logged in.")
        return redirect('signin')
    
    # Store user info for logging
    user_identifier = getattr(request.user, 'email_or_phone', 'Unknown')
    user_uuid = getattr(request.user, 'user_uuid', 'Unknown')
    
    try:
        # Update last activity
        if isinstance(request.user, User):
            request.user.update_last_activity()
        
        # Log the logout
        logger.info(f"User logged out: {user_uuid} ({user_identifier})")
        
        # Perform logout
        auth_logout(request)
        
        # Success message
        messages.success(request, "You have been logged out successfully. See you soon!")
        
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        auth_logout(request)
        messages.warning(request, "You have been logged out.")
    
    return redirect('signin')


# ========== LOGOUT WITH REDIRECT PARAMETER ==========

@require_http_methods(["GET", "POST"])
def LogoutViewWithRedirect(request):
    """
    Logout with custom redirect support
    Usage: /logout/?next=/custom-page/
    """
    
    if not request.user.is_authenticated:
        return redirect('signin')
    
    # Get redirect URL from query parameter
    next_url = request.GET.get('next') or request.POST.get('next') or 'signin'
    
    # Whitelist allowed redirect URLs for security
    allowed_redirects = ['signin', 'home', 'landing']
    
    # Validate redirect URL
    if next_url not in allowed_redirects and not next_url.startswith('/'):
        next_url = 'signin'
    
    # Store user info
    user_identifier = getattr(request.user, 'email_or_phone', 'Unknown')
    user_uuid = getattr(request.user, 'user_uuid', 'Unknown')
    
    try:
        # Update last activity
        if isinstance(request.user, User):
            request.user.update_last_activity()
        
        # Log the logout
        logger.info(f"User logged out: {user_uuid} ({user_identifier})")
        
        # Perform logout
        auth_logout(request)
        
        messages.success(request, "You have been logged out successfully.")
        
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        auth_logout(request)
        messages.warning(request, "You have been logged out.")
    
    return redirect(next_url)