# Contributing to Attendee

Thank you for your interest in contributing to Attendee! This document provides guidelines and instructions for contributing to the project.

## Getting Started

1. Fork the repository and clone it locally
2. Set up the development environment:
   ```bash
   # Build the Docker image (takes ~5 minutes)
   docker compose -f dev.docker-compose.yml build
   
   # Create local environment variables
   docker compose -f dev.docker-compose.yml run --rm attendee-app-local python init_env.py > .env
   
   # Edit .env and add your AWS credentials
   
   # Start all services
   docker compose -f dev.docker-compose.yml up
   
   # In a separate terminal, run migrations
   docker compose -f dev.docker-compose.yml exec attendee-app-local python manage.py migrate
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
- For other types documentation, see the related *.md file in the `docs` directory

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

- Join our [Slack Community](https://join.slack.com/t/attendeecommu-rff8300/shared_invite/zt-2uhpam6p2-ZzLAoVrljbL2UEjqdSHrgQ) for discussions
- Star the repository if you find it useful

## License

By contributing to Attendee, you agree that your contributions will be licensed under the same license as the project.