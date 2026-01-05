"""
WSGI config for inventory_site project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os
import sys

# Prevent Python from creating __pycache__ directories
sys.dont_write_bytecode = True

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'inventory_site.settings')

application = get_wsgi_application()
