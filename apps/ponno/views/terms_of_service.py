# apps/ponno/views/terms_of_service.py

from django.shortcuts import render


def TermsOfServiceView(request):
    """
    Static "Terms of Service" page.
    Simple informational view — no DB queries required.
    """

    context = {
        'meta_description': 'Read the PIELAM Terms of Service governing use of the platform.',
        'meta_keywords':    'PIELAM, terms of service, terms and conditions, legal',
        'og_title':         'Terms of Service — PIELAM',
        'og_description':   'Read the PIELAM Terms of Service governing use of the platform.',
        'og_url':           request.build_absolute_uri(),
        'last_updated':     'July 1, 2026',
    }

    return render(request, 'ponno/terms_of_service.html', context)