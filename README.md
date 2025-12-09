# QELLM - Agentic Quality Engineering Platform

QELLM is a comprehensive, AI-powered Quality Engineering platform that automates end-to-end testing workflows using agentic architecture and RAG (Retrieval-Augmented Generation) capabilities.

## Architecture Overview

QELLM provides a complete end-to-end testing automation pipeline that combines:
- Web state capture and change detection
- AI-powered test generation using RAG
- Automated test execution with persistence
- Real-time monitoring and reporting

### Core Components

1. **Agent Architecture** - A distributed system of specialized agents that handle different aspects of the QA workflow
2. **Infrastructure Services** - PostgreSQL, MinIO, Qdrant, Redis, Kafka for data persistence and messaging
3. **Web Frontend** - Interactive dashboard for orchestrating the QA workflow

## Project Structure

```
.
├── agents/                     # Specialized AI agents
│   ├── change_detection_agent.py      # Detects UI changes using LLM analysis
│   ├── test_generation_agent_multi_llm.py  # Generates Playwright tests using RAG
│   ├── webpage_state_capture_agent.py     # Captures web page state for comparison
│   ├── test_execution_agent.py       # Executes generated tests in a sandboxed environment
│   ├── llm_fine_tuning_agent.py      # Manages RAG knowledge base ingestion
│   └── artifact_management_agent.py  # Manages artifact versioning in MinIO and PostgreSQL
├── frontend/                   # Web UI for orchestrating workflows
│   ├── app.jsx                # Main React application
│   └── index.html             # HTML entry point
├── test_execution_agent/      # Test execution environment with Docker support
│   ├── Dockerfile             # Custom Dockerfile for test execution
│   ├── test_execution_agent.py # Execution agent logic
│   └── requirements.txt       # Dependencies for execution agent
├── service/                   # Background services
│   └── reporing_service.py    # Kafka consumer for test result reporting
├── utils/                     # Utility scripts and configurations
│   └── db_setup/              # Database schema initialization
│       └── artifact_metadata.sql  # PostgreSQL table definitions
├── docker-compose.yml         # Multi-service orchestration
├── Dockerfile.agent           # Base Dockerfile for agent services  
├── Dockerfile.base            # Multi-stage build for agents
├── Dockerfile.frontend        # Frontend Docker configuration
├── Dockerfile.reporting       # Reporting service Dockerfile
├── Dockerfile.worker          # Kafka consumer worker Dockerfile
├── requirements.in            # Main project dependencies
├── requirements.txt           # Generated dependency list
├── test/                      # Test suite for the platform
│   ├── Dockerfile             # Test environment configuration  
│   ├── requirements.txt       # Test dependencies
│   └── run_tests.sh           # Test execution script
├── .env                       # Environment configuration file
└── README.md                  # This documentation file
```

## Services Architecture

### Infrastructure Services (via docker-compose.yml)

1. **PostgreSQL** - Primary database for test run metadata and artifact tracking
2. **MinIO** - Object storage for test artifacts and snapshots  
3. **Qdrant** - Vector database for RAG knowledge base storage
4. **Redis** - Caching and session management
5. **Kafka** - Message broker for asynchronous communication between agents
6. **Zookeeper** - Kafka coordination service

### Agent Services

1. **Artifact Management Agent** (`8007`) - Manages artifact versioning in PostgreSQL and MinIO
2. **Change Detection Agent** (`8000`) - Analyzes web page changes using LLMs
3. **Test Generation Agent** (`8001`) - Generates Playwright Python tests using RAG
4. **Webpage State Capture Agent** (`8002`) - Captures and compares web page states
5. **Test Execution Agent** (`8003`) - Executes generated tests in a sandboxed environment
6. **LLM Fine Tuning Agent** (`8004`) - Manages RAG knowledge base ingestion and versioning
7. **Test Reporting Service** (`8005`) - Consumes Kafka events and updates reporting database

### Frontend Service

- **QELLM Frontend** (`3000`) - Interactive dashboard for orchestrating QA workflows

## Key Features

### 1. RAG-Based Test Generation
- Integrates with Qdrant vector database for semantic search of product knowledge
- Uses LLMs (Gemini or self-hosted) to generate Playwright tests based on product specifications
- Maintains knowledge base versioning for reproducible test generation

### 2. Change Detection & Analysis
- Captures web page states before and after changes
- Uses LLMs to analyze differences and generate targeted test plans
- Supports both manual and automated change detection workflows

### 3. End-to-End Test Automation
- Generates executable Playwright Python test code
- Executes tests in isolated environments
- Provides detailed execution results and logs

### 4. Artifact Management & Versioning
- Manages versioned artifacts in MinIO object storage
- Tracks artifact versions in PostgreSQL database
- Supports persistence of test code and execution results

### 5. Real-time Monitoring & Reporting
- Web dashboard for monitoring agent health and workflow status
- Kafka-based event system for real-time updates
- Comprehensive test result reporting

## Getting Started

### Prerequisites

- Docker and Docker Compose installed
- At least 4GB of available RAM (recommended 8GB+)
- Node.js and npm for frontend development (optional)

### Quick Start

1. **Clone the repository:**
```bash
git clone <repository-url>
cd qellm
```

2. **Start all services:**
```bash
docker-compose up --build
```

3. **Access the dashboard:**
Open your browser to `http://localhost:3000`

### Development

For development, you can:
- Modify individual agents in `agents/` directory
- Update the frontend in `frontend/app.jsx`
- Customize database schema in `utils/db_setup/`

### Configuration

Environment variables are managed through:
- `.env` file for local development
- Docker Compose configuration for service-specific settings

## API Endpoints

### Agent Endpoints

1. **LLM Fine Tuning Agent** (`/ingest_knowledge`):
   - POST: Ingests product specifications into Qdrant knowledge base

2. **Webpage State Capture Agent** (`/capture`):
   - POST: Captures web page state for change detection

3. **Change Detection Agent** (`/detect_changes`):
   - POST: Analyzes differences between captured states

4. **Test Generation Agent** (`/generate_test_plan`):
   - POST: Generates Playwright test plans using RAG

5. **Test Execution Agent** (`/run_tests`):
   - POST: Executes generated tests from MinIO

### Health Endpoints

- `GET /agent_card` - Health check for each agent
- `GET /health` - General service health status

## Architecture Details

### Data Flow

1. **Knowledge Ingestion**: Product specifications are ingested into Qdrant
2. **State Capture**: Web page states are captured before and after changes  
3. **Change Detection**: LLM analyzes differences between states
4. **Test Generation**: Playwright Python tests are generated using RAG
5. **Test Execution**: Generated tests execute in isolated environment
6. **Reporting**: Results are persisted and reported through Kafka

### Persistence Strategy

- **PostgreSQL**: Stores test run metadata, status tracking, and reporting data
- **MinIO**: Stores actual test artifacts (code files) and snapshots
- **Qdrant**: Stores RAG knowledge base for semantic search
- **Kafka**: Provides async messaging between agents

### Security Considerations

- All services run in isolated Docker containers
- Test code execution happens in sandboxed environments  
- Secure communication with environment-specific credentials
- CORS policy applied to web frontend for development

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Make your changes
4. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
5. Push to the branch (`git push origin feature/AmazingFeature`)
6. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support, please create an issue in the GitHub repository or contact the development team.
