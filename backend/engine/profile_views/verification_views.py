"""
Email and Phone verification views
"""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta
import random
import hashlib
import logging

logger = logging.getLogger(__name__)


def generate_otp(length=6):
    """Generate random OTP"""
    return ''.join([str(random.randint(0, 9)) for _ in range(length)])


def hash_otp(otp):
    """Hash OTP for secure storage"""
    return hashlib.sha256(otp.encode()).hexdigest()


# ==================== EMAIL VERIFICATION ====================

@login_required
@require_POST
def SendEmailVerificationView(request):
    """
    Send verification code to user's email
    """
    try:
        email = request.POST.get('email') or request.user.email
        
        if not email:
            return JsonResponse({
                'success': False,
                'message': 'No email address found'
            }, status=400)
        
        # Generate OTP
        otp = generate_otp()
        
        # Store hashed OTP in user model
        request.user.email_verification_hash = hash_otp(otp)
        request.user.verification_expires_at = timezone.now() + timedelta(minutes=15)
        request.user.save(update_fields=['email_verification_hash', 'verification_expires_at', 'updated_at'])
        
        # Send email
        subject = 'PIELAM - Email Verification Code'
        message = f"""
Hello {request.user.email_or_phone},

Your verification code is: {otp}

This code will expire in 15 minutes.

If you did not request this code, please ignore this email.

Best regards,
PIELAM Security Team
        """
        
        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [email],
                fail_silently=False,
            )
            
            logger.info(f"Verification email sent to {email} for user {request.user.user_uuid}")
            
            return JsonResponse({
                'success': True,
                'message': f'Verification code sent to {email}'
            })
            
        except Exception as email_error:
            logger.error(f"Email sending failed: {str(email_error)}")
            
            # For development - return OTP in response
            if settings.DEBUG:
                return JsonResponse({
                    'success': True,
                    'message': f'Development mode: Your code is {otp}',
                    'dev_otp': otp  # Only in DEBUG mode
                })
            
            return JsonResponse({
                'success': False,
                'message': 'Failed to send email. Please try again.'
            }, status=500)
        
    except Exception as e:
        logger.error(f"Email verification error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred'
        }, status=500)


@login_required
@require_POST
def VerifyEmailView(request):
    """
    Verify email with OTP code
    """
    try:
        code = request.POST.get('code', '').strip()
        
        if not code:
            return JsonResponse({
                'success': False,
                'message': 'Verification code is required'
            }, status=400)
        
        # Check if verification is expired
        if not request.user.verification_expires_at or \
           request.user.verification_expires_at < timezone.now():
            return JsonResponse({
                'success': False,
                'message': 'Verification code expired. Please request a new one.'
            }, status=400)
        
        # Verify code
        if hash_otp(code) != request.user.email_verification_hash:
            return JsonResponse({
                'success': False,
                'message': 'Invalid verification code'
            }, status=400)
        
        # Mark email as verified
        request.user.is_email_verified = True
        request.user.email_verified_at = timezone.now()
        request.user.email_verification_hash = None
        request.user.verification_expires_at = None
        request.user.save(update_fields=[
            'is_email_verified',
            'email_verified_at',
            'email_verification_hash',
            'verification_expires_at',
            'updated_at'
        ])
        
        logger.info(f"Email verified for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'Email verified successfully!'
        })
        
    except Exception as e:
        logger.error(f"Email verification error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred'
        }, status=500)


# ==================== PHONE VERIFICATION ====================

@login_required
@require_POST
def SendPhoneVerificationView(request):
    """
    Send verification code to user's phone via SMS
    """
    try:
        phone = request.POST.get('phone') or request.user.phone
        
        if not phone:
            return JsonResponse({
                'success': False,
                'message': 'No phone number found'
            }, status=400)
        
        # Generate OTP
        otp = generate_otp()
        
        # Store hashed OTP in user model
        request.user.phone_otp_hash = hash_otp(otp)
        request.user.verification_expires_at = timezone.now() + timedelta(minutes=10)
        request.user.save(update_fields=['phone_otp_hash', 'verification_expires_at', 'updated_at'])
        
        # Send SMS (placeholder - integrate with Twilio/AWS SNS)
        try:
            # TODO: Integrate with actual SMS service
            # For now, log the OTP
            logger.info(f"SMS OTP for {phone}: {otp}")
            
            # Development mode - return OTP
            if settings.DEBUG:
                return JsonResponse({
                    'success': True,
                    'message': f'Development mode: Your code is {otp}',
                    'dev_otp': otp
                })
            
            return JsonResponse({
                'success': True,
                'message': f'Verification code sent to {phone}'
            })
            
        except Exception as sms_error:
            logger.error(f"SMS sending failed: {str(sms_error)}")
            return JsonResponse({
                'success': False,
                'message': 'Failed to send SMS. Please try again.'
            }, status=500)
        
    except Exception as e:
        logger.error(f"Phone verification error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred'
        }, status=500)


@login_required
@require_POST
def VerifyPhoneView(request):
    """
    Verify phone with OTP code
    """
    try:
        code = request.POST.get('code', '').strip()
        
        if not code:
            return JsonResponse({
                'success': False,
                'message': 'Verification code is required'
            }, status=400)
        
        # Check if verification is expired
        if not request.user.verification_expires_at or \
           request.user.verification_expires_at < timezone.now():
            return JsonResponse({
                'success': False,
                'message': 'Verification code expired. Please request a new one.'
            }, status=400)
        
        # Verify code
        if hash_otp(code) != request.user.phone_otp_hash:
            return JsonResponse({
                'success': False,
                'message': 'Invalid verification code'
            }, status=400)
        
        # Mark phone as verified
        request.user.is_phone_verified = True
        request.user.phone_verified_at = timezone.now()
        request.user.phone_otp_hash = None
        request.user.verification_expires_at = None
        request.user.save(update_fields=[
            'is_phone_verified',
            'phone_verified_at',
            'phone_otp_hash',
            'verification_expires_at',
            'updated_at'
        ])
        
        logger.info(f"Phone verified for user {request.user.user_uuid}")
        
        return JsonResponse({
            'success': True,
            'message': 'Phone verified successfully!'
        })
        
    except Exception as e:
        logger.error(f"Phone verification error: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': 'An error occurred'
        }, status=500)