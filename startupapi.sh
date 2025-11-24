python manage.py makemigrations
python manage.py migrate --noinput
gunicorn attendee.wsgi:application --bind 0.0.0.0:8000 --workers 3 --threads 4 --timeout 60