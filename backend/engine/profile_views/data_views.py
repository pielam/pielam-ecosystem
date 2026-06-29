"""
Data export and account management views
"""
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
import json
import csv
import logging

logger = logging.getLogger(__name__)


# ==================== DATA EXPORT ====================

@login_required
def ExportUserDataView(request):
    """
    Export user data as JSON or CSV
    """
    try:
        export_format = request.GET.get('format', 'json')
        user = request.user
        
        # Collect user data
        user_data = {
            'account_info': {
                'uuid': str(user.user_uuid),
                'email_or_phone': user.email_or_phone,
                'identity_type': user.identity_type,
                'email': user.email,
                'phone': user.phone,
                'role': user.role,
            },
            'verification_status': {
                'is_email_verified': user.is_email_verified,
                'email_verified_at': user.email_verified_at.isoformat() if user.email_verified_at else None,
                'is_phone_verified': user.is_phone_verified,
                'phone_verified_at': user.phone_verified_at.isoformat() if user.phone_verified_at else None,
            },
            'account_status': {
                'is_active': user.is_active,
                'is_deleted': user.is_deleted,
                'is_staff': user.is_staff,
                'is_superuser': user.is_superuser,
            },
            'security_info': {
                'failed_login_attempts': user.failed_login_attempts,
                'is_locked': user.is_account_locked,
                'locked_until': user.locked_until.isoformat() if user.locked_until else None,
                'lock_reason': user.lock_reason,
            },
            'timestamps': {
                'created_at': user.created_at.isoformat(),
                'updated_at': user.updated_at.isoformat(),
                'last_login_at': user.last_login_at.isoformat() if user.last_login_at else None,
                'last_activity_at': user.last_activity_at.isoformat() if user.last_activity_at else None,
                'last_password_change': user.last_password_change.isoformat() if user.last_password_change else None,
            },
            'preferences': {
                'timezone': user.user_timezone,
            },
        }
        
        if export_format == 'json':
            response = HttpResponse(
                json.dumps(user_data, indent=2),
                content_type='application/json'
            )
            response['Content-Disposition'] = f'attachment; filename="pielam_user_data_{user.user_uuid}.json"'
            
        elif export_format == 'csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="pielam_user_data_{user.user_uuid}.csv"'
            
            writer = csv.writer(response)
            writer.writerow(['Category', 'Field', 'Value'])
            
            for category, fields in user_data.items():
                for field, value in fields.items():
                    writer.writerow([category, field, value])
        else:
            return JsonResponse({
                'success': False,
                'message': 'Invalid export format. Use "json" or "csv".'
            }, status=400)
        
        logger.info(f"Data exported for user {user.user_uuid} in {export_format} format")
        return response
        
    except Exception as e:
        logger.error(f"Data export error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Export failed'
        }, status=500)


# ==================== ACCOUNT DEACTIVATION ====================

@login_required
@require_POST
def DeactivateAccountView(request):
    """
    Deactivate user account (can be reactivated)
    """
    try:
        password = request.POST.get('password')
        reason = request.POST.get('reason', 'User requested deactivation')
        
        # Verify password
        if not request.user.check_password(password):
            return JsonResponse({
                'success': False,
                'message': 'Incorrect password'
            }, status=400)
        
        # Deactivate account
        request.user.is_active = False
        request.user.save(update_fields=['is_active', 'updated_at'])
        
        logger.warning(f"Account deactivated for user {request.user.user_uuid}. Reason: {reason}")
        
        # Logout user
        logout(request)
        
        return JsonResponse({
            'success': True,
            'message': 'Account deactivated successfully',
            'redirect_url': '/auth/signin/'
        })
        
    except Exception as e:
        logger.error(f"Account deactivation error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Deactivation failed'
        }, status=500)


# ==================== ACCOUNT DELETION ====================

@login_required
@require_POST
def DeleteAccountView(request):
    """
    Soft delete user account (can be restored within 30 days)
    """
    try:
        password = request.POST.get('password')
        confirmation = request.POST.get('confirmation')
        reason = request.POST.get('reason', 'User requested deletion')
        
        # Verify password
        if not request.user.check_password(password):
            return JsonResponse({
                'success': False,
                'message': 'Incorrect password'
            }, status=400)
        
        # Verify confirmation text
        if confirmation != 'DELETE':
            return JsonResponse({
                'success': False,
                'message': 'Please type DELETE to confirm'
            }, status=400)
        
        # Soft delete account
        request.user.soft_delete(reason=reason)
        
        logger.warning(f"Account deleted (soft) for user {request.user.user_uuid}. Reason: {reason}")
        
        # Logout user
        logout(request)
        
        return JsonResponse({
            'success': True,
            'message': 'Account deleted successfully. You can restore it within 30 days by contacting support.',
            'redirect_url': '/auth/signin/'
        })
        
    except Exception as e:
        logger.error(f"Account deletion error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'Deletion failed'
        }, status=500)