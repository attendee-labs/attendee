#!/bin/bash
# Start PostgreSQL and Redis services (for WSL)
# Usage: ./scripts/start_services.sh

echo "Starting PostgreSQL..."
sudo service postgresql start

echo "Starting Redis..."
sudo service redis-server start

echo ""
echo "Services started!"
echo ""

# Check status
echo "PostgreSQL status:"
pg_isready -h localhost -p 5432 && echo "PostgreSQL is ready!" || echo "PostgreSQL is not ready"

echo ""
echo "Redis status:"
redis-cli ping && echo "Redis is ready!" || echo "Redis is not ready"
