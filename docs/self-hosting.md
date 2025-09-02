# Self-Hosting Attendee

Attendee is designed for convenient self-hosting and can reduce costs by 10x compared to closed-source alternatives. This guide will walk you through deploying Attendee on your own infrastructure.

## Overview

Attendee runs as a Django application in a single Docker image with minimal external dependencies:

- **PostgreSQL** - Database for storing bot data, users, and configurations
- **Redis** - Message broker for Celery tasks and caching
- **AWS S3** - Storage for recordings and media files (optional, can use local storage)

## Prerequisites

Before you begin, ensure you have:

- **Docker and Docker Compose** installed on your system
- **Git** for cloning the repository
- **AWS Account** (optional, for S3 storage)
- **Zoom OAuth Credentials** (if using Zoom meetings)
- **Deepgram API Key** (for transcription services)

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/attendee-labs/attendee.git
cd attendee
```

### 2. Set Up Environment Variables

Generate the initial environment configuration:

**Linux/Mac:**
```bash
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env
```

**Windows:**
```powershell
docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py | Out-File -Encoding utf8 .env
```

### 3. Configure Your Environment

Edit the `.env` file and update the following key variables:

```bash
# Database Configuration
POSTGRES_HOST=postgres
POSTGRES_DB=attendee_production
POSTGRES_USER=attendee_user
POSTGRES_PASSWORD=your_secure_password

# Redis Configuration
REDIS_URL=redis://redis:6379/5

# AWS Configuration (for S3 storage)
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_REGION=us-east-1
AWS_S3_BUCKET=your-attendee-bucket

# Django Settings
SECRET_KEY=your_django_secret_key
DEBUG=False
ALLOWED_HOSTS=your-domain.com,localhost

# Email Configuration (for user registration)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
EMAIL_USE_TLS=True
```

### 4. Build and Start Services

Build the Docker image (takes about 5 minutes):
```bash
docker compose -f dev.docker-compose.yaml build
```

Start all services:
```bash
docker compose -f dev.docker-compose.yaml up -d
```

### 5. Run Database Migrations

In a separate terminal:
```bash
docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
```

### 6. Create Your First Account

1. Navigate to `http://localhost:8000` in your browser
2. Create an account
3. Check the server logs for the confirmation link:
   ```bash
   docker compose -f dev.docker-compose.yaml logs attendee-app-local
   ```
4. Look for a line like: `http://localhost:8000/accounts/confirm-email/<key>/`
5. Paste the link into your browser to confirm your account

### 7. Configure Platform Credentials

1. Sign in to your Attendee instance
2. Navigate to the "Settings" section
3. Add your Zoom OAuth credentials and Deepgram API key
4. Create an API key in the "API Keys" section

## Production Deployment

### Using Docker Compose (Recommended)

For production, create a `docker-compose.yaml` file:

```yaml
version: '3.8'

services:
  attendee-app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - POSTGRES_HOST=postgres
      - REDIS_URL=redis://redis:6379/5
      - DJANGO_SETTINGS_MODULE=attendee.settings.production
    depends_on:
      - postgres
      - redis
    restart: unless-stopped

  attendee-worker:
    build: .
    environment:
      - POSTGRES_HOST=postgres
      - REDIS_URL=redis://redis:6379/5
      - DJANGO_SETTINGS_MODULE=attendee.settings.production
    depends_on:
      - postgres
      - redis
    restart: unless-stopped
    command: ["/bin/bash", "-c", "/opt/bin/entrypoint.sh && celery -A attendee worker -l INFO"]

  attendee-scheduler:
    build: .
    environment:
      - POSTGRES_HOST=postgres
      - REDIS_URL=redis://redis:6379/5
      - DJANGO_SETTINGS_MODULE=attendee.settings.production
    depends_on:
      - postgres
      - redis
    restart: unless-stopped
    command: ["/bin/bash", "-c", "/opt/bin/entrypoint.sh && python manage.py run_scheduler"]

  postgres:
    image: postgres:15.3-alpine
    environment:
      POSTGRES_DB: attendee_production
      POSTGRES_USER: attendee_user
      POSTGRES_PASSWORD: your_secure_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    restart: unless-stopped

volumes:
  postgres_data:
  redis_data:
```

