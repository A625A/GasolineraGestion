# Fuel Station Operations & Analytics Prototype

A prototype for fuel-station operations combining inventory monitoring, consumption analytics, depletion estimates, and restocking recommendations.

The project was developed around a **Shell fuel-station operations use case** to explore how operational data could be transformed into clearer inventory decisions and proactive restocking alerts.

> This is a prototype and not a production Shell system.

## Problem

Fuel stations need to understand how quickly each fuel type is being consumed, how much inventory remains, and when a new delivery should be scheduled.

A reactive process can create two opposite problems:

- Fuel runs out before the next delivery.
    
- Excess inventory is ordered unnecessarily.
    

This prototype explores how operational data can support more proactive inventory management.

## What the Prototype Does

The backend exposes fuel analytics used by the dashboard to provide information such as:

- Current fuel inventory
    
- Tank capacity
    
- Weekly liters sold
    
- Seven-day average consumption
    
- Fuel pricing
    
- Recent restock information
    
- Estimated days until depletion
    
- Projected depletion date
    
- Recommended restocking date
    
- Operational alerts
    

The prototype currently supports:

- Regular
    
- Super
    
- Diesel
    

## Restocking Logic

The current recommendation model intentionally uses a simple and explainable approach rather than a complex machine-learning model.

### 1. Recent Consumption

The system analyzes recent daily fuel consumption and calculates a seven-day average.

### 2. Days to Depletion

Estimated remaining operating time is calculated from:

```text
current inventory / average daily consumption
```

### 3. Projected Depletion

The system estimates the date on which the current inventory would be depleted if recent consumption continues.

### 4. Restocking Recommendation

The expected delivery lead time is applied to the projected depletion date to recommend when the next restock should occur.

### 5. Alerts

Operational status is classified according to the estimated depletion window.

- **Warning:** projected depletion within seven days
    
- **Critical:** projected depletion within three days
    

The dashboard can use these statuses to highlight fuels requiring attention.

## Current Data Status

The current analytics endpoint uses synthetic/demo consumption history to validate the concept.

It is **not connected to live Shell operational data**.

The prototype was designed so that the demo data source can later be replaced with a real operational source.

## Architecture

The application uses a multi-service Docker architecture.

```text
             ┌──────────────┐
             │   Frontend   │
             └──────┬───────┘
                    │
                    ▼
             ┌──────────────┐
             │   FastAPI    │
             │   Backend    │
             └──────┬───────┘
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
 ┌────────────────┐   ┌────────────────┐
 │ PostgreSQL /   │   │     Redis      │
 │ PostGIS        │   │                │
 └────────────────┘   └────────────────┘

           Reverse Proxy
                │
                ▼
              Nginx
```

Docker Compose manages the main services.

## Tech Stack

### Backend

- Python
    
- FastAPI
    
- SQLAlchemy
    
- Pandas
    

### Data & Infrastructure

- PostgreSQL
    
- PostGIS
    
- Redis
    
- Docker
    
- Docker Compose
    
- Nginx
    

## API

The main analytics endpoint is:

```text
GET /api/gasoline/stats
```

It provides the data required for the fuel-monitoring dashboard and operational indicators.

## Running the Project

Clone the repository:

```bash
git clone https://github.com/A625A/GasolineraGestion.git
cd GasolineraGestion
```

Create local environment files from the examples:

```bash
cp env/backend.env.example env/backend.env
cp env/frontend.env.example env/frontend.env
```

Review the generated files and replace development placeholders where necessary.

Then run:

```bash
docker compose up --build
```

The Docker environment includes services for:

```text
frontend
backend
postgres
redis
nginx
```

The backend is exposed locally on:

```text
http://localhost:8000
```

## Security

Real credentials should never be committed to the repository.

The committed environment files are templates using placeholder values such as:

```text
CHANGE_ME
```

Local credentials should be stored only in local environment files excluded from Git.

## Current Limitations

This project is a prototype.

Current limitations include:

- Demo/synthetic consumption history
    
- No connection to live station transaction systems
    
- No production Shell integration
    
- Restocking recommendations use an intentionally simple forecasting approach
    
- Recommendations should be treated as decision-support indicators, not automatic purchasing decisions
    

## Future Improvements

Possible next steps include:

- Connect real transaction and inventory data
    
- Store historical consumption in PostgreSQL
    
- Compare consumption behavior by station
    
- Add seasonality and day-of-week effects
    
- Improve demand forecasting
    
- Add delivery lead-time uncertainty
    
- Build station-level anomaly detection
    
- Add historical KPI reporting
    
- Evaluate forecasting performance against actual consumption
    

## Purpose

The objective of this project is not to create an unnecessarily complex prediction model.

It is to demonstrate how fuel-station operational data can be transformed into **clear, explainable, and actionable inventory information**.

