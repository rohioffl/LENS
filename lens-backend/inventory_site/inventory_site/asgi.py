"""
ASGI config for inventory_site project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os
import sys

# Prevent Python from creating __pycache__ directories
sys.dont_write_bytecode = True

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'inventory_site.settings')

application = get_asgi_application()
