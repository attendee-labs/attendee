# Contributing to Attendee

Thank you for your interest in contributing to Attendee! This document provides guidelines and instructions for contributing to the project.

## Getting Started

1. Fork the repository and clone it locally
2. Set up the development environment:

   ```bash
   # Build the Docker image (takes ~5 minutes)
   docker compose -f dev.docker-compose.yaml build

   # Create local environment variables
   docker compose -f dev.docker-compose.yaml run --rm attendee-app-local python init_env.py > .env

   # Edit .env and configure storage:
   # Option A: Add your AWS credentials
   # Option B: Use MinIO (local S3) - add these lines to .env:
   #   AWS_RECORDING_STORAGE_BUCKET_NAME=attendee-recordings
   #   AWS_ACCESS_KEY_ID=minioadmin
   #   AWS_SECRET_ACCESS_KEY=minioadmin
   #   AWS_DEFAULT_REGION=us-east-1
   #   AWS_ENDPOINT_URL=http://minio:9000

   # Start all services
   # With AWS S3:
   docker compose -f dev.docker-compose.yaml up
   # With MinIO (no AWS needed):
   docker compose -f dev.docker-compose.yaml -f dev.docker-compose.local.yaml up

   # In a separate terminal, run migrations
   docker compose -f dev.docker-compose.yaml exec attendee-app-local python manage.py migrate
   ```

3. Create a new branch for your changes:
   ```bash
   git switch -c feature/your-feature-name
   ```

## Development Guidelines

### Code Style

We use Ruff for both linting and formatting. The configuration can be found in `pyproject.toml`. To ensure your code meets our style guidelines:

1. Install pre-commit hooks:

   ```bash
   pip install pre-commit
   pre-commit install
   ```

2. The pre-commit hooks will automatically:
   - Run the Ruff linter with auto-fixing enabled
   - Run the Ruff formatter
   - Check for common issues

### Documentation

Contributing to documentation means modifying the files in the `docs` directory.

- Update the API documentation in `docs/openapi.yml` for any API changes
- Update the README.md if necessary
- For other types documentation, see the related \*.md file in the `docs` directory

## Pull Request Process

1. Create a Pull Request with a clear title and description
2. Update the documentation as needed
3. Reference any related issues in your PR description
4. Wait for review from maintainers

## Reporting Issues

When reporting issues, please include:

- A clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs or screenshots

## Community

- Join our [Slack Community](https://join.slack.com/t/attendee-community/shared_invite/zt-3l43ns8cl-G8YnMccWVTugMlloUtSf9g) for discussions
- Star the repository if you find it useful

## License

By contributing to Attendee, you agree that your contributions will be licensed under the same license as the project.
