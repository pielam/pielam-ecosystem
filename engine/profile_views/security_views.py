"""
Security management views for password, 2FA, and session management
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.core.exceptions import ValidationError
import pyotp
import qrcode
import io
import base64
import logging

logger = logging.getLogger(__name__)


# ==================== PASSWORD MANAGEMENT ====================

@login_required
@require_POST
def ChangePasswordView(request):
    """
    Change user password with validation
    """
    try:
        current_password = request.POST.get('current_password')
        new_password = request.POST.get('new_password')
        confirm_password = request.POST.get('confirm_password')
        
        # Validation
        if not all([current_password, new_password, confirm_password]):
            return JsonResponse({
                'success': False,
                'message': 'All fields are required'
            }, status=400)
        
        # Check current password
        if not request.user.check_password(current_password):
            return JsonResponse({
                'success': False,
                'message': 'Current password is incorrect'
            }, status=400)
        
        # Check new passwords match
        if new_password != confirm_password:
            return JsonResponse({
                'success': False,
                'message': 'New passwords do not match'
            }, status=400)
        
        # Check password strength (basic)
        if len(new_password) < 8:
            return JsonResponse({
                'success': False,
                'message': 'Password must be at least 8 characters long'
            }, status=400)
        
        # Change password
        request.user.change_password(new_password)
        
        # Update session to prevent logout
        update_session_auth_hash(request, request.user)
        
        logger.info(f"Password changed successfully for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'Password changed successfully'
        })
        
    except Exception as e:
        logger.error(f"Password change error for user {request.user.user_uuid}: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred. Please try again.'
        }, status=500)


# ==================== TWO-FACTOR AUTHENTICATION ====================

@login_required
@require_http_methods(["GET", "POST"])
def Setup2FAView(request):
    """
    Setup Two-Factor Authentication (TOTP)
    """
    if request.method == 'GET':
        # Generate secret key for TOTP
        secret = pyotp.random_base32()
        request.session['temp_2fa_secret'] = secret
        
        # Generate QR code
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
            name=request.user.email_or_phone,
            issuer_name='PIELAM'
        )
        
        # Create QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(totp_uri)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return JsonResponse({
            'success': True,
            'secret': secret,
            'qr_code': f'data:image/png;base64,{qr_base64}',
            'manual_entry': secret
        })
    
    # POST - Not used in this implementation
    return JsonResponse({'success': False, 'message': 'Invalid request'}, status=400)


@login_required
@require_POST
def Verify2FAView(request):
    """
    Verify and enable 2FA
    """
    try:
        code = request.POST.get('code')
        secret = request.session.get('temp_2fa_secret')
        
        if not secret:
            return JsonResponse({
                'success': False,
                'message': '2FA setup session expired. Please try again.'
            }, status=400)
        
        # Verify TOTP code
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return JsonResponse({
                'success': False,
                'message': 'Invalid verification code'
            }, status=400)
        
        # Save 2FA secret to user (you'll need to add this field to User model)
        # For now, we'll store it in session as placeholder
        request.session['2fa_enabled'] = True
        request.session['2fa_secret'] = secret
        
        # Clear temp secret
        del request.session['temp_2fa_secret']
        
        logger.info(f"2FA enabled for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'Two-Factor Authentication enabled successfully'
        })
        
    except Exception as e:
        logger.error(f"2FA verification error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred. Please try again.'
        }, status=500)


@login_required
@require_POST
def Disable2FAView(request):
    """
    Disable Two-Factor Authentication
    """
    try:
        password = request.POST.get('password')
        
        if not request.user.check_password(password):
            return JsonResponse({
                'success': False,
                'message': 'Incorrect password'
            }, status=400)
        
        # Remove 2FA
        request.session['2fa_enabled'] = False
        if '2fa_secret' in request.session:
            del request.session['2fa_secret']
        
        logger.info(f"2FA disabled for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'Two-Factor Authentication disabled'
        })
        
    except Exception as e:
        logger.error(f"2FA disable error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred'
        }, status=500)


# ==================== SESSION MANAGEMENT ====================

@login_required
def SessionManagementView(request):
    """
    View all active sessions for the user
    """
    try:
        # Get all active sessions
        sessions = Session.objects.filter(expire_date__gte=timezone.now())
        
        user_sessions = []
        current_session_key = request.session.session_key
        
        for session in sessions:
            data = session.get_decoded()
            if data.get('_auth_user_id') == str(request.user.pk):
                user_sessions.append({
                    'session_key': session.session_key,
                    'is_current': session.session_key == current_session_key,
                    'expire_date': session.expire_date.isoformat(),
                    'ip_address': data.get('ip_address', 'Unknown'),
                    'user_agent': data.get('user_agent', 'Unknown'),
                })
        
        return JsonResponse({
            'success': True,
            'sessions': user_sessions,
            'total': len(user_sessions)
        })
        
    except Exception as e:
        logger.error(f"Session management error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Could not retrieve sessions'
        }, status=500)


@login_required
@require_POST
def TerminateSessionView(request, session_key):
    """
    Terminate a specific session
    """
    try:
        if session_key == request.session.session_key:
            return JsonResponse({
                'success': False,
                'message': 'Cannot terminate current session'
            }, status=400)
        
        # Find and delete the session
        session = Session.objects.filter(session_key=session_key).first()
        
        if session:
            # Verify it belongs to the user
            data = session.get_decoded()
            if data.get('_auth_user_id') == str(request.user.pk):
                session.delete()
                logger.info(f"Session {session_key} terminated by user {request.user.user_uuid}")
                
                return JsonResponse({
                    'success': True,
                    'message': 'Session terminated successfully'
                })
        
        return JsonResponse({
            'success': False,
            'message': 'Session not found'
        }, status=404)
        
    except Exception as e:
        logger.error(f"Session termination error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Could not terminate session'
        }, status=500)


# ==================== JWT KEY MANAGEMENT ====================

@login_required
@require_POST
def RegenerateJWTKeysView(request):
    """
    Regenerate JWT signing keys (invalidates all existing tokens)
    """
    try:
        password = request.POST.get('password')
        
        if not request.user.check_password(password):
            return JsonResponse({
                'success': False,
                'message': 'Incorrect password'
            }, status=400)
        
        # Regenerate JWT keys
        request.user.regenerate_jwt_keys()
        
        logger.warning(f"JWT keys regenerated for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'JWT keys regenerated successfully. All existing tokens are now invalid.'
        })
        
    except Exception as e:
        logger.error(f"JWT regeneration error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Could not regenerate keys'
        }, status=500)