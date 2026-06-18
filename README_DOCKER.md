# Docker Setup for Lexxadata Backend

This directory contains Docker configuration files to containerize the Lexxadata Customer Scraper API backend.

## Files Created

- `Dockerfile` - Multi-stage build configuration for the FastAPI backend
- `docker-compose.yml` - Complete orchestration with PostgreSQL and Redis
- `.dockerignore` - Optimization file to exclude unnecessary files from build context
- `init.sql` - PostgreSQL initialization script (placeholder, modify as needed)
- `README_DOCKER.md` - This file with usage instructions

## Prerequisites

- Docker and Docker Compose installed on your system
- Sufficient disk space for the containers and images

## Quick Start

1. **Build and start all services:**
   ```bash
   docker-compose up --build -d
   ```

2. **View logs:**
   ```bash
   docker-compose logs -f backend
   ```

3. **Stop services:**
   ```bash
   docker-compose down
   ```

## Services

### Backend API
- **Container Name:** bakcend-fastapi
- **Port:** 8001 (host) → 8001 (container)
- **Health Check:** http://localhost:8001/
- **Environment Variables:** All variables from `.env` file are passed through docker-compose

### PostgreSQL Database
- **Container Name:** lexxadata-postgres
- **Port:** 5435 (host) → 5432 (container)
- **Database:** data
- **User:** root
- **Password:** Noclex1965
- **Volume:** postgres_data (persistent storage)

### Redis Cache
- **Container Name:** lexxadata-redis
- **Port:** 6379 (host) → 6379 (container)
- **Volume:** redis_data (persistent storage)

## Development Workflow

### First Time Setup
1. Ensure your `.env` file is properly configured
2. Run `docker-compose up --build -d`
3. Check that all services are healthy: `docker-compose ps`

### Making Changes
1. After code changes, rebuild and restart:
   ```bash
   docker-compose up --build -d backend
   ```

### Database Management
- Connect to PostgreSQL:
  ```bash
  docker-compose exec postgres psql -U root -d data
  ```

### Viewing Logs
- Backend logs: `docker-compose logs -f backend`
- Database logs: `docker-compose logs -f postgres`
- Redis logs: `docker-compose logs -f redis`

## Production Considerations

1. **Security:**
   - Change default passwords in production
   - Use Docker secrets for sensitive data
   - Limit network access with proper firewall rules

2. **Performance:**
   - Adjust resource limits in docker-compose.yml
   - Use external managed database services for production
   - Implement proper backup strategies

3. **Monitoring:**
   - Add health checks and monitoring
   - Implement log aggregation
   - Set up alerting for critical failures

## Troubleshooting

### Common Issues

1. **Port Conflicts:**
   - Ensure ports 8001, 5435, and 6379 are available
   - Modify ports in docker-compose.yml if needed

2. **Permission Issues:**
   - Check file permissions for mounted volumes
   - Ensure Docker has proper access rights

3. **Build Failures:**
   - Check requirements.txt for incompatible versions
   - Verify system dependencies in Dockerfile

### Reset Everything
```bash
docker-compose down -v
docker system prune -f
docker-compose up --build -d
```

## Environment Variables

All environment variables from the `.env` file are passed to the backend container. The database connection variables are automatically updated to use the Docker network:

- `DB_HOST` is set to `postgres` (container name)
- `DB_PORT` is set to `5432` (internal PostgreSQL port)

## Volumes

- `postgres_data`: Persistent PostgreSQL data
- `redis_data`: Persistent Redis data
- Session files (`.pkl`) are mounted as bind mounts for persistence

## Networks

All services communicate through the `lexxadata-network` bridge network for secure inter-container communication.

### Port Mapping Summary
- Backend API: 8001 (host) → 8001 (container)
- PostgreSQL: 5435 (host) → 5432 (container)
- Redis: 6379 (host) → 6379 (container)