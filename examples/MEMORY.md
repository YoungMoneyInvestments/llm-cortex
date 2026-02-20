# Project Memory

## Architecture Decisions
- Using PostgreSQL for primary data store
- REST API built with FastAPI
- Frontend in React + TypeScript

## Key Patterns
- All API endpoints require authentication via JWT
- Database migrations managed with Alembic
- Tests use pytest with fixtures in conftest.py

## Active Work
- Auth module: JWT refresh token rotation (in progress)
- API: Rate limiting middleware (planned)

## Team
- Alice: Backend lead, owns auth module
- Bob: Frontend, React components
- Carol: DevOps, CI/CD pipeline

## Lessons Learned
- Always run migrations before deploying
- The staging DB uses a different schema version — check before testing
- Redis cache TTL should be 300s for user sessions, 60s for API responses
