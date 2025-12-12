#!/bin/bash
# Set up PostgreSQL database for local development
# Usage: ./scripts/setup_db.sh

set -e

DB_NAME="${POSTGRES_DB:-attendee_development}"
DB_USER="${POSTGRES_USER:-attendee_development_user}"
DB_PASSWORD="${POSTGRES_PASSWORD:-attendee_development_user}"

echo "==================================="
echo "PostgreSQL Database Setup"
echo "==================================="
echo ""
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo ""

# Start PostgreSQL if not running
if ! pg_isready -h localhost -p 5432 > /dev/null 2>&1; then
    echo "Starting PostgreSQL..."
    sudo service postgresql start
    sleep 2
fi

echo "Creating database user and database..."

# Create user (if not exists)
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || \
    echo "User $DB_USER already exists, skipping..."

# Create database (if not exists)
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || \
    echo "Database $DB_NAME already exists, skipping..."

# Grant privileges
sudo -u postgres psql -c "ALTER USER $DB_USER CREATEDB;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

echo ""
echo "Database setup complete!"
echo ""
echo "Next steps:"
echo "  1. Run migrations: python manage.py migrate"
echo "  2. Create superuser: python manage.py createsuperuser"
echo ""
