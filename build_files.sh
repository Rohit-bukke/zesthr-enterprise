#!/bin/bash
echo "Installing dependencies..."
python3 -m pip install -r requirements.txt --break-system-packages

echo "Collecting static files..."
python3 manage.py collectstatic --noinput --clear

echo "Preparing static directories for Vercel CDN..."
mkdir -p staticfiles/static
cp -r staticfiles/css staticfiles/static/ 2>/dev/null || true
cp -r staticfiles/js staticfiles/static/ 2>/dev/null || true
cp -r staticfiles/admin staticfiles/static/ 2>/dev/null || true
cp -r staticfiles/account staticfiles/static/ 2>/dev/null || true
cp -r staticfiles/rest_framework staticfiles/static/ 2>/dev/null || true
