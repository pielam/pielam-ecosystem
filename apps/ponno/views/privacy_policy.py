# apps/ponno/views/privacy_policy.py

from django.shortcuts import render


def PrivacyPolicyView(request):
    """
    Static "Privacy Policy" page.
    Simple informational view — no DB queries required.
    """

    context = {
        'meta_description': 'Read the PIELAM Privacy Policy to learn how we collect, use, and protect your data.',
        'meta_keywords':    'PIELAM, privacy policy, data protection, terms',
        'og_title':         'Privacy Policy — PIELAM',
        'og_description':   'Read the PIELAM Privacy Policy to learn how we collect, use, and protect your data.',
        'og_url':           request.build_absolute_uri(),
        'last_updated':     'July 1, 2026',
    }

    return render(request, 'ponno/privacy_policy.html', context)