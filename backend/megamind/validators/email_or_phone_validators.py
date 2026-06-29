"""
Validators for User model
- Email validation
- Phone validation (E.164 format)
- Identity parsing utilities
"""
import re
from dataclasses import dataclass
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

# E.164 phone number regex: +[country code][number]
# Examples: +12025551234, +442071234567, +8613800138000
PHONE_REGEX = re.compile(r'^\+[1-9]\d{1,14}$')

# Email regex (basic validation)
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


# ---------------------------------------------------------------------
# Result class for identity parsing
# ---------------------------------------------------------------------

@dataclass
class IdentityResult:
    """Result of parsing an email_or_phone value"""
    identity_type: str  # "email" or "phone"
    normalized: str     # Normalized value
    original: str       # Original input value


# ---------------------------------------------------------------------
# Core validation functions
# ---------------------------------------------------------------------

def is_valid_email(value):
    """
    Check if value is a valid email address.
    
    Args:
        value: String to validate
        
    Returns:
        bool: True if valid email
    """
    if not value:
        return False
    
    # Use Django's EmailValidator for robust validation
    validator = EmailValidator()
    try:
        validator(value)
        return True
    except ValidationError:
        return False


def is_valid_phone(value):
    """
    Check if value is a valid E.164 phone number.
    
    E.164 format: +[country code][subscriber number]
    - Starts with +
    - Country code: 1-3 digits
    - Total length: 8-15 digits (including country code)
    
    Examples:
        Valid: +12025551234, +442071234567, +8613800138000
        Invalid: 2025551234, +1-202-555-1234, +1 202 555 1234
    
    Args:
        value: String to validate
        
    Returns:
        bool: True if valid E.164 phone
    """
    if not value:
        return False
    
    # Remove any whitespace
    value = value.strip()
    
    # Check E.164 format
    return bool(PHONE_REGEX.match(value))


def normalize_email(email):
    """
    Normalize email address.
    
    - Convert to lowercase
    - Strip whitespace
    
    Args:
        email: Email address to normalize
        
    Returns:
        str: Normalized email
    """
    return email.strip().lower()


def normalize_phone(phone):
    """
    Normalize phone number to E.164 format.
    
    - Remove all whitespace, dashes, parentheses
    - Ensure starts with +
    
    Args:
        phone: Phone number to normalize
        
    Returns:
        str: Normalized phone in E.164 format
    """
    # Remove common formatting characters
    phone = re.sub(r'[\s\-\(\)\.]+', '', phone)
    
    # Ensure it starts with +
    if not phone.startswith('+'):
        raise ValidationError(
            "Phone number must start with + and include country code (E.164 format). "
            "Example: +12025551234"
        )
    
    return phone


# ---------------------------------------------------------------------
# Identity parsing
# ---------------------------------------------------------------------

def parse_identity(value):
    """
    Parse and identify whether value is email or phone.
    
    Args:
        value: Email or phone string
        
    Returns:
        IdentityResult: Parsed identity information
        
    Raises:
        ValidationError: If value is neither valid email nor phone
    """
    if not value:
        raise ValidationError("Email or phone is required.")
    
    original = value
    value = value.strip()
    
    # Try to identify as email first
    if '@' in value:
        if is_valid_email(value):
            return IdentityResult(
                identity_type="email",
                normalized=normalize_email(value),
                original=original
            )
        else:
            raise ValidationError(
                f"'{value}' is not a valid email address."
            )
    
    # Try to identify as phone
    elif value.startswith('+') or value.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').isdigit():
        try:
            normalized = normalize_phone(value)
            if is_valid_phone(normalized):
                return IdentityResult(
                    identity_type="phone",
                    normalized=normalized,
                    original=original
                )
            else:
                raise ValidationError(
                    f"'{value}' is not a valid phone number. "
                    "Use E.164 format: +[country code][number]. Example: +12025551234"
                )
        except ValidationError:
            raise
    
    # Neither email nor phone
    else:
        raise ValidationError(
            f"'{value}' is neither a valid email address nor a valid phone number (E.164 format)."
        )


# ---------------------------------------------------------------------
# Django validator for model field
# ---------------------------------------------------------------------

def email_or_phone_validator(value):
    """
    Django validator for email_or_phone field.
    
    Validates that value is either:
    - A valid email address, OR
    - A valid E.164 phone number
    
    Args:
        value: Value to validate
        
    Raises:
        ValidationError: If value is invalid
        
    Examples:
        Valid:
            - user@example.com
            - +12025551234
            - +442071234567
            
        Invalid:
            - not-an-email
            - 2025551234 (missing +)
            - +1-202-555-1234 (has formatting)
    """
    try:
        parse_identity(value)
    except ValidationError as e:
        raise ValidationError(str(e))


# ---------------------------------------------------------------------
# Additional utility functions
# ---------------------------------------------------------------------

def format_phone_for_display(phone):
    """
    Format E.164 phone for human-readable display.
    
    Examples:
        +12025551234 -> +1 (202) 555-1234
        +442071234567 -> +44 20 7123 4567
    
    Args:
        phone: E.164 phone number
        
    Returns:
        str: Formatted phone number
    """
    if not phone or not phone.startswith('+'):
        return phone
    
    # US/Canada formatting (+1)
    if phone.startswith('+1') and len(phone) == 12:
        return f"+1 ({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
    
    # UK formatting (+44)
    elif phone.startswith('+44') and len(phone) >= 12:
        return f"+44 {phone[3:5]} {phone[5:9]} {phone[9:]}"
    
    # Generic international formatting
    else:
        # +[country] [rest]
        if len(phone) > 4:
            return f"{phone[:3]} {phone[3:]}"
        return phone


def get_country_code(phone):
    """
    Extract country code from E.164 phone number.
    
    Args:
        phone: E.164 phone number
        
    Returns:
        str: Country code (without +), or None if invalid
        
    Examples:
        +12025551234 -> "1"
        +442071234567 -> "44"
        +8613800138000 -> "86"
    """
    if not phone or not phone.startswith('+'):
        return None
    
    # Try common country code lengths (1-3 digits)
    for length in [1, 2, 3]:
        potential_code = phone[1:1+length]
        if potential_code.isdigit():
            # Validate that remaining part is also digits
            remaining = phone[1+length:]
            if remaining and remaining.isdigit():
                return potential_code
    
    return None


# ---------------------------------------------------------------------
# Validation messages
# ---------------------------------------------------------------------

VALIDATION_MESSAGES = {
    'invalid_email': 'Enter a valid email address.',
    'invalid_phone': 'Enter a valid phone number in E.164 format (e.g., +12025551234).',
    'invalid_format': 'Enter either a valid email address or phone number in E.164 format.',
    'phone_no_country_code': 'Phone number must include country code with + prefix.',
    'phone_too_short': 'Phone number is too short.',
    'phone_too_long': 'Phone number is too long (max 15 digits).',
}


def get_validation_message(error_type):
    """Get a validation error message by type"""
    return VALIDATION_MESSAGES.get(error_type, VALIDATION_MESSAGES['invalid_format'])