### Using Kubernetes

For Kubernetes deployment, you can use the provided Helm charts or create your own manifests. Key considerations:

- **Resource Requirements**: Bots require significant CPU and memory
- **Persistent Storage**: For PostgreSQL and Redis
- **Load Balancing**: For high availability
- **Monitoring**: Prometheus metrics and logging

### Using Cloud Platforms

#### AWS ECS/Fargate
- Use the provided Docker image
- Configure RDS for PostgreSQL
- Use ElastiCache for Redis
- Set up Application Load Balancer

#### Google Cloud Run
- Deploy the containerized application
- Use Cloud SQL for PostgreSQL
- Use Memorystore for Redis

#### Azure Container Instances
- Deploy using the Docker image
- Use Azure Database for PostgreSQL
- Use Azure Cache for Redis

## Configuration Options

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `POSTGRES_HOST` | PostgreSQL host | Yes | - |
| `POSTGRES_DB` | Database name | Yes | `attendee` |
| `POSTGRES_USER` | Database user | Yes | - |
| `POSTGRES_PASSWORD` | Database password | Yes | - |
| `REDIS_URL` | Redis connection URL | Yes | - |
| `AWS_ACCESS_KEY_ID` | AWS access key | No | - |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | No | - |
| `AWS_REGION` | AWS region | No | `us-east-1` |
| `AWS_S3_BUCKET` | S3 bucket name | No | - |
| `SECRET_KEY` | Django secret key | Yes | - |
| `DEBUG` | Debug mode | No | `False` |
| `ALLOWED_HOSTS` | Allowed hosts | Yes | - |

### Bot Configuration

Configure bot behavior through the web interface or API:

- **Recording settings**: Audio/video quality, storage location
- **Transcription settings**: Language, provider selection
- **Automatic leave settings**: Timeout configurations
- **Webhook endpoints**: For real-time notifications

## Monitoring and Maintenance

### Health Checks

Monitor the following endpoints:
- `/health/` - Application health
- `/api/v1/bots/` - API availability
- Database connectivity
- Redis connectivity

### Logs

View logs for different services:
```bash
# Application logs
docker compose logs attendee-app

# Worker logs
docker compose logs attendee-worker

# Scheduler logs
docker compose logs attendee-scheduler
```

### Backup Strategy

1. **Database Backups**: Regular PostgreSQL dumps
2. **File Storage**: S3 bucket versioning or local backups
3. **Configuration**: Version control for environment files

### Scaling Considerations

- **Horizontal Scaling**: Run multiple worker instances
- **Vertical Scaling**: Increase CPU/memory for bot instances
- **Database Scaling**: Consider read replicas for high traffic
- **Caching**: Redis for session and API response caching

## Troubleshooting

### Common Issues

1. **Bots not joining meetings**
   - Check Zoom OAuth credentials
   - Verify network connectivity
   - Review bot logs

2. **Transcription failures**
   - Verify Deepgram API key
   - Check audio quality settings
   - Review transcription provider logs

3. **High resource usage**
   - Monitor bot resource consumption
   - Adjust bot limits and timeouts
   - Consider resource quotas

### Getting Help

- **Documentation**: Check other guides in this documentation
- **Community**: Join our [Slack community](https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ)
- **Issues**: Report bugs on [GitHub](https://github.com/attendee-labs/attendee)
- **Support**: Schedule a call at [calendly.com/noah-attendee/30min](https://calendly.com/noah-attendee/30min)

## Security Considerations

### Network Security
- Use HTTPS in production
- Configure firewall rules
- Implement rate limiting
- Use VPN for internal deployments

### Data Security
- Encrypt database connections
- Use secure API keys
- Implement proper access controls
- Regular security updates

### Compliance
- GDPR compliance for data handling
- SOC 2 considerations for enterprise deployments
- HIPAA compliance for healthcare use cases

## Cost Optimization

### Resource Optimization
- Right-size bot instances
- Use spot instances where possible
- Implement auto-scaling
- Monitor and optimize storage usage

### Alternative Storage
- Use local storage instead of S3 for small deployments
- Implement data retention policies
- Compress recordings and transcripts
- Use CDN for static assets
