"""
Complete Identity Profile View with all necessary context
Enhanced with dynamic trust score, profile data, and better structure
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
import logging
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from django.shortcuts import render, get_object_or_404
import requests

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
import json
from megamind.models.connected_service import ConnectedService

logger = logging.getLogger(__name__)


@login_required
def IdentitySettingsView(request):
    """
    Enterprise-level identity view with data validation and comprehensive user data
    """
    user = request.user
    
    # ========== DATA VALIDATION & FIXES ==========
    # Retrieve the user's profile or 404 if not found
    profile_info = get_object_or_404(ProfileInfo, user=request.user)
    # Fix identity type mismatch if exists
    if user.email_or_phone:
        if '@' in user.email_or_phone and user.identity_type != 'email':
            logger.warning(f"Identity type mismatch for user {user.user_uuid}: email_or_phone is email but identity_type is {user.identity_type}")
            user.identity_type = 'email'
            user.save(update_fields=['identity_type'])
        elif user.email_or_phone.startswith('+') and user.identity_type != 'phone':
            logger.warning(f"Identity type mismatch for user {user.user_uuid}: email_or_phone is phone but identity_type is {user.identity_type}")
            user.identity_type = 'phone'
            user.save(update_fields=['identity_type'])
    
    # ========== CALCULATE ACCOUNT AGE ==========
    
    account_age = timezone.now() - user.created_at
    account_age_days = max(0, account_age.days)
    
    # Human-readable account age
    if account_age_days == 0:
        account_age_display = "Today"
    elif account_age_days == 1:
        account_age_display = "1 day"
    elif account_age_days < 30:
        account_age_display = f"{account_age_days} days"
    elif account_age_days < 365:
        months = account_age_days // 30
        account_age_display = f"{months} month{'s' if months > 1 else ''}"
    else:
        years = account_age_days // 365
        account_age_display = f"{years} year{'s' if years > 1 else ''}"
    
    # ========== VERIFICATION STATE ==========
    
    
   
    # ========== DYNAMIC TRUST SCORE CALCULATION ==========
    
    trust_score = 0
    max_score = 100
    
    # Primary identity verified (30 points)
   
    
    
    # Security keys active (20 points)
    
    
    # Encryption key present (10 points)
   
    
    # No failed login attempts (10 points)
    if user.failed_login_attempts == 0:
        trust_score += 10
    elif user.failed_login_attempts < 3:
        trust_score += 5
    
    # Account age (5 points)
    if account_age_days > 30:
        trust_score += 5
    elif account_age_days > 7:
        trust_score += 3
    
    # Password changed recently (5 points)
    if user.last_password_change:
        password_age = timezone.now() - user.last_password_change
        if password_age.days < 90:
            trust_score += 5
    
    # Account active (5 points)
    
    
    # Ensure score is within bounds
    trust_score = min(trust_score, max_score)
    
    # ========== SECURITY ALERTS ==========
    
    security_alerts = []
    
    # Failed login attempts
    if user.failed_login_attempts > 0:
        if user.failed_login_attempts >= 5:
            alert_type = 'danger'
        elif user.failed_login_attempts >= 3:
            alert_type = 'warning'
        else:
            alert_type = 'info'
        
        security_alerts.append({
            'type': alert_type,
            'icon': 'exclamation-triangle',
            'message': f"{user.failed_login_attempts} failed login attempt(s) detected"
        })
    
    # Account locked
    
    
    # Password age warning
    if user.last_password_change:
        password_age = timezone.now() - user.last_password_change
        if password_age.days > 180:
            security_alerts.append({
                'type': 'danger',
                'icon': 'key',
                'message': f"Password is {password_age.days} days old. Change it immediately!"
            })
        elif password_age.days > 90:
            security_alerts.append({
                'type': 'warning',
                'icon': 'key',
                'message': f"Password is {password_age.days} days old. Consider changing it."
            })
    else:
        security_alerts.append({
            'type': 'warning',
            'icon': 'shield-exclamation',
            'message': "No password change history. Consider updating your password."
        })
    
    # Unverified account warning
    
    
    # ========== ACTIVITY TRACKING ==========
    
    # Last login display
    if user.last_login_at:
        last_login_delta = timezone.now() - user.last_login_at
        if last_login_delta.days == 0:
            if last_login_delta.seconds < 3600:
                minutes = last_login_delta.seconds // 60
                last_login_display = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                hours = last_login_delta.seconds // 3600
                last_login_display = f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif last_login_delta.days == 1:
            last_login_display = "Yesterday"
        elif last_login_delta.days < 7:
            last_login_display = f"{last_login_delta.days} days ago"
        else:
            last_login_display = user.last_login_at.strftime('%b %d, %Y')
    else:
        last_login_display = "Never"
    
    # Last activity display
    if user.last_activity_at:
        activity_delta = timezone.now() - user.last_activity_at
        if activity_delta.days == 0:
            if activity_delta.seconds < 3600:
                minutes = activity_delta.seconds // 60
                last_activity_display = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                hours = activity_delta.seconds // 3600
                last_activity_display = f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif activity_delta.days == 1:
            last_activity_display = "Yesterday"
        else:
            last_activity_display = user.last_activity_at.strftime('%b %d, %Y')
    else:
        last_activity_display = "No activity tracked"
    
    # ========== PROFILE INFO ==========
    
    # Extract display name from email_or_phone
    if '@' in user.email_or_phone:
        display_name = user.email_or_phone.split('@')[0].replace('.', ' ').replace('_', ' ').title()
    else:
        # For phone numbers, just show the identifier
        display_name = user.email_or_phone
    
    # Get initials for avatar
    initials = display_name[0].upper() if display_name else 'U'
    
    
    
    # ========== STATUS BADGES ==========
    
    badges = []
    
    # Active/Inactive Badge
    if user.is_deleted:
        badges.append({
            'label': 'Deleted',
            'color': 'danger',
            'icon': 'trash',
            'priority': 1
        })
    
    elif user.is_active:
        badges.append({
            'label': 'Active',
            'color': 'success',
            'icon': 'check-circle',
            'priority': 10
        })
    else:
        badges.append({
            'label': 'Inactive',
            'color': 'warning',
            'icon': 'pause-circle',
            'priority': 3
        })
    
    # Verification Badge
    
    
    # New Account Badge
    if account_age_days < 7:
        badges.append({
            'label': f'{account_age_days} Day{"s" if account_age_days != 1 else ""} Old',
            'color': 'info',
            'icon': 'clock',
            'priority': 8
        })
    
    # Failed Logins Badge
    if user.failed_login_attempts > 0:
        badges.append({
            'label': f'{user.failed_login_attempts} Failed Login{"s" if user.failed_login_attempts != 1 else ""}',
            'color': 'danger' if user.failed_login_attempts >= 5 else 'warning',
            'icon': 'exclamation-triangle',
            'priority': 5
        })
    
    # JWT Keys Badge
   
    
    # Sort badges by priority
    badges.sort(key=lambda x: x['priority'])
    
    # ========== 2FA STATUS ==========
    
    # TODO: Add is_2fa_enabled field to User model
    # For now, check session or return False
    is_2fa_enabled = getattr(user, 'is_2fa_enabled', False) or request.session.get('2fa_enabled', False)
    
    # ========== USER DATA FOR TEMPLATE ==========
    
    user_data = {
        # Identity
        'uuid': str(user.user_uuid),
        'email_or_phone': user.email_or_phone,
        'identity_type': user.identity_type,
        'identity_type_display': user.identity_type.title(),
        
        # Contact
       
       
        
        # Verification
       
        # Role & Permissions
        'role': user.get_role_display(),
        'role_raw': user.role,
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        
        # Status
        'is_active': user.is_active,
        'is_deleted': user.is_deleted,
       
        'deleted_at': user.deleted_at,
        'locked_until': user.locked_until,
        'lock_reason': user.lock_reason,
        
        # Timestamps
        'created_at': user.created_at,
        'updated_at': user.updated_at,
        'last_login': user.last_login_at,
        'last_login_display': last_login_display,
        'last_activity': user.last_activity_at,
        'last_activity_display': last_activity_display,
        'last_password_change': user.last_password_change,
        
        # Preferences
        'timezone': user.user_timezone,
        
        # Permissions
        'groups': user.groups.all(),
        'user_permissions': user.user_permissions.all(),
    }
    
    # ========== STATISTICS ==========
    
    stats = {
        'account_age_days': account_age_days,
        'account_age_display': account_age_display,
        'failed_attempts': user.failed_login_attempts,
       
        
        'is_2fa_enabled': is_2fa_enabled,
        'trust_score': trust_score,  # NEW: Dynamic trust score
    }
    
    # ========== NOTIFICATION COUNT ==========
    
    # TODO: Implement actual notification system
    notification_count = 0
    
    services = ConnectedService.objects.filter(user=request.user)
    # ========== CONTEXT ==========
    
    context = {
        'user': user,
        'profile_info': profile_info,
        'user_data': user_data,
        'stats': stats,
        'badges': badges,
        'security_alerts': security_alerts,
        'page_title': 'Identity Profile',
        'profile_info': profile_info,
        'notification_count': notification_count,
        'services': services,
    }
    
    return render(request, "security/identity_settings.html", context)


@login_required
@require_http_methods(["POST"])
def add_service(request):
    try:
        data = json.loads(request.body)
        service_name = data.get('service_name')
        service_url = data.get('service_url')
        service_type = data.get('service_type', 'other')
        
        if not service_name or not service_url:
            return JsonResponse({'success': False, 'error': 'Service name and URL are required'}, status=400)
        
        service = ConnectedService.objects.create(
            user=request.user,
            service_name=service_name,
            service_url=service_url,
            service_type=service_type,
            is_connected=False,
            status='private'
        )
        
        return JsonResponse({
            'success': True,
            'service': {
                'id': service.id,
                'service_name': service.service_name,
                'service_url': service.service_url,
                'service_type': service.service_type,
                'is_connected': service.is_connected,
                'status': service.status
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def toggle_service_connection(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.is_connected = not service.is_connected
        service.save()
        
        # If connecting, try to fetch data immediately
        if service.is_connected:
            fetch_service_data(service)
        
        return JsonResponse({
            'success': True,
            'is_connected': service.is_connected,
            'status': service.status
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_http_methods(["DELETE"])
def delete_service(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.delete()
        
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def fetch_service_data(service):
    """
    Fetch data from the service URL
    This function attempts to fetch and parse data from the connected service
    """
    print(f"[FETCH] Starting fetch for: {service.service_name} - {service.service_url}")
    
    try:
        import requests
        from bs4 import BeautifulSoup
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Add authentication if available
        if service.api_key:
            headers['Authorization'] = f'Bearer {service.api_key}'
        
        print(f"[FETCH] Making request to: {service.service_url}")
        
        # Make request with timeout
        response = requests.get(service.service_url, headers=headers, timeout=15, verify=True)
        response.raise_for_status()
        
        print(f"[FETCH] Response status: {response.status_code}")
        print(f"[FETCH] Content-Type: {response.headers.get('Content-Type')}")
        
        content_type = response.headers.get('Content-Type', '').lower()
        
        # Try to parse as JSON first (for API endpoints)
        if 'application/json' in content_type or service.service_url.endswith('.json'):
            try:
                data = response.json()
                print(f"[FETCH] Successfully parsed as JSON")
                service.last_fetched_data = {
                    'type': 'json',
                    'content': json.dumps(data, indent=2),
                    'raw_data': data
                }
            except json.JSONDecodeError as e:
                print(f"[FETCH] JSON decode error: {e}")
                raise
        else:
            # Parse as HTML
            print(f"[FETCH] Parsing as HTML")
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract useful information
            title = soup.find('title')
            title_text = title.get_text().strip() if title else 'No title'
            
            # Get meta description
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if not meta_desc:
                meta_desc = soup.find('meta', attrs={'property': 'og:description'})
            description = meta_desc.get('content', '').strip() if meta_desc else ''
            
            # Extract main content (remove scripts and styles)
            for script in soup(['script', 'style', 'nav', 'footer', 'header']):
                script.decompose()
            
            # Try to find main content area
            main_content = soup.find('main') or soup.find('article') or soup.find('body')
            
            if main_content:
                # Get text content
                text_content = main_content.get_text()
                lines = (line.strip() for line in text_content.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = '\n'.join(chunk for chunk in chunks if chunk)
            else:
                text = soup.get_text()
            
            # Limit text length
            text = text[:5000] if len(text) > 5000 else text
            
            print(f"[FETCH] HTML parsed - Title: {title_text[:50]}")
            
            service.last_fetched_data = {
                'type': 'html',
                'title': title_text,
                'description': description,
                'content': text,
                'url': service.service_url,
                'content_length': len(response.text)
            }
        
        service.last_fetch_time = timezone.now()
        service.fetch_status = 'success'
        service.fetch_error = None
        service.save()
        
        print(f"[FETCH] Successfully saved data for: {service.service_name}")
        return True
        
    except requests.exceptions.SSLError as e:
        error_msg = f"SSL Error: {str(e)}"
        print(f"[FETCH ERROR] {error_msg}")
        service.fetch_status = 'error'
        service.fetch_error = error_msg
        service.last_fetch_time = timezone.now()
        service.save()
        return False
        
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Connection Error: Cannot reach {service.service_url}"
        print(f"[FETCH ERROR] {error_msg}")
        service.fetch_status = 'error'
        service.fetch_error = error_msg
        service.last_fetch_time = timezone.now()
        service.save()
        return False
        
    except requests.exceptions.Timeout as e:
        error_msg = f"Timeout Error: Request took too long"
        print(f"[FETCH ERROR] {error_msg}")
        service.fetch_status = 'error'
        service.fetch_error = error_msg
        service.last_fetch_time = timezone.now()
        service.save()
        return False
        
    except requests.exceptions.RequestException as e:
        error_msg = f"Request Error: {str(e)}"
        print(f"[FETCH ERROR] {error_msg}")
        service.fetch_status = 'error'
        service.fetch_error = error_msg
        service.last_fetch_time = timezone.now()
        service.save()
        return False
        
    except Exception as e:
        error_msg = f"Unexpected Error: {type(e).__name__} - {str(e)}"
        print(f"[FETCH ERROR] {error_msg}")
        service.fetch_status = 'error'
        service.fetch_error = error_msg
        service.last_fetch_time = timezone.now()
        service.save()
        return False

