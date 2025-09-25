# Football Data Webscraping

A comprehensive Python toolkit for scraping football data from multiple sources including FBref, Transfermarkt, Understat, SofaScore, and WhoScored. This project demonstrates various web scraping techniques from simple HTTP requests to advanced Selenium automation.

## Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Data Sources](#data-sources)
- [Modules Overview](#modules-overview)
- [Usage Examples](#usage-examples)
- [Anti-Detection Techniques](#anti-detection-techniques)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Overview

This project provides ready-to-use Python modules for extracting football data from major sports websites. Each module is designed to handle the specific challenges of its target website, from simple HTML parsing to complex JavaScript rendering and API interactions.

### Key Features

- **Multiple Data Sources**: FBref, Transfermarkt, Understat, SofaScore, WhoScored
- **Various Scraping Methods**: HTTP requests, HTML parsing, Selenium automation, API calls
- **Anti-Detection Measures**: Rate limiting, user agent rotation, delays
- **Data Processing**: Pandas DataFrames for easy analysis
- **Robust Error Handling**: Comprehensive exception handling and logging

## Installation

### Prerequisites

- Python 3.9 or higher
- Chrome browser (for Selenium-based scrapers)

### Setup Instructions

1. **Clone the repository**

   ```bash
   git clone https://github.com/sahil-gidwani/football-data-webscraping.git
   cd football-data-webscraping
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

For detailed setup instructions, see [SETUP.md](SETUP.md).

## Quick Start

Here's a simple example to get you started:

```python
from fbref.fbref_player_data import load_fbref_player_data

# Load player statistics from FBref
url = "https://fbref.com/en/comps/Big5/stats/players/Big-5-European-Leagues-Stats"
player_data = load_fbref_player_data(url)
print(player_data.head())
```

## Data Sources

### 1. FBref

- **Purpose**: Player and team statistics
- **Method**: HTML table parsing with pandas
- **Best for**: Season statistics, historical data

### 2. Transfermarkt

- **Purpose**: Player profiles, transfer values, market data
- **Method**: BeautifulSoup HTML parsing
- **Best for**: Player information, transfer history

### 3. Understat

- **Purpose**: Expected goals (xG) and shot data
- **Method**: JavaScript data extraction
- **Best for**: Advanced analytics, shot maps

### 4. SofaScore

- **Purpose**: Live scores, match data, player ratings
- **Method**: API calls via curl conversion
- **Best for**: Real-time data, match events

**Note**: For FotMob, you can follow a similar process to what's used for SofaScore.

### 5. WhoScored

- **Purpose**: Detailed match events and player ratings
- **Method**: Selenium with JavaScript execution
- **Best for**: Event-level match data

## Modules Overview

### Core Scraping Modules

#### FBref (`fbref/`)

```python
from fbref.fbref_player_data import load_fbref_player_data
from fbref.fbref_team_data import load_fbref_team_data

# Load player data
player_data = load_fbref_player_data(url, table_index=0)

# Load team data
team_data = load_fbref_team_data(url)
```

#### Transfermarkt (`transfermarkt/`)

```python
from transfermarkt.transfermarkt_data import get_transfermarkt_player_info

# Get detailed player information
player_info = get_transfermarkt_player_info(player_url)
```

#### Understat (`understat/`)

```python
from understat.understat_shots_data import load_understat_shots_data

# Load shot data for analysis
shots_data = load_understat_shots_data(player_url)
```

#### SofaScore (`sofascore/`)

```python
from sofascore.sofascore_api import convert_curl_to_requests

# Convert curl command to Python request
response = convert_curl_to_requests(curl_command)
```

#### WhoScored (`whoscored/`)

```python
from whoscored.whoscored_events_data import load_whoscored_events_data

# Load match event data
events_data = load_whoscored_events_data(match_url)
```

### Utility Modules

#### WebDriver Management (`utils/`)

```python
from utils.driver import DriverContext, get_driver

# Context manager for driver lifecycle
with DriverContext() as driver:
    driver.get(url)
    # Perform scraping operations

# Simple driver getter
with get_driver() as driver:
    driver.get(url)
```

#### Anti-Detection (`scraping-countermeasures/`)

```python
from scraping_countermeasures.rate_limiter import RateLimiter
from scraping_countermeasures.delays import add_random_delay
from scraping_countermeasures.user_agent_rotation import get_random_user_agent

# Rate limiting
limiter = RateLimiter(max_requests=10, time_window=60)
limiter.wait_if_needed()

# Random delays
add_random_delay(min_delay=1, max_delay=3)

# User agent rotation
headers = {'User-Agent': get_random_user_agent()}
```

## Usage Examples

### Example 1: FBref Player Statistics

```python
from fbref.fbref_player_data import load_fbref_player_data
import pandas as pd

# Load Premier League player stats
url = "https://fbref.com/en/comps/9/stats/Premier-League-Stats"
df = load_fbref_player_data(url)

# Analyze top scorers
top_scorers = df.nlargest(10, 'Goals')
print("Top 10 Goal Scorers:")
print(top_scorers[['Player', 'Goals', 'Team']])
```

### Example 2: Transfermarkt Player Information

```python
from transfermarkt.transfermarkt_data import get_transfermarkt_player_info

# Get player information
player_url = "https://www.transfermarkt.com/player/profil/spieler/123"
player_info = get_transfermarkt_player_info(player_url)

print(f"Player: {player_info.get('name')}")
print(f"Market Value: {player_info.get('market_value')}")
print(f"Position: {player_info.get('position')}")
```

### Example 3: Understat Shot Analysis

```python
from understat.understat_shots_data import load_understat_shots_data
import matplotlib.pyplot as plt

# Load shot data
player_url = "https://understat.com/player/123"
shots_df = load_understat_shots_data(player_url)

# Create shot map
plt.figure(figsize=(12, 8))
plt.scatter(shots_df['X'], shots_df['Y'],
           c=shots_df['xG'], cmap='Reds', alpha=0.6)
plt.title('Shot Map - xG Values')
plt.show()
```

### Example 4: Rate-Limited Scraping

```python
from scraping_countermeasures.rate_limiter import RateLimiter
from scraping_countermeasures.delays import add_random_delay
import requests

# Initialize rate limiter
limiter = RateLimiter(max_requests=5, time_window=60)

urls = ["url1", "url2", "url3", "url4", "url5"]

for url in urls:
    # Wait if necessary to respect rate limits
    limiter.wait_if_needed()

    # Add random delay between requests
    add_random_delay(min_delay=2, max_delay=5)

    # Make request
    response = requests.get(url)
    print(f"Scraped: {url} - Status: {response.status_code}")
```

## Anti-Detection Techniques

This project implements several techniques to avoid detection and blocking:

### 1. Rate Limiting

```python
# Limit requests per time window
limiter = RateLimiter(max_requests=10, time_window=60)
```

### 2. Random Delays

```python
# Add unpredictable delays between requests
add_random_delay(min_delay=1, max_delay=5)
```

### 3. User Agent Rotation

```python
# Rotate user agents to appear as different browsers
headers = {'User-Agent': get_random_user_agent()}
```

### 4. Selenium Stealth Options

```python
# Chrome options to avoid detection
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option('useAutomationExtension', False)
```

## Best Practices

### 1. Respect robots.txt

Always check the website's robots.txt file before scraping:

```
https://example.com/robots.txt
```

### 2. Use Appropriate Delays

```python
# Don't overload servers
add_random_delay(min_delay=2, max_delay=5)
```

### 3. Handle Errors Gracefully

```python
try:
    data = scrape_function(url)
except Exception as e:
    print(f"Error scraping {url}: {e}")
    continue
```

### 4. Cache Results

```python
# Save results to avoid re-scraping
df.to_csv(f'data_{timestamp}.csv', index=False)
```

### 5. Monitor Your Requests

```python
# Log all requests for debugging
print(f"Making request to: {url}")
print(f"Status code: {response.status_code}")
```

## Troubleshooting

### Common Issues

#### 1. Chrome Driver Issues

```bash
# Update ChromeDriver automatically
pip install --upgrade webdriver-manager
```

#### 2. Rate Limiting Errors

```python
# Increase delays between requests
add_random_delay(min_delay=5, max_delay=10)
```

#### 3. JavaScript Rendering Issues

```python
# Use Selenium instead of requests
from utils.driver import get_driver

with get_driver() as driver:
    driver.get(url)
    # Wait for content to load
    time.sleep(5)
```

#### 4. HTML Structure Changes

```python
# Use multiple selectors as fallbacks
selectors = ['div.stats', '.player-stats', '#statistics']
for selector in selectors:
    element = soup.select_one(selector)
    if element:
        break
```

## Project Structure

```
football-data-webscraping/
├── fbref/                          # FBref scrapers
│   ├── fbref_player_data.py       # Player statistics
│   ├── fbref_selenium.py          # Selenium-based scraper
│   └── fbref_team_data.py         # Team statistics
├── sofascore/                      # SofaScore scrapers
│   ├── sofascore_api.py           # API interactions
│   ├── sofascore_endpoints.py     # Endpoint definitions
│   ├── sofascore_json.py          # JSON data parsing
│   └── sofascore_selenium.py      # Selenium scraper
├── transfermarkt/                  # Transfermarkt scrapers
│   └── transfermarkt_data.py      # Player and transfer data
├── understat/                      # Understat scrapers
│   └── understat_shots_data.py    # Shot and xG data
├── whoscored/                      # WhoScored scrapers
│   └── whoscored_events_data.py   # Match events data
├── scraping-countermeasures/       # Anti-detection tools
│   ├── delays.py                  # Random delays
│   ├── rate_limiter.py           # Rate limiting
│   └── user_agent_rotation.py    # User agent management
├── utils/                          # Utility functions
│   └── driver.py                  # WebDriver management
└── soccerdata/                     # Additional data tools
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Guidelines

- Follow PEP 8 style guidelines
- Add docstrings to all functions
- Include error handling in scrapers
- Test with multiple URLs before submitting
- Update documentation for new features

## Legal and Ethical Considerations

- Always respect website terms of service
- Implement appropriate delays between requests
- Don't overload servers with excessive requests
- Use scraped data responsibly and legally
- Consider reaching out to websites for official API access

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

**Disclaimer**: This project is for educational purposes. Always check website terms of service and robots.txt before scraping. The authors are not responsible for any misuse of this code.
